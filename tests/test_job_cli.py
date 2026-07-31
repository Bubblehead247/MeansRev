"""Tests for running MeansRev's jobs one at a time.

Each job is about to be fired by Windows Task Scheduler as its own short-lived
process, instead of depending on a long-running loop being alive at the right
minute. That dependence is what cost the XLV trade: the machine rebooted at
08:47:52 on 2026-07-15, MeansRev came back at 08:48:31 — past its 08:45 slot —
and the `schedule` library moved the job to the next day rather than running it.
No fill confirmation, no stop placed.

The properties worth guarding are the ones that make a scheduled run safe:
a wrong exit code either hides a failure or retries something already done, and a
lateness window that is too generous re-submits an order into an auction that has
already happened.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

import meansrev_main as main
from quantcore.jobs import EXIT_FAILED, EXIT_OK


@pytest.fixture(autouse=True)
def _open_market(monkeypatch):
    """Treat every test day as a normal trading day unless overridden."""
    from quantcore import market_calendar as mc

    monkeypatch.setattr(
        mc, "trading_session",
        lambda bot, day=None, **kw: mc.Session(
            day=day or date(2026, 7, 30), open=time(9, 30), close=time(16, 0)),
    )


@pytest.fixture(autouse=True)
def _no_alerts(monkeypatch):
    monkeypatch.setattr(main, "_load_pending", lambda: None)
    from quantcore import jobs
    monkeypatch.setattr(jobs, "_alert_failure", lambda *a, **k: None)


# --------------------------------------------------------------------------
# The jobs are all reachable, and named after what they do
# --------------------------------------------------------------------------


def test_every_scheduled_job_has_a_cli_entry_point():
    """Every job the loop runs must be runnable standalone.

    ``confirm-final`` joined the list with the move to DAY limit entries: they
    stay live all session, so a second pass before the close places the stop on
    anything that filled late and cancels whatever did not fill.
    """
    assert set(main._JOB_SPECS) == {
        "scan", "execute", "confirm", "confirm-final", "status"}
    assert set(main._JOB_WORK) == set(main._JOB_SPECS)


def test_job_times_come_from_config_not_a_second_copy():
    """A schedule defined twice is a schedule that will drift."""
    import config

    assert main._JOB_SPECS["execute"].scheduled == time(*main._hhmm(config.EXECUTE_TIME))
    assert main._JOB_SPECS["confirm"].scheduled == time(*main._hhmm(config.FILL_CONFIRM_TIME))
    assert main._JOB_SPECS["scan"].scheduled == time(*main._hhmm(config.SCAN_TIME))


def test_each_job_locks_on_its_own_name():
    """Two different jobs must not block each other."""
    names = {spec.lock_name for spec in main._JOB_SPECS.values()}
    assert len(names) == len(main._JOB_SPECS)
    assert all(n.startswith("meansrev-") for n in names)


# --------------------------------------------------------------------------
# Lateness windows — the part that makes catch-up safe
# --------------------------------------------------------------------------


def test_the_order_submission_job_has_a_short_window():
    """A limit-on-open order cannot be placed once the auction has gone."""
    execute = main._JOB_SPECS["execute"]
    assert execute.max_lateness <= timedelta(minutes=30)


def test_the_fill_confirm_job_stays_useful_all_session():
    """Confirming a fill and placing its missing stop is worth doing late.

    This is the job that did not run on 2026-07-15. Had it run even hours late,
    it would have found the unfilled order and no phantom trade would exist.
    """
    confirm = main._JOB_SPECS["confirm"]
    assert confirm.max_lateness >= timedelta(hours=4)


def test_a_late_confirm_still_runs(monkeypatch):
    ran = []
    monkeypatch.setitem(main._JOB_WORK, "confirm", lambda: ran.append(True))
    from quantcore import jobs

    code = jobs.run_job(main._JOB_SPECS["confirm"], main._JOB_WORK["confirm"],
                        now=datetime(2026, 7, 30, 12, 0))
    assert code == EXIT_OK
    assert ran == [True], "the fill-confirm job refused to run late"


def test_a_late_execute_does_not_submit_orders(monkeypatch):
    ran = []
    from quantcore import jobs

    code = jobs.run_job(main._JOB_SPECS["execute"], lambda: ran.append(True),
                        now=datetime(2026, 7, 30, 11, 0))
    assert ran == [], "a stale run submitted opening-auction orders"
    assert code == EXIT_OK, "skipping is not a failure"


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------


def test_a_successful_job_exits_zero(monkeypatch):
    monkeypatch.setitem(main._JOB_WORK, "status", lambda: None)
    assert main.run_one_job("status", now=datetime(2026, 7, 30, 15, 30)) == EXIT_OK


def test_a_failing_job_exits_non_zero(monkeypatch):
    def _boom():
        raise RuntimeError("alpaca said no")

    monkeypatch.setitem(main._JOB_WORK, "status", _boom)
    assert main.run_one_job("status", now=datetime(2026, 7, 30, 15, 30)) == EXIT_FAILED


def test_a_closed_market_exits_zero(monkeypatch):
    """A holiday must not look like a broken bot, or alerts stop being read."""
    from quantcore import market_calendar as mc

    monkeypatch.setattr(mc, "trading_session", lambda bot, day=None, **kw: None)
    ran = []
    monkeypatch.setitem(main._JOB_WORK, "scan", lambda: ran.append(True))

    assert main.run_one_job("scan", now=datetime(2026, 7, 30, 15, 30)) == EXIT_OK
    assert ran == []


def test_the_pending_queue_is_restored_before_the_job_runs(monkeypatch):
    """execute consumes what scan queued, so the queue must load first."""
    order = []
    monkeypatch.setattr(main, "_load_pending", lambda: order.append("load"))
    monkeypatch.setitem(main._JOB_WORK, "execute", lambda: order.append("work"))

    main.run_one_job("execute", now=datetime(2026, 7, 30, 8, 25))
    assert order == ["load", "work"]


# --------------------------------------------------------------------------
# The loop still works, and now refuses to double up
# --------------------------------------------------------------------------


def test_the_legacy_loop_refuses_to_start_twice(monkeypatch):
    """MeansRev was the only bot with no single-instance lock."""
    from quantcore.single_instance import SingleInstance

    with SingleInstance("meansrev-scheduler") as held:
        assert held.acquired
        # A second main() must bail out rather than schedule a second set of jobs.
        assert main.main() == 1


# --------------------------------------------------------------------------
# The legacy loop and the scheduled tasks must not both do the work
# --------------------------------------------------------------------------


def test_the_loop_runs_jobs_through_the_shared_harness(monkeypatch):
    """Otherwise the loop and a Windows task would each submit the day's orders.

    The loop used to call post_close_scan / pre_open_execute directly, taking no
    lock and leaving no completion marker, so running it alongside the scheduled
    tasks would have doubled every order.
    """
    ran = []
    monkeypatch.setitem(main._JOB_WORK, "confirm", lambda: ran.append("work"))

    at_slot = datetime(2026, 7, 30, 8, 45)
    assert main._loop_job("confirm", now=at_slot) == EXIT_OK
    assert ran == ["work"]

    # A second call the same day - the task firing after the loop - stands down.
    assert main._loop_job("confirm", now=at_slot) == EXIT_OK
    assert ran == ["work"], "the loop and the task both did the work"


def test_the_loop_does_not_reload_the_pending_queue(monkeypatch):
    """_load_pending extends rather than replaces, so reloading duplicates."""
    loads = []
    monkeypatch.setattr(main, "_load_pending", lambda: loads.append(1))
    monkeypatch.setitem(main._JOB_WORK, "execute", lambda: None)

    main._loop_job("execute", now=datetime(2026, 7, 30, 8, 25))
    assert loads == [], "the loop reloaded a queue it already holds"


# --------------------------------------------------------------------------
# O5 — the ntfy topic is a credential, not a constant
# --------------------------------------------------------------------------


def test_the_ntfy_topic_is_not_hardcoded():
    """ntfy.sh is public and unauthenticated: the topic name IS the credential.

    It sat in config.py in plaintext until 2026-07-30, so anyone with the repo
    could read every trade alert and publish forged ones.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "config.py"
    text = source.read_text(encoding="utf-8")

    assert 'NTFY_TOPIC = "MeansRevRSI"' not in text
    assert 'os.getenv("NTFY_TOPIC"' in text


def test_no_topic_configured_sends_nothing(monkeypatch):
    """Better silent than falling back to a topic published in the repo."""
    import config
    import notifier

    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    sent = []
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: sent.append(1))

    notifier.send_signal("SPY", 8.0)
    notifier.send_error("boom")

    assert sent == [], "posted somewhere with no topic configured"


def test_the_env_file_beats_an_ambient_variable():
    """This machine has a user-level NTFY_TOPIC=WeeklyAI from another project.

    `load_dotenv()` does not override an existing OS variable, so without
    override=True that stray value silently captured MeansRev's alerts.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "config.py"
    assert "override=True" in source.read_text(encoding="utf-8")
