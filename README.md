# Mean Reversion Bot — Connors RSI(2) on 11 liquid ETFs

Buy a short-term dip inside a long-term uptrend, then sell the bounce.

That is the whole idea. "Mean reversion" means betting that a price which has
dropped unusually fast will snap back toward its recent average. RSI(2) is the
measure of "unusually fast" — a 2-day Relative Strength Index, which runs from 0
to 100 and only reaches single digits after a sharp drop. The 200-day moving
average is the check that the drop is a dip in a rising market rather than the
start of a fall.

> **`config.py` is the source of truth.** Every number below is read from it. If
> the two ever disagree, the code is right and this file is stale.

## Trading Universe — 11 ETFs

| Group | Symbols |
|---|---|
| Broad index | SPY, QQQ, IWM, DIA |
| Sector (SPDR) | XLE, XLF, XLK, XLV, XLU |
| Commodity / bonds | GLD, TLT |

ETFs rather than individual stocks, on purpose: they cannot go bankrupt or get
delisted, so a backtest over them is not flattered by survivorship bias (the
error of only testing on companies that happened to survive). All 11 are mapped
to sectors in `sectors.py` for the `MAX_PER_SECTOR` cap.

## Strategy Logic

**Entry — both must be true on the same day:**

- **Trend gate:** close is **above** the 200-day SMA (`SMA_DAILY_TREND = 200`,
  `USE_DAILY_SMA200_FILTER = True`). Only buy dips in a market that is rising.
- **Dip signal:** **RSI(2) is below 10** (`RSI_ENTRY_THRESHOLD = 10.0`,
  `ENTRY_MODE = "oversold"`). Note the direction: the bot buys *while* the
  reading is still low, not after it recovers.

Three further filters exist in the code and are all **switched off**. They are
left in so they can be re-tested, and their values are still computed and logged:

| Filter | Setting | State | Why off |
|---|---|---|---|
| Weekly SMA(50)/SMA(200) gate | `USE_TREND_FILTER` | **off** | Superseded by the daily SMA200 gate |
| Volume spike > 1.5× 20-day average | `USE_VOLUME_FILTER` | **off** | Removed ~90% of signals; 12-year return fell from +51% to +11% |
| Weekly ADX band \[20, 25\) | `USE_REGIME_FILTER` | **off** | On 2026-09-10 → 09-27; turned off because it left ~85% of capital idle (see `research/strategy_review_2026_09/`) |

**Exit — first condition hit wins:**

1. **Hard stop** — a GTC stop order resting on Alpaca's servers at
   `entry − 2.5 × ATR(14)`. It executes even if this bot is not running, which is
   the point of putting it at the exchange rather than checking it here.
2. **Time stop** — held 7 calendar days or more (`MAX_HOLD_DAYS = 7`).
3. **RSI target** — RSI(2) crosses back **below** 70
   (`RSI_EXIT_THRESHOLD = 70.0`). This is the bounce being taken.

A fourth exit, weekly trend break, is tied to `USE_TREND_FILTER` and is therefore
inactive.

**Order execution.** Both entries and exits go out as limit-on-open (LOO) orders
at 09:25 ET, priced 0.5% either side of the prior close
(`ENTRY_LIMIT_PCT` / `EXIT_LIMIT_PCT = 0.005`). An LOO order only participates in
the opening auction; if it does not fill there it expires, and at 09:45 ET the bot
submits a plain DAY market order instead.

> In practice the limit rarely fills — most orders go out as the market fallback.
> See `LIMIT_ORDER_ANALYSIS.md`.

**Risk.**

- 1% of equity risked per trade (`RISK_PER_TRADE = 0.01`).
- No position may exceed 15% of equity (`MAX_POSITION_PCT = 0.15`). This cap
  binds often: the 1%-risk formula divides by the stop distance, so a low-volatility
  ETF with a tight stop produces a very large share count without it.
- At most 6 concurrent positions (`MAX_POSITIONS = 6`), and at most 2 in any one
  sector (`MAX_PER_SECTOR = 2`).
- Size = `floor(equity × 0.01 / stop_distance)`, then reduced to obey the 20% cap.

**Stop variants.** `STOP_MULT_A = 1.5` and `STOP_MULT_B = 2.5` are both recorded
for comparison; only `ACTIVE_STOP_MULT` (currently **2.5**) is actually traded.

## Setup

```bash
pip install -r requirements.txt
```

Create `alpaca.env` in this directory:

```
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
```

`config.py` loads it by absolute path, so it is found no matter where you launch
from. Note that `load_dotenv` does **not** override variables already set in your
shell — an old `ALPACA_API_KEY` in your environment will silently win over this
file. Check for one before debugging an authentication error.

Then:

```bash
py -3.14 meansrev_main.py
```

Use the `py -3.14` selector. The shared `quantcore` package (indicators, and the
blended fill-price helper this bot uses when an exit fills in pieces) is installed
for 3.14 only. See `SETUP.md`.

## Daily Schedule

`config.py` holds these times in **US/Central**, which is Eastern minus one hour.

| Config value | Central | Eastern | Job |
|---|---|---|---|
| `SCAN_TIME` | 15:30 | 16:30 | Post-close scan — evaluate all 11 symbols, queue actions |
| `EXECUTE_TIME` | 08:25 | 09:25 | Submit LOO orders for the next open |
| `FILL_CONFIRM_TIME` | 08:45 | 09:45 | Confirm fills, place GTC stops, send market fallbacks |

The 09:45 job is load-bearing and there is no catch-up pass: if the bot is not
running at that minute, an entry from 09:25 goes unconfirmed and unstopped for the
rest of the day. That is what happened on 2026-07-15.

## Bookkeeping

`trades.csv` is the record of closed trades and `positions.json` of open ones.
Both are anchored to this directory, not the working directory.

Two rules the bot now enforces, learned from a phantom trade that sat in the
ledger for two weeks:

- **A close is never booked without proof of both an entry fill and an exit
  fill.** A position is written to `positions.json` when the order is *submitted*,
  so "tracked but missing from Alpaca" does not mean "stopped out" — it can just
  as easily mean the entry never filled. When the evidence is missing the position
  is marked `needs_review` and nothing is written. See `exit_evidence.py`.
- **A recorded price is always a real fill price.** When an exit fills in more
  than one piece — common here, because a partial opening-auction fill plus a
  market fallback is two fills — the trade is booked at the quantity-weighted
  blend of them.

To check the books against the broker:

```bash
py -3.14 -m quantcore.reconcile --bot meansrev
```

It exits non-zero if anything disagrees.

## File Structure

```
MeansRev/
├── config.py            # All tunable parameters — start here
├── indicators.py        # Re-exports the shared quantcore indicators
├── scanner.py           # Fetches bars, evaluates signals
├── risk.py              # Position sizing
├── position_tracker.py  # positions.json persistence
├── executor.py          # All Alpaca interactions and order logic
├── exit_evidence.py     # Whether a vanished position may be booked as closed
├── meansrev_main.py     # Scheduler and job definitions
├── trade_log.py         # Appends closed trades to trades.csv
├── eval_checkpoint.py   # Counts closed trades toward the review checkpoint
├── notifier.py          # ntfy.sh push alerts
├── dashboard.py         # Local status view
├── sectors.py           # Symbol → sector map for MAX_PER_SECTOR
├── screener.py          # Standalone stock screener (does NOT affect live trading)
├── backtest*.py         # Backtests and validation
├── tests/               # pytest suite
├── positions.json       # Auto-generated — open positions
├── trades.csv           # Auto-generated — closed trades
├── pending.json         # Auto-generated — queued actions between jobs
└── old_code/            # Previous version of the bot
```

## Tests

```bash
py -3.14 -m pytest
```

## Comparing Stop Variants

Both stop prices are logged daily for every position:

```bash
grep "Stop_A\|Stop_B" bot.log
```

Run a full market cycle on each before drawing conclusions. The tighter stop
(1.5×) stops out more often for smaller losses; the wider stop (2.5×) survives
more shakeouts but loses more when a trend genuinely breaks.

## Going Live

1. Set `PAPER = False` in `config.py`.
2. Fund the Alpaca live account and swap in live keys.
3. Times in `config.py` are US/Central — adjust if the host runs elsewhere.
4. Watch the first few orders by hand before leaving it alone.
5. Run the reconciler daily and treat a non-zero exit as something to look at.
