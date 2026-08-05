"""The scan queues actions; the pre-open execute submits them. Nothing checked.

Those two run as separate processes an evening apart. On 2026-07-31 the scan
queued an XLF exit, and the 2026-08-03 execute logged "No pending actions" and
sent nothing. The position ran to 10 days against a 7-day time stop before
anyone noticed, and by then `pending.json` had been rewritten twice, so why the
queue was empty could no longer be established.

The daily books-vs-broker reconciler could never have caught it: it compares the
bot's records against Alpaca, and neither of them had the trade. A dropped
signal is invisible to a comparison that only looks at what did happen.

So these pin the receipts and the check that reads them.
"""

from __future__ import annotations

import json

import pytest

import meansrev_main as main
from conftest import unguarded_project_paths


@pytest.fixture(autouse=True)
def empty_queue(monkeypatch):
    """The receipt log and pending.json are redirected by the conftest fixture."""
    monkeypatch.setattr(main, "_pending", [])


def test_no_runtime_file_is_left_unguarded():
    """Adding a state file without redirecting it lets the suite write it for real.

    That happened when ``queue_log.jsonl`` was introduced: the first full run
    appended four ``submitted`` receipts to the live log. A receipt means "the
    order went out", so those entries could have suppressed a true alert about an
    order that never did — the suite quietly disarming the guard it was testing.
    """
    assert unguarded_project_paths() == []


@pytest.fixture
def alerts(monkeypatch):
    """Capture dropped-action alerts, and reject the wrong alert type."""
    sent = []
    monkeypatch.setattr(main.notifier, "send_dropped_action",
                        lambda symbol, action, queued_at: sent.append((symbol, action)))

    def _wrong(*args, **kwargs):
        raise AssertionError(
            "send_review_needed claims the position vanished from Alpaca. A "
            "dropped order means the opposite — the position is still there and "
            "the order never went out. Reusing it repeats the 2026-08-03 alert "
            "that said something untrue.")

    monkeypatch.setattr(main.notifier, "send_review_needed", _wrong)
    return sent


def _events():
    return [json.loads(line) for line in
            main.QUEUE_LOG_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


def test_a_queued_action_that_was_submitted_is_not_reported(alerts):
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])
    main._append_queue_event("submitted", symbol="XLF", action="EXIT")

    main._check_queue_was_drained()

    assert alerts == []


def test_a_queued_action_that_was_never_submitted_is_reported(alerts):
    """The 2026-08-03 failure, in miniature."""
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])
    main._append_queue_event("execute_empty")

    main._check_queue_was_drained()

    assert alerts == [("XLF", "EXIT")]


def test_only_the_unsubmitted_part_of_a_queue_is_reported(alerts):
    main._append_queue_event("queued", actions=[["XLF", "EXIT"], ["QQQ", "EXIT"]])
    main._append_queue_event("submitted", symbol="QQQ", action="EXIT")

    main._check_queue_was_drained()

    assert alerts == [("XLF", "EXIT")]


def test_a_superseded_queue_is_not_reported(alerts):
    """Each scan clears the queue, so an older one was replaced, not dropped."""
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])
    main._append_queue_event("queued", actions=[["QQQ", "EXIT"]])
    main._append_queue_event("submitted", symbol="QQQ", action="EXIT")

    main._check_queue_was_drained()

    assert alerts == []


def test_no_receipts_yet_reports_nothing(alerts):
    """A first run must not alert on a file that does not exist."""
    main._check_queue_was_drained()

    assert alerts == []


def test_a_scan_that_queued_nothing_reports_nothing(alerts):
    main._append_queue_event("queued", actions=[])
    main._append_queue_event("execute_empty")

    main._check_queue_was_drained()

    assert alerts == []


def test_an_unreadable_line_does_not_lose_the_rest(alerts):
    """A torn final line costs one receipt, not the whole log."""
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])
    with open(main.QUEUE_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"event": "submitted", "sym\n')

    main._check_queue_was_drained()

    assert alerts == [("XLF", "EXIT")]


def test_the_check_never_re_queues_the_dropped_action(alerts):
    """Alert only. A stale signal fired quietly is how a bug becomes a position."""
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])
    main._append_queue_event("execute_empty")

    main._check_queue_was_drained()

    assert main._pending == []


# --------------------------------------------------------------------------
# Writing the receipts
# --------------------------------------------------------------------------


def test_the_scan_records_what_it_queued(monkeypatch):
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "XLF", "action": "EXIT", "reason": "time_stop", "close": 57.37},
    ])

    main._save_pending(fresh=True)
    main._append_queue_event(
        "queued", actions=[[p["symbol"], p["action"]] for p in main._pending])

    assert _events()[-1]["actions"] == [["XLF", "EXIT"]]


def test_an_empty_execute_is_recorded(monkeypatch):
    """The line that would have made the 2026-08-03 diagnosis immediate.

    "No pending actions" in the log looks identical whether the scan queued
    nothing or the queue was lost. Only a receipt tells them apart.
    """
    monkeypatch.setattr(main, "_skip_if_closed", lambda name: False)

    main.pre_open_execute()

    assert [e["event"] for e in _events()] == ["execute_empty"]


def test_a_submitted_order_is_recorded(monkeypatch):
    monkeypatch.setattr(main, "_skip_if_closed", lambda name: False)
    monkeypatch.setattr(main.executor, "submit_exit", lambda *a, **k: None)
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "XLF", "action": "EXIT", "reason": "time_stop", "close": 57.37},
    ])

    main.pre_open_execute()

    assert _events() == [
        {**_events()[0], "event": "submitted", "symbol": "XLF", "action": "EXIT"}]


def test_a_failed_submit_records_no_receipt(monkeypatch, alerts):
    """The receipt is written after the order goes out, and that ordering matters.

    Recording before the submit would mark an order as sent when it never was,
    which hides exactly the failure this log exists to catch. Recording after
    costs a false alarm on a crash in between, which is cheap and self-resolving.
    """
    monkeypatch.setattr(main, "_skip_if_closed", lambda name: False)

    def _boom(*args, **kwargs):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(main.executor, "submit_exit", _boom)
    monkeypatch.setattr(main, "_pending", [
        {"symbol": "XLF", "action": "EXIT", "reason": "time_stop", "close": 57.37},
    ])
    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])

    with pytest.raises(RuntimeError):
        main.pre_open_execute()

    assert not any(e["event"] == "submitted" for e in _events())

    main._check_queue_was_drained()
    assert alerts == [("XLF", "EXIT")], "a submit that raised must not look sent"


def test_recording_never_breaks_the_bot(monkeypatch, tmp_path):
    """Bookkeeping must not be able to stop a trade going out."""
    monkeypatch.setattr(main, "QUEUE_LOG_FILE", tmp_path / "nope" / "queue_log.jsonl")

    main._append_queue_event("queued", actions=[["XLF", "EXIT"]])  # must not raise

    assert main._read_queue_events() == []


# --------------------------------------------------------------------------
# Where the check runs
# --------------------------------------------------------------------------


def test_the_first_confirm_pass_runs_the_check(monkeypatch):
    ran = []
    monkeypatch.setattr(main, "_skip_if_closed", lambda name: False)
    monkeypatch.setattr(main, "_check_queue_was_drained", lambda: ran.append(True))
    monkeypatch.setattr(main.executor, "confirm_fills_and_place_stops", lambda **k: None)
    monkeypatch.setattr(main.executor, "confirm_exit_fills", lambda: None)

    main.fill_confirm()

    assert ran == [True]


def test_the_final_confirm_pass_does_not_repeat_the_check(monkeypatch):
    """Both passes run before the next scan, so the second would re-send the alert."""
    ran = []
    monkeypatch.setattr(main, "_skip_if_closed", lambda name: False)
    monkeypatch.setattr(main, "_check_queue_was_drained", lambda: ran.append(True))
    monkeypatch.setattr(main.executor, "confirm_fills_and_place_stops", lambda **k: None)
    monkeypatch.setattr(main.executor, "confirm_exit_fills", lambda: None)

    main.fill_confirm(final=True)

    assert ran == []
