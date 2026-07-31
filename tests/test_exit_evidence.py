"""
Tests for the rule that a close needs real evidence before it is booked.

The anchor case is XLV on 2026-07-15, reconstructed from the broker record:

  * 08:25 — a limit-on-open BUY for 132 shares was submitted, and the position
    was written to the tracker straight away (that happens at submission, not
    at fill).
  * The 08:45 fill-confirm job never ran — the bot restarted at 08:46 and again
    at 08:48, past its slot. So no fallback order and no stop were placed, and
    ``stop_order_id`` stayed ``None``.
  * The buy order ended ``canceled`` with ``filled_qty=0``. Nothing was ever held.
  * 15:30 — the position checker saw XLV tracked as open and absent from Alpaca,
    concluded "stopped out", found no sell fill, and fell back to the *estimated*
    stop price of $151.73. That produced an exact -4.138% loss, ``days_held=0``,
    and $864.60 of fictional realized loss in trades.csv.

Nothing here touches Alpaca or the filesystem.
"""

import exit_evidence
from exit_evidence import BOOK, REVIEW, evaluate_vanished_position


def _xlv_position() -> dict:
    """XLV exactly as positions.json held it on 2026-07-15."""
    return {
        "symbol": "XLV",
        "entry_price": 158.28,
        "stop_price": 151.73,
        "stop_price_a": 154.35,
        "stop_price_b": 151.73,
        "shares": 132,
        "stop_mult": 2.5,
        "stop_order_id": None,      # the stop was never placed
        "exit_status": "open",
        "rsi2_at_signal": 9.22,
        "sma200_weekly_at_signal": 0.0,
        "sma50_daily_at_signal": 151.97,
        "atr14_at_signal": 2.62,
        "entry_date": "2026-07-15",
        "opened_at": "2026-07-15T13:25:50.272155",
    }


def _filled_position(**overrides) -> dict:
    """A position that really was held: entry confirmed, stop resting."""
    base = {
        "symbol": "QQQ",
        "entry_price": 688.434483,
        "stop_price": 653.21,
        "shares": 29,
        "stop_mult": 2.5,
        "stop_order_id": "65e76e3d-7871-4d74-b35d-564c9f8fd81b",
        "exit_status": "open",
        "entry_date": "2026-07-27",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# The XLV case
# --------------------------------------------------------------------------


def test_xlv_is_not_booked_as_a_closed_trade():
    """The whole point: no closed-trade row may be produced for XLV."""
    decision = evaluate_vanished_position(
        _xlv_position(),
        exit_fill_price=None,   # get_last_fill_price returned None
    )
    assert decision.action == REVIEW
    assert decision.should_book is False
    assert decision.exit_price is None


def test_xlv_missing_stop_order_id_alone_rules_out_a_stop_out():
    """A stop that was never placed cannot have triggered.

    This single check would have prevented the bug on its own.
    """
    decision = evaluate_vanished_position(_xlv_position(), exit_fill_price=None)
    assert "no stop order id" in decision.reason
    assert "cannot have been stopped out" in decision.reason


def test_xlv_is_surfaced_rather_than_swallowed():
    """The situation has to be reported, not quietly dropped."""
    decision = evaluate_vanished_position(_xlv_position(), exit_fill_price=None)
    assert decision.reason, "a review decision must explain itself"
    assert "XLV" in decision.reason


def test_the_estimated_stop_price_is_never_used_as_an_exit_price():
    """151.73 was the estimated stop. It must not become a fill price.

    Even with a stop order id present, a missing sell fill must not be papered
    over with the tracked stop.
    """
    position = _xlv_position()
    position["stop_order_id"] = "some-stop-order"   # pretend the stop existed
    decision = evaluate_vanished_position(position, exit_fill_price=None)
    assert decision.action == REVIEW
    assert decision.exit_price is None
    assert decision.exit_price != position["stop_price"]


def test_broker_reporting_zero_filled_shares_is_decisive():
    """filled_qty=0 proves the entry never happened, whatever else is recorded."""
    position = _xlv_position()
    position["stop_order_id"] = "stale-id-that-should-not-save-it"
    decision = evaluate_vanished_position(
        position, exit_fill_price=None, entry_fill_qty=0,
    )
    assert decision.action == REVIEW
    assert "never filled" in decision.reason


# --------------------------------------------------------------------------
# Real stop-outs must still be booked
# --------------------------------------------------------------------------


def test_a_real_stop_out_is_booked_at_the_actual_fill_price():
    """The fix must not stop the bot recording genuine exits."""
    decision = evaluate_vanished_position(
        _filled_position(), exit_fill_price=653.18,
    )
    assert decision.action == BOOK
    assert decision.should_book is True
    assert decision.exit_price == 653.18


def test_a_real_stop_out_is_booked_when_the_broker_confirms_the_entry():
    decision = evaluate_vanished_position(
        _filled_position(stop_order_id=None),   # stop placement failed
        exit_fill_price=653.18,
        entry_fill_qty=29,
    )
    assert decision.action == BOOK
    assert decision.exit_price == 653.18


def test_entry_confirmed_but_no_sell_fill_goes_to_review():
    """A position really held, really gone, but with no sell fill on record.

    Something happened that the bot cannot explain, so it must not guess.
    """
    decision = evaluate_vanished_position(
        _filled_position(), exit_fill_price=None,
    )
    assert decision.action == REVIEW
    assert "no sell fill was found" in decision.reason
    assert decision.exit_price is None


def test_a_zero_or_negative_fill_price_is_not_evidence():
    for bad_price in (0, 0.0, -1.0):
        decision = evaluate_vanished_position(
            _filled_position(), exit_fill_price=bad_price,
        )
        assert decision.action == REVIEW, f"{bad_price!r} was treated as a fill"


def test_module_has_no_broker_or_filesystem_dependency():
    """The rule must stay testable, which means staying pure."""
    source = open(exit_evidence.__file__, encoding="utf-8").read()
    for forbidden in ("import alpaca", "TradingClient", "open(", "requests"):
        assert forbidden not in source, f"exit_evidence must not use {forbidden}"
