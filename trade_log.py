"""
trade_log.py — Appends one row per closed trade to trades.csv.

Called from executor.confirm_exit_fills() (signal/time exits) and
main.post_close_scan() (hard stop exits). Headers are written automatically
on first use.
"""
import csv
import logging
from datetime import date
from pathlib import Path

logger   = logging.getLogger(__name__)

# Anchored to this file, not the working directory. Launched from anywhere else,
# a relative path silently starts a second, empty ledger — and eval_checkpoint.py
# already resolves trades.csv absolutely, so the writer and the trade-count gate
# would end up reading different files.
CSV_FILE = Path(__file__).resolve().parent / "trades.csv"

HEADERS = [
    "symbol", "entry_date", "entry_price", "shares",
    "stop_price", "stop_mult",
    "rsi2_at_signal", "sma200_weekly_at_signal", "sma50_daily_at_signal", "atr14_at_signal",
    "exit_date", "exit_price", "exit_reason",
    "days_held", "realized_pnl", "realized_pnl_pct",
    # Shadow record of the disabled ADX regime filter (config.USE_REGIME_FILTER)
    # — captured on every real entry so win rate/PF can be split by "would have
    # passed the band" without ever gating live entries on it. See scanner.py's
    # regime_band_ok and position_tracker.add.
    "weekly_adx_at_signal", "regime_band_ok_at_signal",
]


def log_closed_trade(pos_data: dict, exit_price: float, exit_reason: str,
                     shares: float | None = None):
    """
    Append a closed-trade row to trades.csv.

    Args:
        pos_data:    Position dict from position_tracker.
        exit_price:  Actual fill price of the closing order. When the exit filled
                     in more than one piece this must be the quantity-weighted
                     blend of those fills, never one of them.
        exit_reason: One of: rsi_exit, weekly_trend_break, time_stop, hard_stop,
                     exit_pending_retry, or *_fallback variants.
        shares:      Shares actually closed, when it differs from the tracked
                     count. Pass this whenever the broker is the authority: a DIA
                     exit was booked as 40 shares at one price when it really
                     filled 21 and then 19 at different prices.
    """
    entry_price = pos_data["entry_price"]
    shares      = pos_data["shares"] if shares is None else shares
    entry_date  = date.fromisoformat(pos_data["entry_date"])
    exit_date   = date.today()

    pnl     = round((exit_price - entry_price) * shares, 2)
    pnl_pct = round((exit_price / entry_price - 1) * 100, 3) if entry_price else 0.0

    row = {
        "symbol":                  pos_data["symbol"],
        "entry_date":              entry_date.isoformat(),
        "entry_price":             round(entry_price, 4),
        "shares":                  shares,
        "stop_price":              pos_data.get("stop_price", ""),
        "stop_mult":               pos_data.get("stop_mult", ""),
        "rsi2_at_signal":          pos_data.get("rsi2_at_signal", ""),
        "sma200_weekly_at_signal": pos_data.get("sma200_weekly_at_signal", ""),
        "sma50_daily_at_signal":   pos_data.get("sma50_daily_at_signal", ""),
        "atr14_at_signal":         pos_data.get("atr14_at_signal", ""),
        "exit_date":               exit_date.isoformat(),
        "exit_price":              round(exit_price, 4),
        "exit_reason":             exit_reason,
        "days_held":               (exit_date - entry_date).days,
        "realized_pnl":            pnl,
        "realized_pnl_pct":        pnl_pct,
        "weekly_adx_at_signal":    pos_data.get("weekly_adx_at_signal", ""),
        "regime_band_ok_at_signal": pos_data.get("regime_band_ok_at_signal", ""),
    }

    write_header = not CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    logger.info(
        f"📊 Trade logged | {row['symbol']} | "
        f"Entry=${entry_price:.2f} Exit=${exit_price:.2f} | "
        f"P&L=${pnl:+.2f} ({pnl_pct:+.3f}%) | "
        f"Reason={exit_reason} | Days={row['days_held']}"
    )
