"""
exit_evidence.py — Decide whether a vanished position may be booked as closed.

A position can disappear from Alpaca for two very different reasons:

  1. It was really held and the GTC stop sold it during the session.
  2. It never existed — the entry order never filled, so there was nothing
     to hold in the first place.

The bot used to treat both the same way: tracked as "open" plus absent from
Alpaca meant "stopped out". A position is written to the tracker at order
*submission* time, before any fill, so an entry that never filled looks
identical to a stop-out. On 2026-07-15 that booked a $864.60 loss on XLV, a
trade that never happened: the limit-on-open buy was canceled with
``filled_qty=0``, and because no sell fill existed either, the exit price fell
back to the *estimated* stop price and produced an exact -4.138% loss with
``days_held=0``.

The rule enforced here: **never book a close without positive evidence of both
an entry fill and an exit fill.** When the evidence is missing, say so and let a
human or the reconciler settle it. Never invent a price.

This module is deliberately pure — no Alpaca client, no file access, no clock —
so the rule can be tested directly.
"""

from dataclasses import dataclass

#: What to do with a position that is no longer at the broker.
BOOK = "book"      # evidence is complete; write the closed trade
REVIEW = "review"  # evidence is missing; surface it and write nothing


@dataclass(frozen=True)
class ExitDecision:
    """The verdict on one vanished position."""

    action: str                 # BOOK or REVIEW
    exit_price: float | None    # only set when action is BOOK
    reason: str                 # why, in plain words

    @property
    def should_book(self) -> bool:
        return self.action == BOOK


def evaluate_vanished_position(
    pos_data: dict,
    *,
    exit_fill_price: float | None = None,
    entry_fill_qty: float | None = None,
) -> ExitDecision:
    """Decide what to do about a tracked position Alpaca no longer holds.

    Args:
        pos_data: The position dict from ``position_tracker``.
        exit_fill_price: Actual fill price of a sell for this symbol, or None
            when no sell fill could be found.
        entry_fill_qty: Shares the broker actually filled on the entry, when
            known. The broker is authoritative: ``0`` proves the entry never
            filled. When None, the presence of a stop order id is used instead,
            because the bot only places the stop *after* confirming a fill.

    Returns:
        An :class:`ExitDecision`. ``exit_price`` is only ever a real fill price;
        it is never estimated or derived from the stop.
    """
    symbol = pos_data.get("symbol", "?")

    # --- Did the entry ever fill? -----------------------------------------
    if entry_fill_qty is not None:
        entry_confirmed = entry_fill_qty > 0
        entry_evidence = f"broker filled {entry_fill_qty:g} shares on entry"
        if not entry_confirmed:
            return ExitDecision(
                REVIEW, None,
                f"{symbol} entry never filled (broker shows filled_qty=0), so "
                f"there was no position to close",
            )
    else:
        # No stop order id means the fill was never confirmed, because the stop
        # is only placed once an actual fill has been seen. That alone rules out
        # a stop-out: a stop that was never placed cannot have triggered.
        entry_confirmed = bool(pos_data.get("stop_order_id"))
        entry_evidence = "a stop order id is recorded, so the entry was confirmed"
        if not entry_confirmed:
            return ExitDecision(
                REVIEW, None,
                f"{symbol} has no stop order id, so its entry fill was never "
                f"confirmed and no stop was ever placed — it cannot have been "
                f"stopped out",
            )

    # --- Did the exit ever fill? ------------------------------------------
    if exit_fill_price is None or exit_fill_price <= 0:
        return ExitDecision(
            REVIEW, None,
            f"{symbol} is gone from Alpaca and {entry_evidence}, but no sell "
            f"fill was found — the exit price is unknown and must not be guessed",
        )

    return ExitDecision(
        BOOK, float(exit_fill_price),
        f"{symbol} closed at a confirmed fill price of ${exit_fill_price:.2f} "
        f"({entry_evidence})",
    )
