"""
position_tracker.py — JSON-based persistence for open position metadata.

Alpaca tracks shares, cost basis, and P&L. We track what Alpaca doesn't:
  - Entry date (for the 7-day time stop)
  - Which stop multiplier variant was used
  - The stop order ID (so we can cancel it before placing a market exit)
  - Both stop price variants (for performance comparison logging)
"""
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import config

logger     = logging.getLogger(__name__)

# Anchored to this file, not the working directory, matching the pattern meansrev_main.py
# already uses for pending.json. A relative path meant launching from any other
# directory read an empty tracker and re-entered every position.
STATE_FILE = Path(__file__).resolve().parent / "positions.json"


# ── I/O Helpers ───────────────────────────────────────────────────────────────

def _load() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save(data: dict):
    """Write the position book atomically.

    A plain write truncates the file before filling it, so a crash in between
    leaves a truncated one — and this file is what says which positions are open
    and where their stops are. See ``quantcore.statefile``.
    """
    from quantcore.statefile import write_json_atomic

    write_json_atomic(STATE_FILE, data, default=str)


# ── Public Interface ──────────────────────────────────────────────────────────

def add(
    symbol: str,
    entry_price: float,
    stop_price: float,
    stop_price_a: float,
    stop_price_b: float,
    shares: int,
    stop_mult: float,
    stop_order_id: str = None,
    rsi2: float = None,
    sma200_weekly: float = None,
    sma50_daily: float = None,
    atr14: float = None,
    weekly_adx: float = None,
    regime_ok: bool = None,
):
    """Record a new open position."""
    data = _load()
    data[symbol] = {
        "symbol":                  symbol,
        "entry_price":             entry_price,
        "stop_price":              stop_price,      # Active stop (the one on exchange)
        "stop_price_a":            stop_price_a,    # 1.5×ATR — tracked for comparison
        "stop_price_b":            stop_price_b,    # 2.5×ATR — tracked for comparison
        "shares":                  shares,
        "stop_mult":               stop_mult,
        "stop_order_id":           stop_order_id,
        "exit_status":             "open",          # "open" | "exit_pending" | "needs_review"
        "rsi2_at_signal":          rsi2,
        "sma200_weekly_at_signal": sma200_weekly,
        "sma50_daily_at_signal":   sma50_daily,
        "atr14_at_signal":         atr14,
        # Shadow record of the disabled ADX regime filter (config.USE_REGIME_FILTER
        # is OFF live) — captured on every real entry so trades.csv can be split
        # into "would have passed the band" vs. not, without ever gating on it.
        "weekly_adx_at_signal":    weekly_adx,
        "regime_band_ok_at_signal": regime_ok,
        "entry_date":              date.today().isoformat(),
        # Timezone-aware UTC. datetime.utcnow() is deprecated in 3.12+ and
        # produced a naive timestamp that read as local time to anything parsing it.
        "opened_at":               datetime.now(timezone.utc).isoformat(),
    }
    _save(data)
    logger.info(
        f"Position recorded: {symbol} | {shares} shares @ ${entry_price:.2f} | "
        f"Stop=${stop_price:.2f} ({stop_mult}×ATR)"
    )


def update_stop_order_id(symbol: str, stop_order_id: str):
    """Update the stop order ID after the hard stop is placed on exchange."""
    data = _load()
    if symbol in data:
        data[symbol]["stop_order_id"] = stop_order_id
        _save(data)
        logger.info(f"Stop order ID updated for {symbol}: {stop_order_id}")


def mark_exit_pending(symbol: str, reason: str = "signal_exit"):
    """Mark a position as having an exit order submitted but not yet confirmed filled."""
    data = _load()
    if symbol in data:
        data[symbol]["exit_status"] = "exit_pending"
        data[symbol]["exit_reason"] = reason
        _save(data)
        logger.info(f"Position marked exit_pending: {symbol} | reason={reason}")


def get_exit_pending_symbols() -> list[str]:
    """Return all symbols currently awaiting exit fill confirmation."""
    return [sym for sym, pos in _load().items() if pos.get("exit_status") == "exit_pending"]


def mark_needs_review(symbol: str, reason: str = ""):
    """Flag a position whose fate cannot be determined from the evidence.

    Used when a tracked position is gone from Alpaca but there is no proof of
    what happened to it. The record is deliberately kept rather than deleted, so
    the position stays visible to the daily reconciler instead of disappearing
    with a guessed price. The status change stops it being re-examined (and
    re-alerted) every session.
    """
    data = _load()
    if symbol in data:
        data[symbol]["exit_status"] = "needs_review"
        data[symbol]["review_reason"] = reason
        _save(data)
        logger.error(f"Position needs review: {symbol} | {reason}")


def get_needs_review_symbols() -> list[str]:
    """Return all symbols flagged for manual review."""
    return [sym for sym, pos in _load().items() if pos.get("exit_status") == "needs_review"]


def update_entry_price(symbol: str, actual_fill_price: float):
    """Update entry price to actual fill (vs. estimated previous close)."""
    data = _load()
    if symbol in data:
        data[symbol]["entry_price"] = actual_fill_price
        _save(data)
        logger.info(f"Entry price updated for {symbol}: ${actual_fill_price:.2f} (actual fill)")


def update_stop_price(symbol: str, actual_stop_price: float):
    """Update the tracked stop to the price actually resting at the exchange.

    Without this the tracker keeps the *estimated* stop worked out before the
    fill, while the exchange holds a different number. QQQ's real stop was
    $653.21 while positions.json, the daily status message and trades.csv all
    said $649.11.
    """
    data = _load()
    if symbol in data:
        data[symbol]["stop_price"] = actual_stop_price
        _save(data)
        logger.info(f"Stop price updated for {symbol}: ${actual_stop_price:.2f} (actual stop placed)")


def update_shares(symbol: str, actual_shares: int):
    """Update the tracked share count to what the broker actually filled."""
    data = _load()
    if symbol in data and data[symbol].get("shares") != actual_shares:
        previous = data[symbol].get("shares")
        data[symbol]["shares"] = actual_shares
        _save(data)
        logger.info(f"Share count updated for {symbol}: {previous} → {actual_shares} (actual fill)")


def remove(symbol: str):
    """Remove a position after it's been closed."""
    data = _load()
    if symbol in data:
        pos = data.pop(symbol)
        _save(data)
        logger.info(
            f"Position removed: {symbol} | "
            f"Was: {pos['shares']} shares @ ${pos['entry_price']:.2f}, "
            f"entered {pos['entry_date']}"
        )


def get(symbol: str) -> dict | None:
    return _load().get(symbol)


def all_positions() -> dict:
    return _load()


def get_stop_order_id(symbol: str) -> str | None:
    pos = get(symbol)
    return pos.get("stop_order_id") if pos else None


def days_open(symbol: str) -> int:
    """Returns calendar days since entry."""
    pos = get(symbol)
    if not pos:
        return 0
    entry = date.fromisoformat(pos["entry_date"])
    return (date.today() - entry).days


def time_stop_triggered(symbol: str) -> bool:
    return days_open(symbol) >= config.MAX_HOLD_DAYS


def get_time_stop_symbols() -> list[str]:
    """Return all symbols whose positions have exceeded the max hold period."""
    expired = [sym for sym in _load() if time_stop_triggered(sym)]
    for sym in expired:
        logger.info(f"⏰ Time stop: {sym} has been open {days_open(sym)} days (max={config.MAX_HOLD_DAYS})")
    return expired
