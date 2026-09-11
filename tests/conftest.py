"""Test guards for MeansRev.

Its jobs run through the shared harness, which records "this job completed
today" so the daily and startup triggers cannot both do the work. A test run
would otherwise write that record for real — and mark real jobs as done, making
the scheduler skip them. That is the worst version of this mistake: it stops a
bot trading, silently, which is exactly what this project exists to prevent.

`redirect_live_state` covers the files quantcore owns under ``~/.quantcore``.
MeansRev's own runtime files sit in the project directory and are not covered by
it, so they are redirected here. That gap was not theoretical: adding
``queue_log.jsonl`` without this fixture, the suite appended four real
``submitted`` receipts to the live log on the first run. Those receipts mean "the
order went out", so a test run could have silenced a genuine alert about an order
that never did.
"""

from pathlib import Path

import pytest

import meansrev_main as main
import position_tracker as pt
import trade_log
from quantcore.testing import redirect_live_state

#: Every ``Path`` on the module as imported, captured before any test redirects
#: them. ``unguarded_project_paths`` compares against this rather than the live
#: attributes, which by then point at a temp directory.
_ORIGINAL_PATHS: dict[str, Path] = {
    name: value
    for name in dir(main)
    if isinstance(value := getattr(main, name, None), Path)
}

#: MeansRev's own runtime state, anchored to the project directory. A file that
#: the bot writes at runtime belongs here on the commit that introduces it.
PROJECT_STATE_PATHS: tuple[tuple[str, str], ...] = (
    ("PENDING_FILE", "pending.json"),
    ("QUEUE_LOG_FILE", "queue_log.jsonl"),
    ("LOG_FILE", "bot.log"),
)


@pytest.fixture(autouse=True)
def _no_live_state(monkeypatch, tmp_path):
    redirect_live_state(monkeypatch, tmp_path)
    for attribute, filename in PROJECT_STATE_PATHS:
        monkeypatch.setattr(main, attribute, tmp_path / filename)


def unguarded_project_paths() -> list[str]:
    """Runtime files in the project directory that the fixture does not redirect.

    A test asserting this is empty catches a new state file the moment it is
    added, rather than after a suite has already written to something real.
    """
    project_dir = Path(main.__file__).resolve().parent
    guarded = {attribute for attribute, _ in PROJECT_STATE_PATHS}
    return [
        f"meansrev_main.{name} -> {value}"
        for name, value in _ORIGINAL_PATHS.items()
        if value.parent == project_dir and name not in guarded
    ]


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the tracker and the CSV at a temp directory."""
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(trade_log, "CSV_FILE", tmp_path / "trades.csv")
    return tmp_path
