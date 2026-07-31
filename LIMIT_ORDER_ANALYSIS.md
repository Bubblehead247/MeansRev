# Why the limit-on-open orders almost never fill

**Status: investigation only. No execution behavior has been changed.**
The proposal at the end needs your agreement before anything is touched.

## What the bot does today

At 09:25 ET it submits a limit-on-open (LOO) order — `time_in_force="opg"` —
priced 0.5% either side of the prior close:

- buys at `prior_close × 1.005` (`ENTRY_LIMIT_PCT = 0.005`)
- sells at `prior_close × 0.995` (`EXIT_LIMIT_PCT = 0.005`)

An LOO order takes part **only in the opening auction**. If it does not fill
there it dies, and at 09:45 ET the bot sends a plain DAY market order instead.

## The record

All nine LOO orders placed between 2026-06-29 and 2026-07-28, with the actual
opening price of that session:

| Date | Sym | Side | Limit | Open | Open within limit? | Filled |
|---|---|---|---|---|---|---|
| 06-29 | SPY | buy | 733.00 | 736.61 | no, +0.492% through | 0 |
| 07-13 | SPY | sell | 751.17 | 752.62 | **yes** | 0 |
| 07-15 | XLV | buy | 159.07 | 157.45 | **yes** | 0 (canceled) |
| 07-20 | QQQ | buy | 698.79 | 702.35 | no, +0.509% through | 0 |
| 07-20 | DIA | buy | 523.24 | 522.04 | **yes** | 0 |
| 07-23 | QQQ | sell | 701.66 | 694.53 | no, −1.016% through | 0 |
| 07-24 | XLF | buy | 56.12 | 55.86 | **yes** | 0 |
| 07-27 | QQQ | buy | 687.75 | 691.78 | no, +0.587% through | 0 |
| 07-28 | DIA | sell | 518.64 | 525.89 | **yes** | 21 of 40 |

**8 of 9 produced no usable fill.** But they failed for two unrelated reasons,
and that distinction is the whole point.

## Cause 1 — the limit is set right at the size of the move it is trying to catch

Four orders missed because the open genuinely traded through the limit. Look at
the three buys:

**+0.492%, +0.509%, +0.587%** — against a 0.500% band.

That is not a coincidence. The strategy buys when RSI(2) drops below 10, i.e.
after a sharp one- or two-day fall, and it is *betting on a bounce*. The bounce
frequently arrives in the opening auction. So the 0.5% limit sits almost exactly
at the median size of the move the strategy exists to capture: the entry is
refused precisely when the thesis is working, and accepted when it is not.

That is adverse selection. The fills the limit *does* let through are
systematically the weaker ones.

## Cause 2 — half the misses are not about price at all

Five of the nine had an opening price **comfortably inside** the limit and still
did not fill:

- **XLV, 07-15** — buy limit 159.07, opened at **157.45**, a full 1.0% of room.
  Filled zero, and ended `canceled` rather than `expired`. This is the order at
  the centre of the phantom-trade bug.
- **DIA, 07-28** — sell limit 518.64, opened at **525.89**, 1.4% of room. Filled
  **21 of 40** shares.
- SPY 07-13, DIA 07-20, XLF 07-24 — all inside the limit, all zero.

A limit order that the market trades straight through, and that either fills
partially or not at all, was not competing on price. It was not fully
participating in the auction. Two candidates, in order of likelihood:

1. **The submission is too close to the cutoff.** Auction-eligible orders must be
   in before roughly 09:28 ET. The bot submits at 09:25:50, leaving about two
   minutes — and `EXECUTE_TIME` is a fixed wall-clock schedule with no awareness
   of market time, so a slow scan, an API retry or a restart eats that margin
   silently. Note the bot also restarted at 08:46 and 08:48 on 07-15.
2. **Thin auction liquidity on a paper account.** DIA's 21-of-40 partial is the
   classic signature of an auction with less size available than the order asked
   for. A real consolidated opening auction in DIA is deep; whatever this order
   met was not.

**A wider limit does nothing for cause 2.** That matters, because widening the
band is the obvious first instinct and it would fix at most four of the nine.

### Caveat on the data

The opening prices above come from Alpaca's **IEX** feed, which is what the bot
itself uses. IEX is one venue and its first print is not necessarily the official
consolidated opening auction price. The direction and rough size of each gap are
reliable; treat the exact basis points as approximate. Verifying cause 2 properly
would mean checking the consolidated open, or simply testing the change below.

## What this costs

The limit is providing very little protection and one real liability:

- In 8 of 9 cases it changed nothing about the eventual trade — the position was
  opened or closed anyway, minutes later, at the market.
- It creates a second code path that runs almost every day. **The XLV phantom
  trade happened inside that path**: the LOO did not fill, the 09:45 confirm job
  did not run, so no fallback and no stop were placed, and by 15:30 the bot
  concluded the position had been stopped out of a trade it had never entered.
- The one time it did fill (DIA 07-28, 21 shares) it created the partial fill that
  the ledger then flattened to a single price, understating that trade by ~$22.

Every bug found in this review on the MeansRev side lived in or was caused by the
limit-on-open path.

## Proposal

Ranked. **I have not implemented any of these.**

**Option A — drop the limit; submit a DAY market order at the open. (Recommended.)**
Deletes the two-step path and the failure mode that produced the phantom trade.
The bot ends up at the market price 8 out of 9 times already, so this mostly makes
the real behavior explicit. Cost: no protection against a violent gap open. Given
the universe is 11 large liquid ETFs, that risk is small — and the current limit
is not protecting against it anyway, since a gap through the limit just becomes a
market order 20 minutes later at a worse price.

**Option B — replace the LOO with a plain DAY limit at the open.** Same prices,
but not auction-only: the order rests through the session and fills whenever price
comes inside the limit. This is the only option that addresses **both** causes —
it removes the dependency on auction participation entirely. Keeps price
protection. Cost: a position may fill late in the day, or not at all, which
changes the holding-period profile the backtest assumes.

**Option C — widen the band to ~1.5% and keep the LOO.** Smallest change. Would
have converted 3 of the 4 genuine price misses. **Does not address cause 2 at
all**, so roughly half the misses would continue, along with the XLV failure path.
I do not recommend this — it looks like a fix while leaving the mechanism that
actually broke.

**Whichever you pick, one thing is worth doing regardless:** move the submission
earlier (09:20 ET) so it is not two minutes from the auction cutoff, and log when
an LOO fills partially rather than treating a partial as a miss.

Backtest note: `ENTRY_LIMIT_PCT` and `EXIT_LIMIT_PCT` are live-execution settings
only — `SLIPPAGE_PCT` is what the backtester models. So none of these options
invalidates the existing backtest results, but none is validated by them either.
Option B in particular changes the fill-timing assumption and deserves a re-run
before going live.
