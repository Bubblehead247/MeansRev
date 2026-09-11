"""
Tests for how an exit's price and size get recorded.

Three separate bugs from the 2026-07-28 review, all in the same code path:

* **M** — the exit was booked from ``current_price``, the position's mark taken
  at ~09:45 *before* the market order was even sent, and then the tracker record
  was deleted so the real fill could never be reconciled. Every ``*_fallback``
  row in trades.csv was an estimate rather than a realized price.
* **N** — DIA on 2026-07-28 filled 21 shares at $525.53 in the opening auction
  (that order then expired) and 19 more at $524.391052 on the market fallback.
  The trade was written as 40 shares at one price, understating it by ~$22.
* **O** — the stop recomputed from the real fill was placed at the exchange but
  never written back to the tracker, so positions.json said $649.11 while the
  exchange held $653.21.
"""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import executor
import position_tracker as pt
import trade_log


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeOrder:
    def __init__(self, order_id, side, filled_qty, filled_price, status="filled",
                 filled_at=None):
        self.id = order_id
        self.side = side
        self.filled_qty = str(filled_qty) if filled_qty is not None else None
        self.filled_avg_price = str(filled_price) if filled_price is not None else None
        self.status = status
        self.filled_at = filled_at or datetime.now(timezone.utc)
        self.updated_at = self.filled_at


def _dia_position() -> dict:
    """DIA as positions.json held it before the 2026-07-28 exit."""
    return {
        "symbol": "DIA",
        "entry_price": 521.79,
        "stop_price": 507.06,
        "shares": 40,
        "stop_mult": 2.5,
        "stop_order_id": "ada425af-old-stop",
        "exit_status": "exit_pending",
        "exit_reason": "time_stop",
        "entry_date": "2026-07-20",
        "atr14_at_signal": 5.43,
    }


# --------------------------------------------------------------------------
# N — partial fills must blend
# --------------------------------------------------------------------------


def test_two_piece_exit_is_booked_at_the_blended_price(isolated_state, monkeypatch):
    """The DIA case: 21 @ 525.53 then 19 @ 524.391052 = 524.9890 over 40 shares."""
    position = _dia_position()
    pt._save({"DIA": position})

    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "get_fills", lambda symbol, side, on_date=None: [
        {"id": "opg", "filled_qty": "21", "filled_avg_price": "525.53",
         "status": "expired"},
        {"id": "mkt", "filled_qty": "19", "filled_avg_price": "524.391052",
         "status": "filled"},
    ])

    executor.confirm_exit_fills()

    rows = list(_read_csv(isolated_state / "trades.csv"))
    assert len(rows) == 1
    row = rows[0]
    assert float(row["exit_price"]) == pytest.approx(524.9890, abs=1e-3)
    assert float(row["shares"]) == pytest.approx(40.0)
    # (524.9890 - 521.79) * 40 = +127.96, versus the +105.60 that was booked.
    assert float(row["realized_pnl"]) == pytest.approx(127.96, abs=0.05)


def test_the_understatement_is_about_22_dollars(isolated_state, monkeypatch):
    """Name the money: the old single-price booking lost ~$22 on this trade."""
    position = _dia_position()
    pt._save({"DIA": position})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "get_fills", lambda symbol, side, on_date=None: [
        {"id": "opg", "filled_qty": "21", "filled_avg_price": "525.53"},
        {"id": "mkt", "filled_qty": "19", "filled_avg_price": "524.391052"},
    ])

    executor.confirm_exit_fills()

    row = list(_read_csv(isolated_state / "trades.csv"))[0]
    booked_before = 105.60
    assert float(row["realized_pnl"]) - booked_before == pytest.approx(22.36, abs=0.1)


def test_a_single_fill_exit_still_records_that_one_price(isolated_state, monkeypatch):
    position = _dia_position()
    pt._save({"DIA": position})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "get_fills", lambda symbol, side, on_date=None: [
        {"id": "mkt", "filled_qty": "40", "filled_avg_price": "524.50"},
    ])

    executor.confirm_exit_fills()

    row = list(_read_csv(isolated_state / "trades.csv"))[0]
    assert float(row["exit_price"]) == pytest.approx(524.50)
    assert float(row["shares"]) == pytest.approx(40.0)


# --------------------------------------------------------------------------
# M — never book before the fill, never delete without booking
# --------------------------------------------------------------------------


def test_a_vanished_position_with_no_sell_fill_is_not_booked(isolated_state, monkeypatch):
    """The silent trade loss: the record used to be deleted with no row written."""
    position = _dia_position()
    pt._save({"DIA": position})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "get_fills", lambda symbol, side, on_date=None: [])
    # The gone-but-no-fill branch retries briefly before giving up (W: a
    # transient miss must not permanently strand the position) — no need for
    # the test to actually wait out those retries.
    monkeypatch.setattr(executor.time, "sleep", lambda seconds: None)

    executor.confirm_exit_fills()

    assert not (isolated_state / "trades.csv").exists(), "a trade was booked with no fill"
    remaining = pt.all_positions()
    assert "DIA" in remaining, "the record was dropped, losing the trade entirely"
    assert remaining["DIA"]["exit_status"] == "needs_review"


def test_a_fallback_sell_that_has_not_filled_books_nothing(isolated_state, monkeypatch):
    """Reproduces M: the old code booked the ~09:45 quote before any fill."""
    position = _dia_position()
    pt._save({"DIA": position})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    # Position still held, no open sell order, so the fallback path runs.
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {
        "DIA": SimpleNamespace(qty="40", current_price="524.43",
                               avg_entry_price="521.79"),
    })
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [])
    monkeypatch.setattr(executor._client, "submit_order",
                        lambda req: SimpleNamespace(id="fallback-order"))
    # It never fills.
    monkeypatch.setattr(executor, "await_fill", lambda order_id, tries=5: None)

    executor.confirm_exit_fills()

    assert not (isolated_state / "trades.csv").exists(), (
        "a trade was booked from a quote before the order filled")
    assert "DIA" in pt.all_positions(), "the record was dropped before the fill"


def test_the_stale_quote_is_never_used_as_an_exit_price(isolated_state, monkeypatch):
    """524.43 was the 09:45 mark. It must not appear as a fill price."""
    position = _dia_position()
    pt._save({"DIA": position})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["DIA"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {
        "DIA": SimpleNamespace(qty="40", current_price="524.43",
                               avg_entry_price="521.79"),
    })
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [])
    monkeypatch.setattr(executor._client, "submit_order",
                        lambda req: SimpleNamespace(id="fallback-order"))
    monkeypatch.setattr(executor, "await_fill", lambda order_id, tries=5: (19.0, 524.391052))
    monkeypatch.setattr(executor, "get_fills", lambda symbol, side, on_date=None: [
        {"id": "opg", "filled_qty": "21", "filled_avg_price": "525.53"},
        {"id": "mkt", "filled_qty": "19", "filled_avg_price": "524.391052"},
    ])

    executor.confirm_exit_fills()

    row = list(_read_csv(isolated_state / "trades.csv"))[0]
    assert float(row["exit_price"]) != pytest.approx(524.43, abs=1e-6), (
        "the pre-fill quote was booked as the exit price")
    assert float(row["exit_price"]) == pytest.approx(524.9890, abs=1e-3)


# --------------------------------------------------------------------------
# O — the tracked stop must match the one at the exchange
# --------------------------------------------------------------------------


def test_tracked_stop_price_is_updated_to_the_stop_actually_placed(isolated_state):
    """QQQ: 688.434483 - 2.5 x 14.09 = 653.21, not the estimated 649.11."""
    pt._save({"QQQ": {
        "symbol": "QQQ",
        "entry_price": 682.0,
        "stop_price": 649.11,        # pre-fill estimate
        "stop_price_b": 649.11,
        "shares": 29,
        "stop_mult": 2.5,
        "stop_order_id": None,
        "exit_status": "open",
        "entry_date": "2026-07-27",
        "atr14_at_signal": 14.09,
    }})

    pt.update_stop_price("QQQ", 653.21)

    assert pt.get("QQQ")["stop_price"] == pytest.approx(653.21)


def test_the_closed_trade_row_carries_the_real_stop(isolated_state):
    """trade_log copies stop_price straight into the CSV, so it must be correct."""
    pt._save({"QQQ": {
        "symbol": "QQQ", "entry_price": 688.434483, "stop_price": 649.11,
        "shares": 29, "stop_mult": 2.5, "stop_order_id": "live-stop",
        "exit_status": "open", "entry_date": "2026-07-27", "atr14_at_signal": 14.09,
    }})
    pt.update_stop_price("QQQ", 653.21)

    trade_log.log_closed_trade(pt.get("QQQ"), 697.588519, "rsi_exit")

    row = list(_read_csv(isolated_state / "trades.csv"))[0]
    assert float(row["stop_price"]) == pytest.approx(653.21)


def test_confirming_a_fill_writes_the_real_stop_back_to_the_tracker(isolated_state,
                                                                   monkeypatch):
    """The actual bug: the stop was placed at the exchange but never recorded.

    ``confirm_fills_and_place_stops`` recomputed the stop from the real fill and
    sent it, then updated only entry_price and stop_order_id. This drives that
    function and requires stop_price to end up matching what was placed.
    """
    pt._save({"QQQ": {
        "symbol": "QQQ",
        "entry_price": 682.0,          # pre-fill estimate
        "stop_price": 649.11,          # pre-fill estimate
        "stop_price_a": 663.2,
        "stop_price_b": 649.11,
        "shares": 30,                  # pre-fill estimate
        "stop_mult": 2.5,
        "stop_order_id": None,
        "exit_status": "open",
        "entry_date": "2026-07-27",
        "atr14_at_signal": 14.09,
    }})

    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {
        "QQQ": SimpleNamespace(qty="29", avg_entry_price="688.434483"),
    })
    monkeypatch.setattr(executor, "_place_stop_order",
                        lambda symbol, stop_price, shares: "65e76e3d-new-stop")

    executor.confirm_fills_and_place_stops()

    record = pt.get("QQQ")
    # 688.434483 - 2.5 x 14.09 = 653.209..., rounded to 653.21
    assert record["stop_price"] == pytest.approx(653.21), (
        f"tracked stop is still {record['stop_price']}, not the stop that was placed")
    assert record["stop_order_id"] == "65e76e3d-new-stop"
    assert record["entry_price"] == pytest.approx(688.434483)
    assert record["shares"] == 29, "share count was not resynced to the real fill"


def test_share_count_is_resynced_to_the_actual_fill(isolated_state):
    pt._save({"QQQ": {
        "symbol": "QQQ", "entry_price": 688.43, "stop_price": 653.21,
        "shares": 30, "stop_mult": 2.5, "stop_order_id": None,
        "exit_status": "open", "entry_date": "2026-07-27",
    }})

    pt.update_shares("QQQ", 29)

    assert pt.get("QQQ")["shares"] == 29


# --------------------------------------------------------------------------
# P — paths and timestamps
# --------------------------------------------------------------------------


def test_state_and_ledger_paths_are_absolute():
    """A relative path silently starts a fresh empty ledger from another CWD."""
    import importlib

    fresh_pt = importlib.reload(importlib.import_module("position_tracker"))
    fresh_log = importlib.reload(importlib.import_module("trade_log"))
    assert fresh_pt.STATE_FILE.is_absolute()
    assert fresh_log.CSV_FILE.is_absolute()
    assert fresh_pt.STATE_FILE.name == "positions.json"
    assert fresh_log.CSV_FILE.name == "trades.csv"


def test_the_writer_and_the_trade_count_gate_read_the_same_file():
    """eval_checkpoint.py resolves trades.csv absolutely; the writer must match."""
    import importlib

    fresh_log = importlib.reload(importlib.import_module("trade_log"))
    checkpoint = importlib.import_module("eval_checkpoint")
    assert fresh_log.CSV_FILE == checkpoint.TRADES_CSV


def test_opened_at_is_timezone_aware(isolated_state):
    pt.add("XLF", entry_price=55.84, stop_price=54.14, stop_price_a=54.82,
           stop_price_b=54.14, shares=374, stop_mult=2.5)
    stamp = pt.get("XLF")["opened_at"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"{stamp!r} is a naive timestamp"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read_csv(path):
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


# --------------------------------------------------------------------------
# W3 — a previous round trip is not evidence about this one
# --------------------------------------------------------------------------


def test_a_sell_from_before_the_position_opened_is_ignored(monkeypatch):
    """MeansRev's universe is small, so symbols repeat.

    `get_last_fill_price` walks the last 10 closed orders and takes the first
    match. Unbounded, an *earlier* round trip's sell in the same symbol is handed
    back as the exit price for the trade being booked now — a real price, from
    the wrong trade.
    """
    old_sell = _FakeOrder("old", "sell", 40, 500.00,
                          filled_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [old_sell])

    assert executor.get_last_fill_price("SPY", "sell") == 500.00, (
        "unbounded, the old fill is returned")

    assert executor.get_last_fill_price(
        "SPY", "sell", not_before=date(2026, 7, 20)) is None, (
        "a fill from before the position opened was accepted as its exit")


def test_a_sell_after_the_position_opened_is_used(monkeypatch):
    """The bound must not reject the fill it is looking for."""
    recent = _FakeOrder("new", "sell", 40, 512.34,
                        filled_at=datetime(2026, 7, 28, tzinfo=timezone.utc))
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [recent])

    assert executor.get_last_fill_price(
        "SPY", "sell", not_before=date(2026, 7, 20)) == 512.34
