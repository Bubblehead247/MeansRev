"""Tests for how scanner.py shapes its bar data (2026-09-27 data fixes).

* Weekly bars must hold completed weeks only, like the backtests' W-FRI
  resample; Alpaca's own weekly bars include the unfinished week, which moved
  the live weekly ADX off the backtested one on a 5-point regime band.
* A misplaced-decimal print (SPY 2026-02-02, low 68.47 on a ~$685 day) must be
  clipped, or it inflates ATR and the weekly ADX for months.
"""

import pandas as pd
import pytest

import scanner


def _daily(dates, closes):
    idx = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize("UTC") + pd.Timedelta(hours=4)
    return pd.DataFrame({"open": closes, "high": [c + 1 for c in closes],
                         "low": [c - 1 for c in closes], "close": closes,
                         "volume": [100] * len(closes)}, index=idx)


def test_an_unfinished_week_is_dropped():
    # Mon 9/14 .. Fri 9/18 is complete; Mon 9/21 .. Wed 9/23 is still in progress.
    days = pd.bdate_range("2026-09-14", "2026-09-23")
    weekly = scanner.completed_weeks(_daily(days, list(range(10, 10 + len(days)))))
    assert list(weekly.index.date.astype(str)) == ["2026-09-18"]
    assert weekly["close"].iloc[-1] == 14
    assert weekly["high"].iloc[-1] == 15 and weekly["low"].iloc[-1] == 9


def test_the_week_counts_once_friday_is_in():
    days = pd.bdate_range("2026-09-14", "2026-09-25")
    weekly = scanner.completed_weeks(_daily(days, list(range(10, 10 + len(days)))))
    assert list(weekly.index.date.astype(str)) == ["2026-09-18", "2026-09-25"]


def test_a_misplaced_decimal_low_is_clipped():
    bars = pd.DataFrame({"open": [684.2, 690.0], "high": [691.5, 695.0],
                         "low": [68.47, 685.0], "close": [689.99, 692.0]},
                        index=pd.to_datetime(["2026-02-02", "2026-02-03"]))
    clean = scanner._clip_bad_prints("SPY", bars)
    assert clean["low"].tolist() == [pytest.approx(684.2), 685.0]
    assert clean["high"].tolist() == [691.5, 695.0]


def test_a_real_40_percent_flash_crash_low_is_kept():
    bars = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [60.0], "close": [98.0]},
                        index=pd.to_datetime(["2015-08-24"]))
    assert scanner._clip_bad_prints("RSP", bars)["low"].iloc[0] == 60.0


def test_stale_bars_switch_signals_off_but_keep_the_result(monkeypatch):
    """Today's bar missing: no entry/exit signal, but not an error (time stops still run)."""
    from datetime import date, timedelta

    yesterday = date.today() - timedelta(days=1)
    monkeypatch.setattr(scanner.config, "SYMBOLS", ["XLF"])
    monkeypatch.setattr(scanner, "evaluate_symbol", lambda s: {
        "symbol": s, "bar_date": yesterday.isoformat(), "close": 54.0,
        "entry_signal": True, "exit_signal": True, "weekly_exit_signal": False,
    })
    result = scanner.run_scan()["XLF"]
    assert not result["entry_signal"] and not result["exit_signal"]
    assert "error" not in result and result["stale_bars"]
    assert result["close"] == 54.0


def test_todays_bar_passes_through(monkeypatch):
    from datetime import datetime

    today = datetime.now(scanner._ET).date()
    monkeypatch.setattr(scanner.config, "SYMBOLS", ["XLF"])
    monkeypatch.setattr(scanner, "evaluate_symbol", lambda s: {
        "symbol": s, "bar_date": today.isoformat(), "close": 54.0,
        "entry_signal": True, "exit_signal": False, "weekly_exit_signal": False,
    })
    assert scanner.run_scan()["XLF"]["entry_signal"]


def test_a_scan_result_survives_the_pending_queue_encoder():
    """Queued entries store the scan result in pending.json."""
    import json

    import meansrev_main

    bars = _daily(pd.bdate_range("2026-09-21", "2026-09-25"), [54.0] * 5)
    json.dumps({"bar_date": scanner._et_date(bars.index[-1])}, default=meansrev_main._json_scalar)
    assert scanner._et_date(bars.index[-1]) == "2026-09-25"
