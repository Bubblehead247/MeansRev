"""Tests for the T-bill cash sweep (cash_sweep.py).

Idle cash is parked in config.CASH_SWEEP_SYMBOL. Each evening the scan knows
tomorrow's entries, so cash is set to their cost + a buffer and the rest is
swept; at 09:25 any remaining shortfall is sold before the entries go in. The
ETF must never look like a strategy position, and its ledger must stay equal
to the broker's holding even when an order fills after the bot stops waiting.
"""

import json
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide

import cash_sweep
import config
import executor


class _Broker:
    """Just enough of TradingClient for the sweep. Orders fill as scripted."""

    def __init__(self, equity, cash, sgov_qty=0.0, price=100.0, fill_fraction=1.0):
        self.equity, self.cash, self.price = equity, cash, price
        self.sgov = sgov_qty
        self.other = {}                      # symbol -> qty of strategy positions
        self.orders = {}
        self.fill_fraction = fill_fraction
        self.cancelled = []

    # account / positions
    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), cash=str(self.cash))

    def get_all_positions(self):
        out = [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self.other.items()]
        if self.sgov > 0:
            out.append(SimpleNamespace(symbol="SGOV", qty=str(self.sgov),
                                       market_value=str(self.sgov * self.price),
                                       unrealized_pl="0"))
        return out

    # orders
    def submit_order(self, request):
        oid = f"o{len(self.orders) + 1}"
        order = SimpleNamespace(id=oid, side=request.side, qty=request.qty,
                                filled_qty="0", filled_avg_price=None, status="new",
                                request=request)
        self.orders[oid] = order
        self._fill(order, self.fill_fraction)
        return order

    def _fill(self, order, fraction):
        target = round(float(order.qty) * fraction, 6)
        extra = target - float(order.filled_qty)
        if extra <= 0:
            return
        order.filled_qty = str(target)
        order.filled_avg_price = str(self.price)
        order.status = "filled" if fraction >= 1 else "partially_filled"
        signed = extra if order.side == OrderSide.BUY else -extra
        self.sgov += signed
        self.cash -= signed * self.price

    def finish(self, oid):
        self._fill(self.orders[oid], 1.0)

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def get_order_by_client_id(self, cid):
        raise LookupError(cid)

    def get_orders(self, request):
        return [o for o in self.orders.values() if o.status in ("new", "partially_filled")]

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        o = self.orders[oid]
        if o.status in ("new", "partially_filled"):
            o.status = "canceled"

    def get(self, path, params=None):
        return []


@pytest.fixture
def broker(monkeypatch):
    b = _Broker(equity=105_000, cash=84_000)
    monkeypatch.setattr(executor, "_client", b)
    monkeypatch.setattr(config, "CASH_SWEEP_ENABLED", True)
    monkeypatch.setattr(cash_sweep, "_quote", lambda: (b.price, b.price))
    monkeypatch.setattr(cash_sweep, "trailing_yield", lambda days=21: 0.04)
    monkeypatch.setattr(cash_sweep.time, "sleep", lambda s: None)
    return b


def _entry(symbol="XLF", close=54.53, stop=52.73):
    return {"symbol": symbol, "action": "ENTRY",
            "scan_result": {"symbol": symbol, "close": close, "active_stop": stop}}


def _ledger():
    return [json.loads(l) for l in cash_sweep.LEDGER_FILE.read_text().splitlines()]


def test_the_etf_is_never_a_strategy_position(broker):
    broker.sgov = 500
    broker.other = {"XLF": 385}
    assert list(executor.get_alpaca_positions()) == ["XLF"]
    assert executor.get_sweep_position().symbol == "SGOV"


def test_spare_cash_above_the_band_is_parked(broker):
    row = cash_sweep.evening_rebalance([])
    # keeps the 2% buffer ($2,100) in cash, parks the rest
    assert row["side"] == "buy"
    assert broker.cash == pytest.approx(2_100, abs=100)
    assert _ledger()[0]["qty"] == pytest.approx(row["requested_qty"])


def test_tomorrows_entries_are_funded_the_evening_before(broker):
    broker.cash, broker.sgov = 5_000, 1_000                      # $100k parked
    need = cash_sweep.entry_cost([_entry()], broker.equity)
    assert need == pytest.approx(288 * 54.53 * 1.005, rel=1e-6)   # 15% cap of $105k, at the limit
    row = cash_sweep.evening_rebalance([_entry()])
    assert row["side"] == "sell"
    assert broker.cash >= need + 0.02 * broker.equity - 1


def test_inside_the_band_nothing_trades(broker):
    broker.cash, broker.sgov = 5_000, 1_000                      # 2% buffer + $2.9k spare < 5% band
    assert cash_sweep.evening_rebalance([]) is None
    assert broker.orders == {}


def test_no_buying_when_the_yield_is_under_the_gate(broker, monkeypatch):
    monkeypatch.setattr(cash_sweep, "trailing_yield", lambda days=21: 0.002)
    assert cash_sweep.evening_rebalance([]) is None
    assert broker.orders == {}


def test_the_morning_sells_only_what_the_entries_still_need(broker):
    broker.cash, broker.sgov = 3_000, 1_000
    row = cash_sweep.fund_entries([_entry()])
    assert row["side"] == "sell"
    assert broker.cash >= cash_sweep.entry_cost([_entry()], broker.equity)


def test_no_morning_sale_when_cash_already_covers_it(broker):
    broker.cash, broker.sgov = 20_000, 800
    assert cash_sweep.fund_entries([_entry()]) is None
    assert broker.orders == {}


def test_a_fill_after_the_wait_still_reaches_the_ledger(broker):
    broker.fill_fraction = 0.5
    row = cash_sweep.evening_rebalance([])
    assert row["qty"] == pytest.approx(row["requested_qty"] * 0.5)
    broker.finish(row["order_id"])                 # the rest fills later
    cash_sweep.settle(cancel=False)
    booked = sum(r["qty"] for r in _ledger())
    assert booked == pytest.approx(broker.sgov)
    assert cash_sweep.report()["ledger_matches_broker"]


def test_a_working_order_is_cancelled_before_the_next_decision(broker):
    broker.fill_fraction = 0.5
    first = cash_sweep.evening_rebalance([])
    broker.fill_fraction = 1.0
    cash_sweep.fund_entries([_entry()] * 6)
    assert first["order_id"] in broker.cancelled
    assert sum(r["qty"] if r["side"] == "buy" else -r["qty"] for r in _ledger()) \
        == pytest.approx(broker.sgov)


def test_the_status_report_does_not_cancel_a_working_order(broker):
    broker.fill_fraction = 0.5
    row = cash_sweep.evening_rebalance([])
    cash_sweep.report()
    assert row["order_id"] not in broker.cancelled


def test_report_adds_price_gain_and_dividends(broker, monkeypatch):
    cash_sweep.evening_rebalance([])
    broker.price = 100.5
    monkeypatch.setattr(broker, "get_all_positions", lambda: [SimpleNamespace(
        symbol="SGOV", qty=str(broker.sgov), market_value=str(broker.sgov * 100.5),
        unrealized_pl=str(broker.sgov * 0.5))])
    monkeypatch.setattr(broker, "get", lambda path, params=None: [
        {"symbol": "SGOV", "net_amount": "123.45"}, {"symbol": "XLF", "net_amount": "9"}])
    r = cash_sweep.report()
    assert r["dividends"] == pytest.approx(123.45)
    assert r["total"] == pytest.approx(broker.sgov * 0.5 + 123.45, abs=0.01)
    assert r["ledger_matches_broker"]


def test_disabled_does_nothing(broker, monkeypatch):
    monkeypatch.setattr(config, "CASH_SWEEP_ENABLED", False)
    assert cash_sweep.evening_rebalance([]) is None
    assert cash_sweep.fund_entries([_entry()]) is None
    assert broker.orders == {}
