"""The morning execute scans a session whose evening scan was lost.

The 15:30 CT scan was lost on 2026-09-28 and 2026-10-14 (network outage longer
than the job's retries), so that evening's signals were never queued. The execute
now checks the receipt log and, if the last scan on record is older than the
previous session, scans that session's close before sending orders.
"""

import json
from datetime import date

import pytest

import meansrev_main as main
from quantcore.market_calendar import Session
from datetime import time

MON, TUE, WED = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
SESSIONS = [Session(day=d, open=time(9, 30), close=time(16, 0))
            for d in (date(2026, 10, 9), MON, TUE, WED, date(2026, 10, 15))]


@pytest.fixture
def scans(monkeypatch):
    """Record post_close_scan calls; receipts are redirected by conftest."""
    calls = []

    def fake_scan(as_of=None):
        calls.append(as_of)
        main._pending.append({"symbol": "QQQ", "action": "ENTRY"})
        main._append_queue_event("queued", actions=[["QQQ", "ENTRY"]],
                                 session=as_of.isoformat() if as_of else "x",
                                 catch_up=bool(as_of))

    monkeypatch.setattr(main, "post_close_scan", fake_scan)
    sent = []
    monkeypatch.setattr(main.notifier, "send_catch_up", lambda *a, **k: sent.append(a))
    main._pending.clear()
    yield calls, sent
    main._pending.clear()


def _queued(session, ts="2026-10-13T20:30:00+00:00"):
    main._append_queue_event("queued", actions=[], session=session)
    # _append_queue_event stamps now; rewrite the last line's ts when a test needs
    # a legacy receipt.


def test_no_receipt_at_all_triggers_a_catch_up(scans):
    calls, sent = scans
    assert main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS) is True
    assert calls == [TUE]
    assert sent and sent[0][1] is True


def test_scan_for_the_prior_session_means_nothing_to_do(scans):
    calls, _ = scans
    _queued(TUE.isoformat())
    assert main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS) is False
    assert calls == []


def test_older_scan_triggers_catch_up_for_the_prior_session_only(scans):
    calls, _ = scans
    _queued(MON.isoformat())
    assert main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS) is True
    assert calls == [TUE]


def test_catch_up_does_not_repeat_next_morning_for_the_session_it_covered(scans):
    """The catch-up's receipt names the session scanned, not the day it ran."""
    calls, _ = scans
    main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS)
    calls.clear()
    # Next morning: prior session is Wed; the last receipt says Tue -> missed again,
    # which is correct (Wednesday evening's scan really has not run).
    assert main.catch_up_scan_if_missed(today=date(2026, 10, 15), sessions=SESSIONS) is True
    assert calls == [WED]
    calls.clear()
    # And once Wednesday's is on record, nothing more.
    assert main.catch_up_scan_if_missed(today=date(2026, 10, 15), sessions=SESSIONS) is False


def test_weekend_gap_uses_the_last_session_not_yesterday(scans):
    calls, _ = scans
    fri, mon = date(2026, 10, 9), MON
    _queued(fri.isoformat())
    assert main.catch_up_scan_if_missed(today=mon, sessions=SESSIONS) is False
    assert calls == []


def test_legacy_receipt_without_session_uses_its_new_york_date(scans):
    calls, _ = scans
    with open(main.QUEUE_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-10-13T20:30:14+00:00", "event": "queued",
                             "actions": []}) + "\n")
    assert main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS) is False
    assert calls == []


def test_unknown_calendar_fails_closed(scans):
    calls, _ = scans
    assert main.catch_up_scan_if_missed(today=WED, sessions=[]) is False
    assert calls == []


def test_a_failed_catch_up_restores_the_queue_and_does_not_raise(monkeypatch, scans):
    calls, sent = scans
    main._pending.append({"symbol": "SPY", "action": "EXIT"})

    def boom(as_of=None):
        main._pending.clear()
        raise ConnectionError("dns")

    monkeypatch.setattr(main, "post_close_scan", boom)
    assert main.catch_up_scan_if_missed(today=WED, sessions=SESSIONS) is False
    assert main._pending == [{"symbol": "SPY", "action": "EXIT"}]
    assert sent and sent[0][1] is False


def test_execute_job_catches_up_before_sending(monkeypatch, scans):
    order = []
    monkeypatch.setattr(main, "catch_up_scan_if_missed", lambda: order.append("catch_up"))
    monkeypatch.setattr(main, "pre_open_execute", lambda: order.append("execute"))
    main._execute_job()
    assert order == ["catch_up", "execute"]
    assert main._JOB_WORK["execute"].__name__ == "<lambda>"


def test_scanner_accepts_the_missed_session_date(monkeypatch):
    """Without expected_date every bar would look stale on a morning run."""
    import scanner
    monkeypatch.setattr(scanner.config, "SYMBOLS", ["SPY"])
    monkeypatch.setattr(scanner, "evaluate_symbol",
                        lambda s: {"symbol": s, "bar_date": "2026-10-13", "entry_signal": True,
                                   "exit_signal": False, "weekly_exit_signal": False})
    assert scanner.run_scan(expected_date="2026-10-13")["SPY"].get("stale_bars") is None
    assert scanner.run_scan(expected_date="2026-10-14")["SPY"]["stale_bars"] is True


def test_time_stop_age_is_taken_as_of_the_scanned_session(monkeypatch):
    import position_tracker as pt
    monkeypatch.setattr(pt, "get", lambda s: {"entry_date": "2026-10-04"})
    assert pt.days_open("X", as_of=date(2026, 10, 13)) == 9
    assert pt.time_stop_triggered("X", as_of=date(2026, 10, 13)) is False
    assert pt.time_stop_triggered("X", as_of=date(2026, 10, 14)) is True


def test_a_forming_bar_dated_after_the_scanned_session_is_ignored(monkeypatch):
    """A pre-market fetch may return today's forming bar; the catch-up must not read it."""
    import pandas as pd
    import scanner
    idx = pd.to_datetime(["2026-10-12 04:00", "2026-10-13 04:00", "2026-10-14 04:00"]).tz_localize("UTC")
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=idx)
    monkeypatch.setattr(scanner, "_as_of_session", "2026-10-13")
    assert [d.date().isoformat() for d in scanner._drop_after_session(bars).index] == [
        "2026-10-12", "2026-10-13"]
    monkeypatch.setattr(scanner, "_as_of_session", None)
    assert len(scanner._drop_after_session(bars)) == 3


def test_run_scan_clears_the_as_of_session_afterwards(monkeypatch):
    import scanner
    monkeypatch.setattr(scanner.config, "SYMBOLS", [])
    scanner.run_scan(expected_date="2026-10-13")
    assert scanner._as_of_session is None
