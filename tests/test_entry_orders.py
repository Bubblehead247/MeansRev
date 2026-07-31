"""Tests for how entry orders are placed and resolved (bot-review D5).

The change these guard: entries used to be **Limit OPG** (limit-on-open), which
gets exactly one chance in the opening auction. 8 of 9 produced no usable fill,
from two unrelated causes — four gapped through the limit, and five had the open
comfortably *inside* the limit and still did not fill.

Entries are now plain **DAY limits**: same price discipline, but live for the
whole session, so a stock that pulls back at 10:30 still gets bought.

That order type needs a second confirm pass before the close, and this module
pins why:

* a fill at 11:00 has no hard stop until something looks at it again;
* an order still unfilled at the close would expire and leave a tracker record
  behind, and the next morning's pass would read "no position, no open order"
  and buy a day-old signal at market.
"""

from types import SimpleNamespace

import pytest

import config
import executor
import position_tracker as pt


def _queued_position(symbol="EEM", shares=10):
    """A tracker record for an entry that has been submitted but not filled."""
    return {
        symbol: {
            "entry_price": 45.0,
            "stop_price": 43.0,
            "stop_price_a": 43.0,
            "stop_price_b": 42.0,
            "shares": shares,
            "stop_mult": 1.5,
            "stop_order_id": None,
            "entry_date": "2026-07-30",
            "atr14_at_signal": 1.2,
        }
    }


class _Recorder:
    """Captures submitted and cancelled orders instead of calling Alpaca."""

    def __init__(self):
        self.submitted = []
        self.cancelled = []

    def submit_order(self, request):
        self.submitted.append(request)
        return SimpleNamespace(id="order-1")

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(executor._client, "submit_order", rec.submit_order)
    monkeypatch.setattr(executor._client, "cancel_order_by_id", rec.cancel_order_by_id)
    return rec


# --- the order type ---------------------------------------------------------


def test_an_entry_is_a_day_limit_not_an_auction_order(isolated_state, recorder,
                                                      monkeypatch):
    monkeypatch.setattr(executor, "has_position", lambda symbol: False)
    monkeypatch.setattr(executor, "get_equity", lambda: 100_000.0)

    ok = executor.submit_entry({
        "symbol": "EEM", "close": 45.0, "active_stop": 43.0,
        "stop_a": 43.0, "stop_b": 42.0,
    })

    assert ok
    request = recorder.submitted[0]
    assert str(request.time_in_force).lower().endswith("day"), (
        "an OPG order gets one chance in the opening auction and 8 of 9 missed")
    # The price discipline is unchanged: prior close + ENTRY_LIMIT_PCT.
    assert request.limit_price == pytest.approx(
        round(45.0 * (1 + config.ENTRY_LIMIT_PCT), 2))


# --- the morning pass -------------------------------------------------------


def test_a_working_entry_is_left_alone_in_the_morning(isolated_state, recorder,
                                                      monkeypatch):
    """A DAY limit still open at 09:45 is normal, not a missed trade."""
    pt._save(_queued_position())
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [
        SimpleNamespace(id="entry-1", side="buy"),
    ])

    executor.confirm_fills_and_place_stops(final=False)

    assert recorder.cancelled == [], "the order had all session left to fill"
    assert recorder.submitted == [], "no market chase — that is the point of a limit"
    assert "EEM" in pt.all_positions()


def test_no_market_fallback_is_ever_sent(isolated_state, recorder, monkeypatch):
    """The old fallback existed because an OPG order was dead by 09:45.

    With no position and no working order the signal is simply not taken —
    chasing at market is what the limit is there to avoid.
    """
    pt._save(_queued_position())
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [])

    executor.confirm_fills_and_place_stops(final=False)

    assert recorder.submitted == [], "a market fallback was sent"
    assert "EEM" not in pt.all_positions(), "a stale record was left behind"


# --- the pre-close pass -----------------------------------------------------


def test_the_final_pass_cancels_an_unfilled_entry(isolated_state, recorder,
                                                  monkeypatch):
    """Left alone it expires at the close, and tomorrow's pass buys a stale signal."""
    pt._save(_queued_position())
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [
        SimpleNamespace(id="entry-1", side="buy"),
    ])

    executor.confirm_fills_and_place_stops(final=True)

    assert recorder.cancelled == ["entry-1"]
    assert "EEM" not in pt.all_positions(), "the record must not survive to tomorrow"


def test_the_final_pass_is_scheduled_before_the_close():
    """It can neither stop a late fill nor cancel anything after 15:00 CT."""
    from quantcore.bot_schedule import for_bot

    job = next(j for j in for_bot("meansrev") if j.name == "confirm-final")
    assert job.at.hour == 14, "must run before the 15:00 CT close"
    assert job.max_lateness.total_seconds() <= 15 * 60, (
        "a late run cannot do either half of this job")
