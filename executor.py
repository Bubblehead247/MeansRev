"""
executor.py — All Alpaca API interactions: placing orders, confirming fills,
placing hard stops, and submitting exits.

Order flow for entries:
  1. 9:25 AM → submit_entry() places a Limit OPG (LOO) buy order at prior close
  2. 9:45 AM → confirm_fills_and_place_stops() checks if filled:
               - Filled → places a GTC hard stop on the exchange using ACTUAL fill price
               - Unfilled (order expired) → submits a DAY market buy as fallback
               - Still pending → leaves it; will retry at next confirm cycle

The hard stop sits on Alpaca's servers — it executes even if this bot crashes.
"""
import logging
import time
from datetime import date, datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce

from quantcore.reconcile import blended_fill_price

import config
import position_tracker as pt
import risk
import trade_log

logger = logging.getLogger(__name__)

_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)

#: How many times to re-read a just-placed order looking for its fill, and how
#: long to wait between tries. A DAY market order at 09:45 fills in well under a
#: second; this only exists so an exit can be booked at its real price instead of
#: a quote taken before the order was even sent.
_FILL_POLL_TRIES = 5
_FILL_POLL_SECONDS = 1.0


# ── Account ───────────────────────────────────────────────────────────────────

def get_equity() -> float:
    account = _client.get_account()
    return float(account.equity)


def get_account_snapshot() -> dict:
    """Equity, prior-day equity, and since-inception baseline, in one call.

    ``TradingClient`` in this alpaca-py version has no typed
    ``get_portfolio_history`` method — only the broker client does, even
    though the trading-API request model exists — so this hits the raw
    endpoint via ``_client.get()`` instead of building a typed request.
    """
    account = _client.get_account()
    history = _client.get(
        "/account/portfolio/history", {"period": "all", "timeframe": "1D"}
    )
    return {
        "equity":          float(account.equity),
        "last_equity":     float(account.last_equity),
        "base_value":      float(history["base_value"]),
        "base_value_asof": history["base_value_asof"],
    }


# ── Market Calendar ───────────────────────────────────────────────────────────

def is_trading_day_today() -> bool:
    """Return True if today has (or had) a regular US equities trading session."""
    today = date.today()
    sessions = _client.get_calendar(GetCalendarRequest(start=today, end=today))
    return len(sessions) > 0


# ── Position Checks ───────────────────────────────────────────────────────────

def get_alpaca_positions() -> dict:
    """Returns {symbol: position_object} for all open Alpaca positions."""
    return {p.symbol: p for p in _client.get_all_positions()}


def has_position(symbol: str) -> bool:
    return symbol in get_alpaca_positions()


# ── Entry ─────────────────────────────────────────────────────────────────────

def submit_entry(scan_result: dict) -> bool:
    """
    Submit a Limit OPG (LOO) buy order at prior close + ENTRY_LIMIT_PCT.
    Position size is estimated using yesterday's close and the active stop.
    The actual hard stop is placed after fill confirmation.

    Returns True if an order was successfully submitted, False otherwise.
    """
    symbol      = scan_result["symbol"]
    est_entry   = scan_result["close"]       # Yesterday's close — size estimate only
    active_stop = scan_result["active_stop"]
    stop_a      = scan_result["stop_a"]
    stop_b      = scan_result["stop_b"]

    if has_position(symbol):
        logger.info(f"Skipping {symbol} entry — position already open.")
        return False

    equity = get_equity()
    shares = risk.calculate_position_size(equity, est_entry, active_stop)

    if shares <= 0:
        logger.warning(f"Skipping {symbol} — calculated 0 shares.")
        return False

    try:
        limit_price = round(est_entry * (1 + config.ENTRY_LIMIT_PCT), 2)
        order = _client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.BUY,
                # DAY, not OPG. Auction-only orders got one chance at the open
                # and 8 of 9 produced no usable fill — four gapped through the
                # limit, and five had the open comfortably inside the limit and
                # still did not fill, which is not a pricing problem at all. A
                # DAY limit keeps the same price discipline but stays live for
                # the whole session, so a stock that pulls back later in the
                # morning still gets bought. (bot-review D5, option B)
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        )

        logger.info(
            f"📥 BUY LIMIT ORDER submitted | {symbol} | {shares} shares | "
            f"Est. entry=${est_entry:.2f} | Limit=${limit_price:.2f} | Order ID={order.id}"
        )

        pt.add(
            symbol=symbol,
            entry_price=est_entry,
            stop_price=active_stop,
            stop_price_a=stop_a,
            stop_price_b=stop_b,
            shares=shares,
            stop_mult=config.ACTIVE_STOP_MULT,
            stop_order_id=None,
            rsi2=scan_result.get("rsi2"),
            sma200_weekly=scan_result.get("sma200_weekly"),
            sma50_daily=scan_result.get("sma50_daily"),
            atr14=scan_result.get("atr14"),
            weekly_adx=scan_result.get("weekly_adx"),
            # regime_band_ok, not regime_ok: the latter is filter-gated and reads
            # True unconditionally while config.USE_REGIME_FILTER is off, which
            # would make the shadow log say nothing failed the band, ever.
            regime_ok=scan_result.get("regime_band_ok"),
        )
        return True

    except Exception as e:
        logger.error(f"Entry order failed for {symbol}: {e}", exc_info=True)
        return False


# ── Fill Confirmation + Hard Stop Placement ───────────────────────────────────

def confirm_fills_and_place_stops(final: bool = False):
    """
    Confirm entry fills and protect them:
      1. Confirm entry buy orders have been filled.
      2. Calculate stop from ACTUAL fill price using ATR at signal time.
      3. Place a GTC hard stop on the exchange.

    Runs twice a day, because entries are DAY limits that stay live for the whole
    session (see ``submit_entry``):

    * ``final=False`` — the morning pass. A still-open entry order is normal; it
      is left working and picked up later.
    * ``final=True``  — the pre-close pass. Anything still open now will expire
      at the close, so it is cancelled and its tracker record removed. Without
      this the record survives to tomorrow, when the next morning's pass would
      see "no position, no open order" and buy a day-old signal at market.

    An entry that never fills is a trade not taken, which is the point of having
    a limit. There is no market fallback: the old one existed because an OPG
    order was dead by 09:45, and chasing at market is what the price discipline
    is there to avoid.
    """
    logger.info("Confirming fills and placing hard stops...")

    tracked          = pt.all_positions()
    alpaca_positions = get_alpaca_positions()

    for symbol, pos_data in tracked.items():
        if pos_data.get("stop_order_id"):
            logger.info(f"{symbol}: Stop already placed (ID={pos_data['stop_order_id']}), skipping.")
            continue

        if symbol not in alpaca_positions:
            # Check whether an OPG order is still open (submitted but not yet processed)
            try:
                open_orders = _client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                )
                open_buys = [o for o in open_orders if o.side == OrderSide.BUY]
            except Exception as e:
                logger.error(f"Could not check open orders for {symbol}: {e}", exc_info=True)
                open_buys = []

            if open_buys and not final:
                logger.info(
                    f"{symbol}: entry limit still working (ID={open_buys[0].id}). "
                    f"Leaving it — a DAY limit has the whole session to fill."
                )
                continue

            if open_buys:  # final pass: it will expire at the close anyway
                for order in open_buys:
                    try:
                        _client.cancel_order_by_id(order.id)
                        logger.info(
                            f"{symbol}: entry limit unfilled at the close — "
                            f"cancelled (ID={order.id}). Trade not taken."
                        )
                    except Exception as e:
                        logger.error(f"Could not cancel {symbol} entry: {e}", exc_info=True)
                pt.remove(symbol)
                continue

            # No position and no working order: the order was rejected, cancelled
            # or expired. The signal was for today's open, so it is stale now.
            logger.warning(
                f"{symbol}: no position and no working entry order — "
                f"removing tracker record. Trade not taken."
            )
            pt.remove(symbol)
            continue

        # Position is open — calculate stop from actual fill price
        alpaca_pos = alpaca_positions[symbol]
        fill_price = float(alpaca_pos.avg_entry_price)
        shares     = int(float(alpaca_pos.qty))

        # Use ATR stored at signal time so the stop distance is correct for this entry
        atr_at_signal = pos_data.get("atr14_at_signal")
        if atr_at_signal:
            actual_stop = round(fill_price - (config.ACTIVE_STOP_MULT * float(atr_at_signal)), 2)
        else:
            # Fallback for positions recorded before atr14_at_signal was stored
            actual_stop = pos_data["stop_price"]
            logger.warning(f"{symbol}: atr14_at_signal not found, using estimated stop ${actual_stop:.2f}")

        pt.update_entry_price(symbol, fill_price)
        # Record what actually happened, not what was estimated before the fill:
        # the real filled quantity, and — once the stop is accepted below — the
        # real stop price. Leaving stop_price at its pre-fill estimate is what
        # made positions.json, the daily status push and trades.csv all report a
        # stop the exchange had never heard of.
        pt.update_shares(symbol, shares)

        stop_order_id = _place_stop_order(symbol, actual_stop, shares)
        if stop_order_id:
            pt.update_stop_order_id(symbol, stop_order_id)
            pt.update_stop_price(symbol, actual_stop)
            logger.info(
                f"✅ Fill confirmed + stop placed | {symbol} | "
                f"Fill=${fill_price:.2f} | Stop=${actual_stop:.2f} | "
                f"Stop Order ID={stop_order_id}"
            )


def confirm_exit_fills():
    """
    Run alongside confirm_fills_and_place_stops() at 9:45 ET.

    Resolves each exit_pending position:
      - Position gone from Alpaca  → fill confirmed, log trade, remove from tracker.
      - Open sell order still live → leave as exit_pending.
      - Position held, no sell     → LOO expired; submit DAY market sell as fallback.
    """
    logger.info("Checking exit_pending positions for fill confirmation...")

    exit_pending = pt.get_exit_pending_symbols()
    if not exit_pending:
        logger.info("No exit_pending positions to confirm.")
        return

    alpaca_positions = get_alpaca_positions()

    for symbol in exit_pending:
        if symbol not in alpaca_positions:
            pos_data   = pt.get(symbol)
            log_reason = (pos_data.get("exit_reason", "signal_exit") if pos_data else "signal_exit")
            # Blend every sell fill for the day, so an exit that filled in two
            # pieces is booked at its true average rather than one of its prices.
            #
            # The position can vanish from get_alpaca_positions() slightly before
            # its fill is visible through get_orders() — two different endpoints,
            # not updated atomically. On 2026-08-04 that gap outlasted a single
            # check and stranded XLF/QQQ in needs_review for a full session, even
            # though the fill was there and correct within seconds. Once flagged,
            # nothing re-checks it (mark_needs_review deliberately stops
            # re-examination, to avoid re-alerting on a genuine gap) — so a
            # transient miss here is permanent. Retry briefly before giving up.
            for attempt in range(_FILL_POLL_TRIES):
                fills = get_fills(symbol, side="sell")
                filled_qty, blended_price = blended_fill_price(fills)
                if filled_qty > 0 or attempt == _FILL_POLL_TRIES - 1:
                    break
                time.sleep(_FILL_POLL_SECONDS)

            if pos_data and filled_qty > 0:
                trade_log.log_closed_trade(
                    pos_data, blended_price, log_reason, shares=filled_qty,
                )
                logger.info(f"{symbol}: Exit fill confirmed. Removing from tracker.")
                pt.remove(symbol)
            else:
                # The old code removed the record here regardless, so a real exit
                # whose fill could not be read vanished with no row in trades.csv
                # at all. Keep it and say so instead.
                logger.error(
                    f"{symbol}: gone from Alpaca but no sell fill could be read — "
                    f"NOT booking a trade and NOT dropping the record. Flagged for review."
                )
                pt.mark_needs_review(
                    symbol,
                    "position disappeared while an exit was pending, but no sell "
                    "fill was found to price it",
                )
            continue

        try:
            open_orders = _client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            )
            open_sells = [o for o in open_orders if o.side == OrderSide.SELL]
        except Exception as e:
            logger.error(f"Could not check open orders for {symbol}: {e}", exc_info=True)
            open_sells = []

        if open_sells:
            logger.info(
                f"{symbol}: Exit order still open (ID={open_sells[0].id}). "
                f"Leaving as exit_pending."
            )
            continue

        # LOO sell expired — submit DAY market sell as fallback
        shares = int(float(alpaca_positions[symbol].qty))
        logger.warning(
            f"{symbol}: LOO exit expired unfilled. "
            f"Submitting fallback DAY market sell for {shares} shares..."
        )
        try:
            pos_data = pt.get(symbol)
            fallback = _client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            logger.info(
                f"🔄 FALLBACK SELL submitted | {symbol} | {shares} shares | "
                f"Order ID={fallback.id}"
            )

            # Wait for the order to actually fill. The old code booked the trade
            # from `current_price` — the position's mark at ~09:45, taken before
            # the order was even sent — and then deleted the tracker record, so
            # the real fill could never be reconciled. Every *_fallback row in
            # trades.csv was an estimate rather than a realized price.
            if await_fill(str(fallback.id)) is None:
                logger.error(
                    f"{symbol}: fallback sell {fallback.id} has not filled yet. "
                    f"Leaving as exit_pending — nothing booked, no price guessed."
                )
                continue

            # Blend every sell fill for today, not just this order. The exit may
            # have filled in two pieces: a partial fill in the opening auction
            # (whose order then expired) plus this market fallback. DIA on
            # 2026-07-28 filled 21 @ 525.53 and 19 @ 524.391052 — a blended
            # 524.9890 across 40 shares, which the old code recorded as a single
            # price on all 40, understating the trade by about $22.
            fills = get_fills(symbol, side="sell")
            filled_qty, blended_price = blended_fill_price(fills)
            if filled_qty <= 0:
                logger.error(
                    f"{symbol}: sell filled but no fill record could be read. "
                    f"Leaving as exit_pending rather than guessing a price."
                )
                continue

            if pos_data:
                log_reason = pos_data.get("exit_reason", "signal_exit") + "_fallback"
                if len(fills) > 1:
                    logger.info(
                        f"{symbol}: exit filled in {len(fills)} pieces totalling "
                        f"{filled_qty:g} shares, blended ${blended_price:.4f}."
                    )
                trade_log.log_closed_trade(
                    pos_data, blended_price, log_reason, shares=filled_qty,
                )
            pt.remove(symbol)
        except Exception as e:
            logger.error(f"Fallback exit order failed for {symbol}: {e}", exc_info=True)


def get_last_fill_price(symbol: str, side: str,
                        not_before: date | None = None) -> float | None:
    """Most recent fill price for ``symbol`` on ``side``, no earlier than a date.

    ``not_before`` matters more than it looks. This walks the last 10 closed
    orders and takes the first match, and MeansRev's universe is small enough
    that SPY, QQQ and DIA each appear in it repeatedly — so without a bound, a
    *previous* round trip's sell can be handed back as the exit price for the
    trade being booked now. The caller passes the position's own entry date:
    nothing before a position was opened can be its exit.

    Returns ``None`` when there is no qualifying fill, which callers must treat
    as "no evidence" rather than substituting a guess.
    """
    try:
        orders = _client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=10)
        )
        for order in orders:
            if (order.side == OrderSide(side.lower())
                    and order.filled_at
                    and order.filled_avg_price):
                if not_before is not None and order.filled_at.date() < not_before:
                    logger.info(
                        f"{symbol}: ignoring a {side} filled {order.filled_at.date()}, "
                        f"before this position opened ({not_before})."
                    )
                    continue
                return float(order.filled_avg_price)
    except Exception as e:
        logger.error(f"Could not fetch fill price for {symbol} ({side}): {e}", exc_info=True)
    return None


def get_fills(symbol: str, side: str, on_date: date | None = None) -> list[dict]:
    """Every fill for symbol on the given side, as plain dicts.

    Includes fills carried by orders that did **not** end as ``filled``: an
    Alpaca order can expire or be canceled having already filled part of its
    quantity. DIA's opening-auction sell on 2026-07-28 expired holding 21 of its
    40 shares, and ignoring it is what understated that trade by about $22.

    Args:
        symbol: Ticker.
        side: ``"buy"`` or ``"sell"``.
        on_date: Only fills from this date. Defaults to today.

    Returns:
        Dicts with ``filled_qty``/``filled_avg_price``, ready for
        :func:`quantcore.reconcile.blended_fill_price`.
    """
    want_date = on_date or date.today()
    try:
        orders = _client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=50)
        )
    except Exception as e:
        logger.error(f"Could not fetch fills for {symbol} ({side}): {e}", exc_info=True)
        return []

    out = []
    for order in orders:
        if order.side != OrderSide(side.lower()):
            continue
        filled_qty = getattr(order, "filled_qty", None)
        filled_price = getattr(order, "filled_avg_price", None)
        if not filled_qty or not filled_price:
            continue
        when = getattr(order, "filled_at", None) or getattr(order, "updated_at", None)
        if when is not None and when.date() != want_date:
            continue
        out.append({
            "id": str(order.id),
            "symbol": symbol,
            "side": side.lower(),
            "filled_qty": str(filled_qty),
            "filled_avg_price": str(filled_price),
            "status": str(getattr(order, "status", "")),
        })
    return out


def await_fill(order_id: str, tries: int = _FILL_POLL_TRIES) -> tuple[float, float] | None:
    """Wait briefly for an order to fill, and return ``(qty, price)``.

    Returns ``None`` if it has not filled in time. The caller must then leave the
    position alone rather than booking a guessed price — a market order at 09:45
    normally fills in well under a second, so not filling is real news.
    """
    for attempt in range(tries):
        try:
            order = _client.get_order_by_id(order_id)
        except Exception as e:
            logger.error(f"Could not re-read order {order_id}: {e}", exc_info=True)
            return None
        qty = getattr(order, "filled_qty", None)
        price = getattr(order, "filled_avg_price", None)
        if qty and price and float(qty) > 0:
            return float(qty), float(price)
        status = str(getattr(order, "status", "")).lower()
        if any(dead in status for dead in ("canceled", "expired", "rejected")):
            logger.warning(f"Order {order_id} ended {status} without filling.")
            return None
        if attempt < tries - 1:
            time.sleep(_FILL_POLL_SECONDS)
    return None


def get_filled_qty(symbol: str, side: str) -> float | None:
    """Total shares the broker actually filled for symbol on the given side.

    Counts every order that moved shares, whatever its final status: an order
    can end ``expired`` or ``canceled`` and still have filled part of its
    quantity. Returns ``0.0`` when orders exist but none of them filled — which
    is proof that nothing was traded — and ``None`` only when the lookup itself
    failed, so "no evidence" is never confused with "evidence of nothing".
    """
    try:
        orders = _client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=50)
        )
    except Exception as e:
        logger.error(f"Could not fetch orders for {symbol} ({side}): {e}", exc_info=True)
        return None

    total = 0.0
    for order in orders:
        if order.side != OrderSide(side.lower()):
            continue
        filled = getattr(order, "filled_qty", None)
        if filled:
            total += float(filled)
    return total


def _place_stop_order(symbol: str, stop_price: float, shares: int) -> str | None:
    """Place a GTC hard stop (sell) order on the exchange."""
    try:
        order = _client.submit_order(
            StopOrderRequest(
                symbol=symbol,
                qty=shares,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )
        )
        logger.info(f"🛑 STOP ORDER placed | {symbol} | ${stop_price:.2f} | Order ID={order.id}")
        return str(order.id)
    except Exception as e:
        logger.error(f"Failed to place stop order for {symbol}: {e}", exc_info=True)
        return None


# ── Exit ──────────────────────────────────────────────────────────────────────

def submit_exit(symbol: str, reason: str = "signal", close_price: float = 0.0) -> bool:
    """
    Submit a LOO (Limit on Open) sell order for the next day's open.
    Cancels any open stop orders first to avoid a double-sell.
    Marks the position as exit_pending — confirm_exit_fills() removes it after fill.

    Returns True if exit order submitted, False otherwise.
    """
    if not has_position(symbol):
        logger.info(f"No open position in {symbol} — nothing to exit.")
        return False

    _cancel_stop_for_symbol(symbol)

    alpaca_positions = get_alpaca_positions()
    shares = int(float(alpaca_positions[symbol].qty))

    try:
        limit_price = round(close_price * (1 - config.EXIT_LIMIT_PCT), 2) if close_price > 0 else None

        if limit_price:
            order = _client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.OPG,  # LOO — Limit on Open
                    limit_price=limit_price,
                )
            )
            logger.info(
                f"📤 SELL LIMIT ORDER submitted | {symbol} | {shares} shares | "
                f"Reason={reason} | Limit=${limit_price:.2f} | Order ID={order.id}"
            )
        else:
            logger.warning(f"{symbol}: No close price for limit exit — submitting market OPG sell.")
            order = _client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.OPG,
                )
            )
            logger.info(
                f"📤 SELL MARKET ORDER submitted | {symbol} | {shares} shares | "
                f"Reason={reason} (market fallback) | Order ID={order.id}"
            )

        pt.mark_exit_pending(symbol, reason=reason)
        return True

    except Exception as e:
        logger.error(f"Exit order failed for {symbol}: {e}", exc_info=True)
        return False


def _cancel_stop_for_symbol(symbol: str):
    """Cancel all open stop orders for a symbol before submitting an exit."""
    stop_order_id = pt.get_stop_order_id(symbol)

    if stop_order_id:
        try:
            _client.cancel_order_by_id(stop_order_id)
            logger.info(f"Cancelled stop order {stop_order_id} for {symbol}")
            return
        except Exception as e:
            logger.warning(f"Could not cancel stop by ID for {symbol}: {e}. Falling back to full scan.")

    try:
        open_orders = _client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        )
        for order in open_orders:
            if hasattr(order, "order_type") and "stop" in str(order.order_type).lower():
                _client.cancel_order_by_id(str(order.id))
                logger.info(f"Cancelled stop order {order.id} for {symbol} (fallback scan)")
    except Exception as e:
        logger.error(f"Error in stop cancellation fallback for {symbol}: {e}", exc_info=True)
