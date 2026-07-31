"""
Regression test for the phantom-close bug, exercising the real code path.

This drives ``main.post_close_scan`` with XLV in exactly the state it was in on
2026-07-15 — tracked as open, absent from Alpaca, ``stop_order_id=None``, and no
sell fill anywhere — and requires that **no closed-trade row is written**.

Against the old code this fails: it wrote a row for -$864.60 using the estimated
stop price of $151.73 as if it were a fill.

Alpaca, the clock and the filesystem are all stubbed out.
"""

import pytest

import meansrev_main as main


@pytest.fixture
def xlv_state():
    """XLV as positions.json held it on 2026-07-15."""
    return {
        "XLV": {
            "symbol": "XLV",
            "entry_price": 158.28,
            "stop_price": 151.73,
            "stop_price_a": 154.35,
            "stop_price_b": 151.73,
            "shares": 132,
            "stop_mult": 2.5,
            "stop_order_id": None,     # the 08:45 job never ran, so no stop
            "exit_status": "open",
            "rsi2_at_signal": 9.22,
            "sma200_weekly_at_signal": 0.0,
            "sma50_daily_at_signal": 151.97,
            "atr14_at_signal": 2.62,
            "entry_date": "2026-07-15",
            "opened_at": "2026-07-15T13:25:50.272155",
        }
    }


@pytest.fixture
def harness(monkeypatch, xlv_state):
    """Wire post_close_scan to stubs and capture what it tries to write."""
    logged: list[tuple] = []
    removed: list[str] = []
    reviewed: list[str] = []
    state = dict(xlv_state)

    # A trading day, but no positions at the broker and no scan results.
    monkeypatch.setattr(main.executor, "is_trading_day_today", lambda: True)
    monkeypatch.setattr(main.executor, "get_alpaca_positions", lambda: {})
    monkeypatch.setattr(main.scanner, "run_scan", lambda: {})

    # There was never a sell, so the fill lookup finds nothing.
    monkeypatch.setattr(main.executor, "get_last_fill_price",
                        lambda symbol, side: None)
    # And the broker confirms the buy filled zero shares.
    if hasattr(main.executor, "get_filled_qty"):
        monkeypatch.setattr(main.executor, "get_filled_qty",
                            lambda symbol, side: 0.0)

    monkeypatch.setattr(main.pt, "all_positions", lambda: state)
    monkeypatch.setattr(main.pt, "get", lambda symbol: state.get(symbol))
    monkeypatch.setattr(main.pt, "remove", lambda symbol: removed.append(symbol))
    monkeypatch.setattr(main.trade_log, "log_closed_trade",
                        lambda pos, price, reason: logged.append((pos["symbol"], price, reason)))

    if hasattr(main.pt, "mark_needs_review"):
        monkeypatch.setattr(main.pt, "mark_needs_review",
                            lambda symbol, reason="": reviewed.append(symbol))
    # Notifications must never reach the network from a test.
    for name in ("send_review_needed", "send_error"):
        if hasattr(main.notifier, name):
            monkeypatch.setattr(main.notifier, name, lambda *a, **k: None)

    return {"logged": logged, "removed": removed, "reviewed": reviewed, "state": state}


def test_no_closed_trade_is_written_for_a_position_that_never_opened(harness):
    """The core assertion: trades.csv must not gain an XLV row."""
    main.post_close_scan()

    assert harness["logged"] == [], (
        f"a closed trade was booked for a position that never opened: "
        f"{harness['logged']}"
    )


def test_the_fictional_stop_price_is_never_written_as_a_fill(harness):
    main.post_close_scan()

    booked_prices = [price for _sym, price, _reason in harness["logged"]]
    assert 151.73 not in booked_prices, (
        "the estimated stop price was booked as though it were a real fill")


def test_the_position_is_surfaced_rather_than_silently_dropped(harness, caplog):
    """A position that cannot be explained must be reported, not swallowed."""
    with caplog.at_level("WARNING"):
        main.post_close_scan()

    surfaced = (
        harness["reviewed"]
        or any("XLV" in record.message for record in caplog.records)
    )
    assert surfaced, "XLV vanished without being flagged for review"


def test_a_real_stop_out_is_still_booked(monkeypatch, harness):
    """The fix must not break recording of genuine stop-outs."""
    harness["state"].clear()
    harness["state"]["QQQ"] = {
        "symbol": "QQQ",
        "entry_price": 688.434483,
        "stop_price": 653.21,
        "shares": 29,
        "stop_mult": 2.5,
        "stop_order_id": "65e76e3d-7871-4d74-b35d-564c9f8fd81b",
        "exit_status": "open",
        "entry_date": "2026-07-27",
    }
    # This time there IS a sell fill.
    monkeypatch.setattr(main.executor, "get_last_fill_price",
                        lambda symbol, side: 653.18)
    if hasattr(main.executor, "get_filled_qty"):
        monkeypatch.setattr(main.executor, "get_filled_qty",
                            lambda symbol, side: 29.0)

    main.post_close_scan()

    assert len(harness["logged"]) == 1, "a genuine stop-out was not booked"
    symbol, price, reason = harness["logged"][0]
    assert symbol == "QQQ"
    assert price == 653.18, "the booked price must be the actual fill"
    assert reason == "hard_stop"
    assert harness["removed"] == ["QQQ"]
