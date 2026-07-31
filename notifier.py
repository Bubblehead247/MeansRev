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
    """
    _push(
        f"review ({symbol})",
        f"⚠️ NEEDS REVIEW: {symbol}",
        f"{symbol} is gone from Alpaca but no closed trade was booked. {reason} "
        f"Nothing was written to trades.csv — check the broker and settle it by hand.",
        5, ["warning"],
    )


def send_error(detail: str):
    """Push alert when a scheduled job fails and the bot enters retry mode."""
    _push(
        "error",
        "❌ BOT JOB FAILED",
        f"A scheduled job raised an error — retrying every 30s. {detail}",
        4, ["x"],
    )


def send_signal(symbol: str, rsi_value: float):
    """Fire a high-priority push alert when a live entry signal fires."""
    _push(
        f"signal ({symbol})",
        f"\U0001f6a8 SIGNAL: {symbol}",
        f"{symbol} RSI(2) = {rsi_value:.1f} — ENTRY SIGNAL FIRED. "
        f"Bot has queued a trade.",
        5, ["rotating_light"],
    )
