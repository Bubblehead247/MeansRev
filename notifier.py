"""
notifier.py — ntfy.sh push alert integration for the Mean Reversion Bot.

Two alert tiers:
  ⚠️  WARNING  RSI(2) ≤ 20 — approaching entry zone, heads-up only
  🚨  SIGNAL   RSI(2) ≤ 10 — entry signal fired, trade queued for next open

Alerts are fire-and-forget. If ntfy.sh is unreachable the bot continues normally.
"""
import logging

import requests

import config

logger = logging.getLogger(__name__)


def _push(what: str, title: str, message: str, priority: int, tags: list[str]):
    """Send one alert. Every alert goes through here, for two reasons.

    The topic is read at call time from ``config.NTFY_TOPIC``, which comes from
    the environment. With no topic set there is nowhere to publish, so alerts are
    off — checking that in one place means a new alert cannot forget to.

    Fire-and-forget: ntfy being unreachable must never interrupt trading.
    """
    if not config.NTFY_TOPIC:
        logger.debug(f"ntfy {what} alert skipped: no NTFY_TOPIC configured.")
        return
    try:
        requests.post(
            "https://ntfy.sh",
            json={
                "topic":    config.NTFY_TOPIC,
                "title":    title,
                "message":  message,
                "priority": priority,
                "tags":     tags,
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"ntfy {what} alert failed: {e}")


def send_warning(symbol: str, rsi_value: float):
    """Fire a push alert when RSI(2) enters the 10–20 warming zone."""
    _push(
        f"warning ({symbol})",
        f"⚠️ WARMING UP: {symbol}",
        f"{symbol} RSI(2) = {rsi_value:.1f} — approaching entry zone. Watch for ≤10.",
        3, ["chart_with_upwards_trend"],
    )


def send_checkpoint(new_trades: int, target: int):
    """One-time push when the paper-trading evaluation checkpoint is reached."""
    _push(
        "checkpoint",
        "\U0001f4ca EVAL CHECKPOINT",
        f"{new_trades} closed trades since v1.4 go-live (target {target}). "
        f"Review execution fidelity: fills/slippage, stops, signal match, "
        f"win~68%/hold~3-4d, drawdown <25%. Run: python eval_checkpoint.py",
        4, ["bar_chart"],
    )


def send_review_needed(symbol: str, reason: str):
    """Push alert when a position's fate cannot be determined from the evidence.

    High priority on purpose: this means the books and the broker disagree, and
    the bot has deliberately refused to guess a price to paper over it.

    The body states as fact that the position has vanished from Alpaca with no
    booked trade, so this must only be sent when that is actually true. It used
    to be reused for the whole-book exit warning below, which produced "ALL
    POSITIONS is gone from Alpaca but no closed trade was booked" on a day when
    nothing had been sold and both positions were sitting in the account. An
    alert that says something false is worse than no alert: the next true one
    gets read as another false alarm.
    """
    _push(
        f"review ({symbol})",
        f"⚠️ NEEDS REVIEW: {symbol}",
        f"{symbol} is gone from Alpaca but no closed trade was booked. {reason} "
        f"Nothing was written to trades.csv — check the broker and settle it by hand.",
        5, ["warning"],
    )


def send_mass_exit_queued(count: int, reasons: list[str]):
    """Heads-up when one scan queues an exit for every open position.

    Nothing has been sold at this point — the scan runs after the close and the
    orders go out at the next open — so this is information, not a fault. Kept
    below :func:`send_review_needed` in priority for that reason.
    """
    _push(
        "mass exit",
        "📕 WHOLE BOOK QUEUED TO EXIT",
        f"All {count} open positions are queued to exit at the next open. "
        f"Reason(s): {', '.join(reasons)}. Nothing has been sold yet — the "
        f"orders go out at 09:30 ET. Cancel by clearing pending.json before then.",
        4, ["books"],
    )


def send_dropped_action(symbol: str, action: str, queued_at: str):
    """Push alert when a queued action was never submitted.

    The scan queues actions and the pre-open execute submits them, in two
    separate processes an evening apart. On 2026-07-31 the scan queued an XLF
    exit and the 2026-08-03 execute found an empty queue; the position ran three
    days past its time stop and nothing reported it. The daily reconciler could
    not: it compares the books against the broker, and neither had the trade.

    High priority — an exit that never went out is money at risk.
    """
    _push(
        f"dropped ({symbol})",
        f"🚨 QUEUED ORDER NEVER SENT: {symbol}",
        f"The scan queued {action} {symbol} at {queued_at}, but the pre-open "
        f"execute did not submit it. The position is still open and unmanaged by "
        f"this signal. Check Alpaca and submit by hand if it still applies.",
        5, ["rotating_light"],
    )


def send_error(detail: str):
    """Push alert when a scheduled job fails and the bot enters retry mode."""
    _push(
        "error",
        "❌ BOT JOB FAILED",
        f"A scheduled job raised an error — retrying every 30s. {detail}",
        4, ["x"],
    )


def send_daily_status(snapshot: dict, positions: dict, sweep: dict | None = None):
    """Push the daily account summary: equity, P&L, every open position, and
    what the T-bill cash sweep has earned (``cash_sweep.report()``) when active.

    Routine, not urgent — priority 3, same tier as :func:`send_warning` and
    :func:`send_checkpoint`. ``[PAPER]`` is prefixed to the title whenever
    ``config.PAPER`` is True, so a paper push can never be mistaken for a live
    account's numbers.
    """
    equity      = snapshot["equity"]
    last_equity = snapshot["last_equity"]
    base_value  = snapshot["base_value"]
    asof        = snapshot["base_value_asof"]

    daily_pl     = equity - last_equity
    daily_pl_pct = (daily_pl / last_equity * 100) if last_equity else 0.0
    total_pl     = equity - base_value
    total_pl_pct = (total_pl / base_value * 100) if base_value else 0.0

    lines = [
        f"Equity: ${equity:,.2f}",
        f"Today: {daily_pl:+,.2f} ({daily_pl_pct:+.2f}%)",
        f"Total: {total_pl:+,.2f} ({total_pl_pct:+.2f}%) since {asof}",
        "",
        "Open positions:",
    ]
    if not positions:
        lines.append("(none)")
    else:
        for symbol, pos in positions.items():
            lines.append(
                f"{symbol} | {pos.qty} sh @ ${float(pos.avg_entry_price):.2f} | "
                f"now ${float(pos.current_price):.2f} | "
                f"{float(pos.unrealized_pl):+,.2f} "
                f"({float(pos.unrealized_plpc) * 100:+.2f}%)"
            )
    if sweep and sweep.get("trades"):
        lines += [
            "",
            f"Cash sweep ({config.CASH_SWEEP_SYMBOL}): ${sweep['holding_value']:,.2f} held | "
            f"earned {sweep['total']:+,.2f} since {sweep['since']} "
            f"(price {sweep['realized'] + sweep['unrealized']:+,.2f}, "
            f"dividends {sweep['dividends']:+,.2f})"
            + ("" if sweep["ledger_matches_broker"] else " | LEDGER MISMATCH"),
        ]

    title = f"{'[PAPER] ' if config.PAPER else ''}\U0001f4ca DAILY STATUS"
    _push("daily status", title, "\n".join(lines), 3, ["bar_chart"])


def send_signal(symbol: str, rsi_value: float):
    """Fire a high-priority push alert when a live entry signal fires."""
    _push(
        f"signal ({symbol})",
        f"\U0001f6a8 SIGNAL: {symbol}",
        f"{symbol} RSI(2) = {rsi_value:.1f} — ENTRY SIGNAL FIRED. "
        f"Bot has queued a trade.",
        5, ["rotating_light"],
    )
