"""Test guards for MeansRev.

Its jobs run through the shared harness, which records "this job completed
today" so the daily and startup triggers cannot both do the work. A test run
would otherwise write that record for real — and mark real jobs as done, making
the scheduler skip them. That is the worst version of this mistake: it stops a
bot trading, silently, which is exactly what this project exists to prevent.
"""

import pytest

from quantcore.testing import redirect_live_state


@pytest.fixture(autouse=True)
def _no_live_state(monkeypatch, tmp_path):
    redirect_live_state(monkeypatch, tmp_path)
