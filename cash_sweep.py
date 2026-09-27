"""
cash_sweep.py — park MeansRev's idle cash in a T-bill ETF (config.CASH_SWEEP_SYMBOL).

The strategy averages ~35% of equity invested, so most of the account is cash.
This moves the cash the strategy doesn't need into a 0-3 month T-bill ETF and
takes it back out before it is needed. It is parked cash, not a trade:

* ``executor.get_alpaca_positions()`` leaves the ETF out, so it never takes a
  slot, counts toward a sector cap, or looks like a position without a stop.
* It is booked here, in ``cash_sweep.jsonl``, never in ``trades.csv``.
  quantcore's reconcile skips fills in symbols this ledger books.

When (all times ET):

* **Evening, after the 16:30 scan** (``evening_rebalance``): tomorrow's entries
  are known, so cash is set to their estimated cost + CASH_BUFFER_PCT of equity.
  A shortfall is sold after hours; spare cash above CASH_SWEEP_BAND_PCT of
  equity is bought, but only while the ETF's trailing yield clears
  CASH_SWEEP_MIN_YIELD.
* **09:25, before the entries** (``fund_entries``): if the evening sale didn't
  fill, the remaining shortfall is sold pre-market and waited for, because
  Alpaca checks buying power when an order is submitted.

Backtest: research/strategy_review_2026_09/sweep_tbill2.py.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

import config
import executor
import risk
import scanner

logger = logging.getLogger(__name__)

LEDGER_FILE = Path(__file__).resolve().parent / "cash_sweep.jsonl"

#: Limit prices sit this far through the quote, so the order is marketable but
#: can never fill at a silly price. SGOV's spread is ~1 bp.
_PRICE_TOLERANCE = 0.0005
#: Fall back to the last close when the quote is older than this.
_QUOTE_MAX_AGE = timedelta(minutes=30)
_FILL_TRIES = 30
_FILL_SECONDS = 1.0
#: Ignore trades smaller than this (dollars).
_MIN_TRADE = 100.0


# ── Inputs ────────────────────────────────────────────────────────────────────

def entry_cost(pending: list[dict], equity: float) -> float:
    """Estimated cash tomorrow's queued entries need: size x limit price.

    Uses the same sizing as ``executor.submit_entry`` (``risk``), priced at the
    entry limit (prior close + ENTRY_LIMIT_PCT), the most an entry can cost.
    """
    total = 0.0
    for action in pending:
        if action.get("action") != "ENTRY":
            continue
        scan = action["scan_result"]
        shares = risk.calculate_position_size(equity, scan["close"], scan["active_stop"])
        total += max(shares, 0) * scan["close"] * (1 + config.ENTRY_LIMIT_PCT)
    return total


def trailing_yield(days: int = 21) -> float | None:
    """The ETF's total return over ``days`` sessions, annualized (dividend-adjusted bars)."""
    try:
        bars = scanner._fetch_bars(config.CASH_SWEEP_SYMBOL, TimeFrame.Day, days * 2 + 15)
        close = bars["close"]
        if len(close) <= days:
            return None
        return (float(close.iloc[-1]) / float(close.iloc[-1 - days])) ** (252 / days) - 1
    except Exception as e:
        logger.error(f"Cash sweep: could not compute trailing yield: {e}", exc_info=True)
        return None


def _quote() -> tuple[float, float]:
    """(bid, ask) for the ETF. IEX quotes run 08:00-17:00 ET, covering both sweep
    times; the SIP feed is not available live on this data plan."""
    try:
        q = scanner._data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=config.CASH_SWEEP_SYMBOL, feed=DataFeed.IEX)
        )[config.CASH_SWEEP_SYMBOL]
        fresh = datetime.now(timezone.utc) - q.timestamp < _QUOTE_MAX_AGE
        if fresh and 0 < q.bid_price <= q.ask_price:
            return float(q.bid_price), float(q.ask_price)
        logger.warning(f"Cash sweep: quote stale or crossed ({q.bid_price}/{q.ask_price} at "
                       f"{q.timestamp}); pricing from the last close.")
    except Exception as e:
        logger.warning(f"Cash sweep: no quote ({e}); pricing from the last close.")
    close = float(scanner._fetch_bars(config.CASH_SWEEP_SYMBOL, TimeFrame.Day, 10)["close"].iloc[-1])
    return close, close


# ── Orders ────────────────────────────────────────────────────────────────────

def _cancel_working_orders() -> None:
    """Cancel any sweep order still working, so a new decision starts clean."""
    try:
        orders = executor._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[config.CASH_SWEEP_SYMBOL])
        )
    except Exception as e:
        logger.error(f"Cash sweep: could not list working orders: {e}", exc_info=True)
        return
    for order in orders:
        try:
            executor._client.cancel_order_by_id(order.id)
            logger.info(f"Cash sweep: cancelled working order {order.id} ({order.side}, {order.qty}).")
        except Exception as e:
            logger.error(f"Cash sweep: could not cancel {order.id}: {e}", exc_info=True)


def _wait(order_id: str):
    """Re-read the order until it is filled or dead, or time runs out."""
    order = None
    for attempt in range(_FILL_TRIES):
        order = executor._client.get_order_by_id(order_id)
        status = str(order.status).lower()
        if status.endswith("filled") and "partially" not in status:
            return order
        if any(dead in status for dead in ("canceled", "expired", "rejected")):
            return order
        if attempt < _FILL_TRIES - 1:
            time.sleep(_FILL_SECONDS)
    return order


def _trade(side: OrderSide, dollars: float, reason: str) -> dict | None:
    """Buy or sell ``dollars`` of the ETF with a marketable extended-hours limit.

    Extended-hours limit orders work pre-market, in the regular session and
    after hours, so one order type covers both sweep times. Fractional quantity
    is allowed for SGOV in extended hours (asset attribute fractional_eh_enabled).
    """
    bid, ask = _quote()
    if side == OrderSide.SELL:
        limit = round(bid * (1 - _PRICE_TOLERANCE), 2)
        held = executor.get_sweep_position()
        held_qty = float(held.qty) if held else 0.0
        qty = min(held_qty, math.ceil(dollars / bid * 10_000) / 10_000)
    else:
        limit = round(ask * (1 + _PRICE_TOLERANCE), 2)
        qty = math.floor(dollars / limit * 10_000) / 10_000
    if qty <= 0 or qty * limit < _MIN_TRADE:
        return None
    request = LimitOrderRequest(
        symbol=config.CASH_SWEEP_SYMBOL, qty=qty, side=side,
        time_in_force=TimeInForce.DAY, limit_price=limit, extended_hours=True,
        client_order_id=executor._client_order_id(f"sweep{side.value}", config.CASH_SWEEP_SYMBOL),
    )
    order = executor._submit(request)
    logger.info(f"Cash sweep: {side.value.upper()} {qty:g} {config.CASH_SWEEP_SYMBOL} limit "
                f"${limit:.2f} (~${qty * limit:,.0f}) | {reason} | Order ID={order.id}")
    order = _wait(str(order.id)) or order
    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    price = float(getattr(order, "filled_avg_price", 0) or 0)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": config.CASH_SWEEP_SYMBOL, "side": side.value, "reason": reason,
        "order_id": str(order.id), "client_order_id": request.client_order_id,
        "qty": filled_qty, "price": price, "notional": round(filled_qty * price, 2),
        "limit": limit, "requested_qty": qty, "status": str(order.status),
    }
    _append(row)
    if filled_qty < qty:
        logger.warning(f"Cash sweep: {side.value} order {order.id} filled {filled_qty:g} of "
                       f"{qty:g} so far ({order.status}); the rest stays working until it "
                       f"expires or the next sweep cancels it.")
    return row


def _append(row: dict) -> None:
    with open(LEDGER_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


_FINAL = ("filled", "canceled", "expired", "rejected")


def _is_final(status) -> bool:
    status = str(status).lower()
    return any(status.endswith(s) for s in _FINAL) and "partially" not in status


def settle(cancel: bool = True) -> None:
    """Cancel working sweep orders (unless ``cancel`` is False), then book any
    fills the ledger hasn't seen.

    ``_trade`` waits ~30 s. An order still partly filled then keeps working, and
    whatever fills afterwards would otherwise never reach the ledger, which
    would drift from the broker. An increase in an order's filled quantity is
    booked as an ``update`` row at the price of the extra shares.
    """
    if cancel:
        _cancel_working_orders()
    rows = ledger_rows()
    latest: dict[str, dict] = {}
    booked_qty: dict[str, float] = {}
    booked_cost: dict[str, float] = {}
    for r in rows:
        oid = r.get("order_id")
        if not oid:
            continue
        latest[oid] = r
        q = float(r.get("qty") or 0)
        booked_qty[oid] = booked_qty.get(oid, 0.0) + q
        booked_cost[oid] = booked_cost.get(oid, 0.0) + q * float(r.get("price") or 0)
    for oid, r in latest.items():
        if _is_final(r.get("status", "")):
            continue
        order = None
        for _ in range(5 if cancel else 1):
            order = executor._client.get_order_by_id(oid)
            if _is_final(order.status):
                break
            time.sleep(_FILL_SECONDS)
        total = float(order.filled_qty or 0)
        extra = max(total - booked_qty[oid], 0.0)
        avg = float(order.filled_avg_price or 0)
        price = (total * avg - booked_cost[oid]) / extra if extra > 1e-9 else 0.0
        if extra <= 1e-9 and not _is_final(order.status):
            continue   # still working, nothing new: no row
        _append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": r["symbol"], "side": r["side"], "reason": "update: later fill / final status",
            "order_id": oid, "client_order_id": r.get("client_order_id"),
            "qty": round(extra, 6), "price": round(price, 4),
            "notional": round(extra * price, 2), "status": str(order.status),
        })
        if extra > 1e-9:
            logger.info(f"Cash sweep: booked {extra:g} more {r['symbol']} from order {oid[:8]} "
                        f"@ {price:.4f} ({order.status}).")


# ── The two sweep points ──────────────────────────────────────────────────────

def evening_rebalance(pending: list[dict]) -> dict | None:
    """After the scan: hold tomorrow's entry cost + buffer in cash, sweep the rest."""
    if not config.CASH_SWEEP_ENABLED:
        return None
    settle()
    account = executor._client.get_account()
    equity, cash = float(account.equity), float(account.cash)
    need = entry_cost(pending, equity)
    free = cash - need - config.CASH_BUFFER_PCT * equity
    logger.info(f"Cash sweep (evening): equity ${equity:,.0f} | cash ${cash:,.0f} | "
                f"tomorrow's entries ~${need:,.0f} | buffer ${config.CASH_BUFFER_PCT * equity:,.0f} "
                f"| free ${free:,.0f}")
    if free < 0:
        return _trade(OrderSide.SELL, -free, "fund tomorrow's entries")
    if free > config.CASH_SWEEP_BAND_PCT * equity:
        y = trailing_yield()
        if y is None or y < config.CASH_SWEEP_MIN_YIELD:
            logger.info(f"Cash sweep: trailing yield {y if y is None else f'{y:.2%}'} under "
                        f"{config.CASH_SWEEP_MIN_YIELD:.0%}; not buying.")
            return None
        return _trade(OrderSide.BUY, free, f"park spare cash (trailing yield {y:.2%})")
    logger.info("Cash sweep: within band; no trade.")
    return None


def fund_entries(pending: list[dict]) -> dict | None:
    """09:25, before entries are submitted: sell whatever cash is still short."""
    if not config.CASH_SWEEP_ENABLED:
        return None
    account = executor._client.get_account()
    equity, cash = float(account.equity), float(account.cash)
    need = entry_cost(pending, equity)
    short = need - cash
    if need <= 0 or short <= 0:
        return None
    settle()
    short = need - float(executor._client.get_account().cash)
    if short <= 0:
        return None
    logger.warning(f"Cash sweep (morning): entries need ~${need:,.0f}, cash ${cash:,.0f}; "
                   f"selling ~${short:,.0f} of {config.CASH_SWEEP_SYMBOL} pre-market.")
    return _trade(OrderSide.SELL, short * 1.002, "fund this morning's entries")


# ── Reporting ─────────────────────────────────────────────────────────────────

def ledger_rows() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    return [json.loads(line) for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def report() -> dict:
    """What the sweep has made: realized + unrealized + dividends, and whether the
    ledger agrees with the broker's holding."""
    settle(cancel=False)
    rows = [r for r in ledger_rows() if r.get("qty")]
    lots: list[list[float]] = []            # FIFO [qty, price]
    realized = 0.0
    for r in rows:
        q, px = float(r["qty"]), float(r["price"])
        if r["side"] == "buy":
            lots.append([q, px])
            continue
        while q > 1e-9 and lots:
            take = min(q, lots[0][0])
            realized += take * (px - lots[0][1])
            lots[0][0] -= take
            q -= take
            if lots[0][0] <= 1e-9:
                lots.pop(0)
    ledger_qty = sum(q for q, _ in lots)
    pos = executor.get_sweep_position()
    broker_qty = float(pos.qty) if pos else 0.0
    unrealized = float(pos.unrealized_pl) if pos else 0.0
    dividends = 0.0
    first = rows[0]["timestamp"][:10] if rows else None
    if first:
        try:
            acts = executor._client.get("/account/activities/DIV", {"after": first})
            dividends = sum(float(a.get("net_amount", 0)) for a in acts
                            if a.get("symbol") == config.CASH_SWEEP_SYMBOL)
        except Exception as e:
            logger.warning(f"Cash sweep report: could not read dividends: {e}")
    return {
        "since": first, "holding_qty": broker_qty,
        "holding_value": float(pos.market_value) if pos else 0.0,
        "realized": round(realized, 2), "unrealized": round(unrealized, 2),
        "dividends": round(dividends, 2),
        "total": round(realized + unrealized + dividends, 2),
        "ledger_matches_broker": abs(ledger_qty - broker_qty) < 1e-4,
        "ledger_qty": round(ledger_qty, 4), "trades": len(rows),
    }
