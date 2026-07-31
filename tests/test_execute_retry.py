"""A retried execute must not resend orders it already sent (W1b).

The double-trigger case — daily and startup triggers both firing — is handled by
the once-a-day completion marker in `quantcore.jobs`. This is the case that
marker deliberately does not cover: a job that fails *partway*.

`pre_open_execute` used to submit every queued action, then clear the queue and
save. Fail after two of three orders and the queue was never cleared, no marker
was written (correctly — it failed), and `RestartCount 2` retried the job. The
retry reloaded the full queue and resent the orders that had already gone out.
`submit_entry` guards on `has_position()`, but an unfilled limit order creates no
position, so that guard does not catch it.
"""

from __future__ import annotations

import json

import pytest

import meansrev_main as main


@pytest.fixture
def queue(monkeypatch, tmp_path):
    """Point the pending file at a temp dir and start from a known queue."""
    monkeypatch.setattr(main, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(main, "_pending", [])
    monkeypatch.setattr(main, "_skip_if_closed", lambda label: False)
    return main


def _action(symbol, kind="EXIT"):
    return {"symbol": symbol, "action": kind, "reason": "rsi_exit", "close": 100.0}


def test_a_failure_partway_does_not_resend_the_earlier_orders(queue, monkeypatch):
    sent = []

    def flaky_exit(symbol, reason=None, close_price=0.0):
        sent.append(symbol)
        if symbol == "QQQ":
            raise RuntimeError("alpaca rejected the session")

    monkeypatch.setattr(main.executor, "submit_exit", flaky_exit)
    main._pending.extend([_action("SPY"), _action("QQQ"), _action("DIA")])
    main._save_pending(fresh=True)

    with pytest.raises(RuntimeError):
        main.pre_open_execute()

    assert sent == ["SPY", "QQQ"], "the run should stop at the failure"

    # The retry: reload from disk exactly as a fresh process would.
    monkeypatch.setattr(main, "_pending", [])
    main._load_pending()
    sent.clear()
    monkeypatch.setattr(main.executor, "submit_exit",
                        lambda symbol, reason=None, close_price=0.0: sent.append(symbol))

    main.pre_open_execute()

    assert "SPY" not in sent, "SPY was submitted twice — duplicate live order"
    assert sent == ["DIA"], "the retry should resume, not restart"


def test_a_clean_run_still_empties_the_queue(queue, monkeypatch):
    sent = []
    monkeypatch.setattr(main.executor, "submit_exit",
                        lambda symbol, reason=None, close_price=0.0: sent.append(symbol))
    main._pending.extend([_action("SPY"), _action("QQQ")])
    main._save_pending(fresh=True)

    main.pre_open_execute()

    assert sent == ["SPY", "QQQ"]
    assert main._pending == []
    assert json.loads(main.PENDING_FILE.read_text())["actions"] == []


def test_draining_the_queue_does_not_reset_the_staleness_clock(queue):
    """Otherwise a part-processed queue looks freshly made on every save.

    The queue is discarded after PENDING_MAX_AGE_DAYS because day-old signals
    are stale, and re-stamping it on each pop would defeat that.
    """
    main._pending.extend([_action("SPY"), _action("QQQ")])
    main._save_pending(fresh=True)
    stamped = json.loads(main.PENDING_FILE.read_text())["queued_at"]

    main._pending.pop(0)
    main._save_pending()

    assert json.loads(main.PENDING_FILE.read_text())["queued_at"] == stamped


def test_the_scan_does_stamp_a_new_queue(queue):
    """The scan is when the signals were actually produced."""
    main._pending.append(_action("SPY"))
    main._save_pending(fresh=True)
    first = json.loads(main.PENDING_FILE.read_text())["queued_at"]

    main._pending.append(_action("QQQ"))
    main._save_pending(fresh=True)

    assert json.loads(main.PENDING_FILE.read_text())["queued_at"] >= first
