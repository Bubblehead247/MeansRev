"""The weekly trend gate must never liquidate the book on missing data (W2).

`config.USE_TREND_FILTER` is off today, so this is dormant — which is exactly why
it is worth pinning now. The trap is that the same default is read in opposite
directions by the two gates:

* entry — "not above the weekly SMA200" blocks the trade. Missing data is
  conservative, and that is correct.
* exit  — the identical value means "the trend has broken, get out".

So with the filter switched on, one failed weekly fetch queued an exit for every
held symbol at once: a whole-book liquidation caused by a network error rather
than by anything the market did.
"""

from __future__ import annotations

import pandas as pd
import pytest

import config
import meansrev_main as main
import scanner


@pytest.fixture
def trend_filter_on(monkeypatch):
    monkeypatch.setattr(config, "USE_TREND_FILTER", True)


def _daily_bars(n=300, price=100.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": price, "high": price * 1.01, "low": price * 0.99,
         "close": price, "volume": 1_000_000.0},
        index=idx,
    )


def _weekly_bars(n, price=100.0):
    idx = pd.date_range("2020-01-06", periods=n, freq="W")
    return pd.DataFrame(
        {"open": price, "high": price * 1.01, "low": price * 0.99,
         "close": price, "volume": 1_000_000.0},
        index=idx,
    )


# --- the source of the problem ---------------------------------------------


def test_a_failed_weekly_fetch_does_not_signal_an_exit(monkeypatch, trend_filter_on):
    """A network error is not a trend break."""
    monkeypatch.setattr(scanner, "fetch_bars", lambda symbol: _daily_bars())
    monkeypatch.setattr(scanner, "fetch_weekly_bars",
                        lambda symbol: (_ for _ in ()).throw(RuntimeError("alpaca down")))

    result = scanner.evaluate_symbol("SPY")

    assert result["weekly_data_ok"] is False
    assert result["weekly_exit_signal"] is False, (
        "a failed weekly fetch queued an exit — this is the whole-book "
        "liquidation path")


def test_too_few_weekly_bars_does_not_signal_an_exit(monkeypatch, trend_filter_on):
    """The other way the weekly figures end up as sentinels rather than data."""
    monkeypatch.setattr(scanner, "fetch_bars", lambda symbol: _daily_bars())
    monkeypatch.setattr(scanner, "fetch_weekly_bars", lambda symbol: _weekly_bars(20))

    result = scanner.evaluate_symbol("SPY")

    assert result["weekly_data_ok"] is False
    assert result["weekly_exit_signal"] is False


def test_a_real_trend_break_still_exits(monkeypatch, trend_filter_on):
    """The guard must not disable the gate it is protecting."""
    monkeypatch.setattr(scanner, "fetch_bars", lambda symbol: _daily_bars(price=50.0))
    # Weekly history far above today's daily close, so price sits below the SMA200.
    monkeypatch.setattr(scanner, "fetch_weekly_bars",
                        lambda symbol: _weekly_bars(260, price=100.0))

    result = scanner.evaluate_symbol("SPY")

    assert result["weekly_data_ok"] is True
    assert result["weekly_exit_signal"] is True


def test_missing_weekly_data_still_blocks_entry(monkeypatch, trend_filter_on):
    """Missing data stays conservative for entries — that direction was right."""
    monkeypatch.setattr(scanner, "fetch_bars", lambda symbol: _daily_bars())
    monkeypatch.setattr(scanner, "fetch_weekly_bars",
                        lambda symbol: (_ for _ in ()).throw(RuntimeError("alpaca down")))

    assert scanner.evaluate_symbol("SPY")["entry_signal"] is False


# --- the second line of defence ---------------------------------------------


@pytest.fixture
def mass_exit_alerts(monkeypatch):
    """Capture whole-book alerts, and fail loudly on the wrong alert type."""
    sent = []
    monkeypatch.setattr(main.notifier, "send_mass_exit_queued",
                        lambda count, reasons: sent.append((count, reasons)))
    monkeypatch.setattr(main.notifier, "send_review_needed", _must_not_be_called)
    return sent


def _must_not_be_called(*args, **kwargs):
    raise AssertionError(
        "send_review_needed says the position is gone from Alpaca with no booked "
        "trade. Nothing has been sold when the scan queues an exit, so this alert "
        "must not be reused here — that is the 2026-08-03 false alarm.")


def test_closing_the_whole_book_raises_an_alarm(monkeypatch, mass_exit_alerts):
    """Whatever the reason, one scan closing every position is worth being told."""
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "SPY", "action": "EXIT", "reason": "weekly_trend_break"},
        {"symbol": "QQQ", "action": "EXIT", "reason": "weekly_trend_break"},
        {"symbol": "IWM", "action": "EXIT", "reason": "weekly_trend_break"},
    ])

    main._check_for_mass_exit({"SPY": object(), "QQQ": object(), "IWM": object()})

    assert mass_exit_alerts == [(3, ["weekly_trend_break"])]


def test_a_partial_exit_is_not_an_alarm(monkeypatch, mass_exit_alerts):
    """Ordinary days close some positions; the alert must stay meaningful."""
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "SPY", "action": "EXIT", "reason": "rsi_exit"},
    ])

    main._check_for_mass_exit({"SPY": object(), "QQQ": object(), "IWM": object()})

    assert mass_exit_alerts == []


def test_a_two_name_book_emptying_is_not_an_alarm(monkeypatch, mass_exit_alerts):
    """The 2026-08-03 alert. Two positions hitting a 7-day stop together is routine.

    It fired then, said the positions were gone from Alpaca when both were still
    open, and read as an emergency. Below the floor there is now no alert at all.
    """
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "QQQ", "action": "EXIT", "reason": "time_stop"},
        {"symbol": "XLF", "action": "EXIT", "reason": "time_stop"},
    ])

    main._check_for_mass_exit({"QQQ": object(), "XLF": object()})

    assert mass_exit_alerts == []


def test_a_single_position_book_is_not_an_alarm(monkeypatch):
    """Holding one position and closing it is a normal Tuesday."""
    sent = []
    monkeypatch.setattr(main.notifier, "send_review_needed",
                        lambda symbol, reason: sent.append((symbol, reason)))
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "SPY", "action": "EXIT", "reason": "rsi_exit"},
    ])

    main._check_for_mass_exit({"SPY": object()})

    assert sent == []
