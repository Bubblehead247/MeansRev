"""
Tests for the three bugs behind the 2026-09-27 reconcile alerts.

* **Partial fills booked as whole exits.** ``await_fill`` returned on the first
  shares filled, so XLV's fallback sells were written to trades.csv as 192 of
  248 shares (2026-09-08) and 32 of 126 (2026-09-18).
* **A second, identical order.** On 2026-08-31 two 124-share XLV buys reached
  the broker 3.1 s apart. The bot's run placed the first; the second arrived
  after that run had finished, from a submitter never identified. Entries and
  exits now carry a client id fixed for the day, ``_submit`` looks it up before
  sending, and an order that went out despite an error (alpaca-py silently
  re-sends after a 429/504) is recognised instead of placed again.
* **Emoji log lines dropped.** bot.log was opened as cp1252 under Task
  Scheduler, so every line with an emoji (the order-ID lines) failed to write.
"""

import csv
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus

import executor
import meansrev_main as main
import position_tracker as pt


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FillingOrder:
    """A market sell that fills in steps, one step per status read."""

    def __init__(self, steps, order_id="mkt-1", price="168.489113"):
        self.id = order_id
        self.side = OrderSide.SELL
        self._steps = list(steps)
        self.filled_avg_price = price
        self.filled_at = datetime.now(timezone.utc)
        self.updated_at = self.filled_at
        self.advance()

    def advance(self):
        qty, status = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        self.filled_qty = str(qty)
        self.status = status


def _xlv_position() -> dict:
    """XLV as positions.json held it on the morning of 2026-09-08."""
    return {
        "symbol": "XLV",
        "entry_price": 169.8034,
        "stop_price": 163.28,
        "shares": 248,
        "stop_mult": 2.5,
        "stop_order_id": "17357fc6-old-stop",
        "exit_status": "exit_pending",
        "exit_reason": "rsi_exit",
        "entry_date": "2026-08-31",
        "atr14_at_signal": 2.61,
    }


# --------------------------------------------------------------------------
# Partial fills
# --------------------------------------------------------------------------


def test_await_fill_waits_for_the_whole_order(monkeypatch):
    order = _FillingOrder([(192, OrderStatus.PARTIALLY_FILLED),
                           (248, OrderStatus.FILLED)])
    reads = iter([order, order])

    def get_order_by_id(order_id):
        current = next(reads)
        return current

    monkeypatch.setattr(executor._client, "get_order_by_id", get_order_by_id)
    monkeypatch.setattr(executor.time, "sleep", lambda s: order.advance())

    assert executor.await_fill("mkt-1") == (248.0, 168.489113)


def test_await_fill_gives_up_on_an_order_that_never_completes(monkeypatch):
    order = _FillingOrder([(32, OrderStatus.PARTIALLY_FILLED)])
    monkeypatch.setattr(executor._client, "get_order_by_id", lambda order_id: order)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    assert executor.await_fill("mkt-1", tries=3) is None


def test_the_xlv_fallback_exit_is_booked_at_its_full_size(isolated_state, monkeypatch):
    """Reproduces 2026-09-08: 248 sold, 192 had filled at the first status read."""
    pt._save({"XLV": _xlv_position()})
    monkeypatch.setattr(pt, "get_exit_pending_symbols", lambda: ["XLV"])
    monkeypatch.setattr(executor, "get_alpaca_positions", lambda: {
        "XLV": SimpleNamespace(qty="248", current_price="168.40",
                               avg_entry_price="169.8034"),
    })
    order = _FillingOrder([(192, OrderStatus.PARTIALLY_FILLED),
                           (248, OrderStatus.FILLED)])

    def get_orders(request):
        # The confirm step's "is a sell still open?" check sees nothing; the
        # fill lookup sees the market order in whatever state it is in now.
        return [] if request.status == QueryOrderStatus.OPEN else [order]

    monkeypatch.setattr(executor._client, "get_orders", get_orders)
    monkeypatch.setattr(executor._client, "submit_order", lambda req: order)
    monkeypatch.setattr(executor._client, "get_order_by_id", lambda order_id: order)
    monkeypatch.setattr(executor.time, "sleep", lambda s: order.advance())

    executor.confirm_exit_fills()

    with open(isolated_state / "trades.csv", newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
    assert float(row["shares"]) == 248
    assert float(row["realized_pnl"]) == pytest.approx((168.489113 - 169.8034) * 248, abs=0.01)
    assert "XLV" not in pt.all_positions()


# --------------------------------------------------------------------------
# Duplicate submits
# --------------------------------------------------------------------------


def _request(symbol="XLV", client_order_id="mr-entry-XLV-20260831"):
    return SimpleNamespace(symbol=symbol, client_order_id=client_order_id)


def _refuse_submit(request):
    raise AssertionError("submit_order must not be called")


def test_an_order_already_at_the_broker_is_not_placed_again(monkeypatch):
    """The 2026-08-31 shape: a second submitter with the same day's entry."""
    first = SimpleNamespace(id="771cc142", status=OrderStatus.FILLED)
    monkeypatch.setattr(executor._client, "get_order_by_client_id", lambda cid: first)
    monkeypatch.setattr(executor._client, "submit_order", _refuse_submit)

    assert executor._submit(_request()) is first


def test_a_dead_order_with_the_same_id_raises_rather_than_passing_as_live(monkeypatch):
    """A rejected stop returned as if placed would record protection that isn't there."""
    rejected = SimpleNamespace(id="x", status=OrderStatus.REJECTED)
    monkeypatch.setattr(executor._client, "get_order_by_client_id", lambda cid: rejected)
    monkeypatch.setattr(executor._client, "submit_order", _refuse_submit)

    with pytest.raises(RuntimeError, match="already exists"):
        executor._submit(_request())


def test_an_order_that_reached_the_broker_despite_an_error_is_used(monkeypatch):
    """The broker created the order, the client saw a 504."""
    placed = SimpleNamespace(id="771cc142", status=OrderStatus.NEW)
    lookups = []

    def get_order_by_client_id(cid):
        lookups.append(cid)
        if len(lookups) == 1:
            raise LookupError("not found")      # before submitting: nothing there
        return placed                           # after the error: it went out

    def submit_order(request):
        raise RuntimeError("504 Gateway Timeout")

    monkeypatch.setattr(executor._client, "get_order_by_client_id", get_order_by_client_id)
    monkeypatch.setattr(executor._client, "submit_order", submit_order)

    assert executor._submit(_request()) is placed
    assert lookups == ["mr-entry-XLV-20260831"] * 2


def test_a_submit_that_really_failed_still_raises(monkeypatch):
    def submit_order(request):
        raise RuntimeError("insufficient buying power")

    monkeypatch.setattr(executor._client, "submit_order", submit_order)

    with pytest.raises(RuntimeError, match="insufficient buying power"):
        executor._submit(_request())


def test_a_submit_error_with_a_rejected_order_behind_it_raises(monkeypatch):
    rejected = SimpleNamespace(id="x", status=OrderStatus.REJECTED)
    lookups = iter([None, rejected])

    def get_order_by_client_id(cid):
        found = next(lookups)
        if found is None:
            raise LookupError("not found")
        return found

    def submit_order(request):
        raise RuntimeError("504 Gateway Timeout")

    monkeypatch.setattr(executor._client, "get_order_by_client_id", get_order_by_client_id)
    monkeypatch.setattr(executor._client, "submit_order", submit_order)

    with pytest.raises(RuntimeError, match="504"):
        executor._submit(_request())


def test_order_ids_are_fixed_per_day_for_entries_and_exits_only(isolated_state, monkeypatch):
    submitted = []

    def submit_order(request):
        submitted.append(request)
        return SimpleNamespace(id=f"order-{len(submitted)}")

    monkeypatch.setattr(executor._client, "submit_order", submit_order)

    monkeypatch.setattr(executor, "has_position", lambda symbol: False)
    monkeypatch.setattr(executor, "get_equity", lambda: 100_000.0)
    executor.submit_entry({"symbol": "XLV", "close": 167.15, "active_stop": 160.38,
                           "stop_a": 162.0, "stop_b": 160.38})
    executor._place_stop_order("XLV", 159.31, 126)
    executor._place_stop_order("XLV", 159.50, 126)
    monkeypatch.setattr(executor, "has_position", lambda symbol: True)
    monkeypatch.setattr(executor, "_cancel_stop_for_symbol", lambda symbol: None)
    monkeypatch.setattr(executor, "get_alpaca_positions",
                        lambda: {"XLV": SimpleNamespace(qty="126")})
    executor.submit_exit("XLV", close_price=168.0)

    today = f"{date.today():%Y%m%d}"
    ids = [request.client_order_id for request in submitted]
    assert ids[0] == f"mr-entry-XLV-{today}"
    assert ids[1].startswith("mr-stop-XLV-") and ids[2].startswith("mr-stop-XLV-")
    assert ids[1] != ids[2], "a re-placed stop must not collide with the first"
    assert ids[3] == f"mr-exit-XLV-{today}"


# --------------------------------------------------------------------------
# Log encoding
# --------------------------------------------------------------------------


def test_emoji_log_lines_reach_bot_log():
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        main._configure_logging()
        logging.getLogger("executor").info(
            "📥 BUY LIMIT ORDER submitted | XLV | 124 shares | Order ID=771cc142")
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved

    text = main.LOG_FILE.read_text(encoding="utf-8")
    assert "📥 BUY LIMIT ORDER submitted | XLV" in text
