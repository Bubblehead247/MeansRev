"""get_fills must trust the by-id read over the (possibly stale) list snapshot.

XLV, 2026-10-09: the limit sell filled 20 and the market fallback filled 74, but
the list endpoint returned the fallback at 69, so 89 shares were booked for a
94-share trade.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import executor
from alpaca.trading.enums import OrderSide
from quantcore.reconcile import blended_fill_price


def _order(oid, qty, price):
    now = datetime.now()
    return SimpleNamespace(id=oid, side=OrderSide.SELL, filled_qty=str(qty),
                           filled_avg_price=str(price), filled_at=now,
                           updated_at=now, status="filled")


def test_get_fills_uses_by_id_read_not_stale_list(monkeypatch):
    stale = [_order("a", 20, 167.90), _order("b", 69, 169.369054)]
    fresh = {"a": _order("a", 20, 167.90), "b": _order("b", 74, 169.369054)}
    monkeypatch.setattr(executor._client, "get_orders", lambda req: stale)
    monkeypatch.setattr(executor._client, "get_order_by_id", lambda oid: fresh[oid])

    qty, price = blended_fill_price(executor.get_fills("XLV", "sell"))

    assert qty == 94
    assert round(price, 4) == 169.0565


def test_get_fills_falls_back_to_list_snapshot_on_error(monkeypatch):
    def boom(oid):
        raise RuntimeError("api down")
    monkeypatch.setattr(executor._client, "get_orders", lambda req: [_order("a", 20, 167.9)])
    monkeypatch.setattr(executor._client, "get_order_by_id", boom)

    assert [f["filled_qty"] for f in executor.get_fills("XLV", "sell")] == ["20"]
