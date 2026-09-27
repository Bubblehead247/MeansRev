# MeansRev strategy review — 2026-09-27

Question from the user: are we following our rules, and how much capital sits
unused? Then: review the strategy (MeansRev only).

## Method

`live_engine.py` is a portfolio backtest written to follow the **live** rules,
because `backtest_portfolio.py` predates several of them. It matches the live
bot on regime filter (weekly ADX in [20, 25)), limit entries at prior close +0.5%
that can go unfilled, calendar-day time stop, signals taken in `config.SYMBOLS`
order, slot/sector caps counted at the scan.

Remaining differences (all approximations, not tuned):

- Weekly ADX from **completed** weeks of yfinance adjusted bars. Live uses the
  week in progress and Alpaca **IEX-only, unadjusted** bars. `check_signals.py`
  compared 321 symbol-days after 9/10: ADX differs by a median of 3 points (absolute)
  (band is 5 wide), so the live filter is not the filter that was backtested.
- Sizing from realized equity (live: marked-to-market equity).
- Exits at the open less 0.05% slip (live sells at market ~09:45; see the
  "exit +0.10%" column).
- Data: yfinance adjusted daily bars 2005-, simulation 2011-01-03 .. 2026-09-25,
  $100k start. Live uses Alpaca (from 2016).

## Results — final (`sweep_final.py`, `results/sweep_final*.csv`)

Source: backtest, yfinance adjusted data, 2011-01-03 .. 2026-09-25, $100k.
Cash-checked (no margin; entries the cash can't cover are rejected, like live).
"Exit +0.10%" is a stress test; the live data says there's no such cost (14 fills
at ~09:45 averaged +0.07% vs the open, median +0.10%, spread about +/-0.4%).

| Variant | CAGR | 2011-18 | 2019-26 | CAGR, exit +0.10% | Max DD | PF | Avg trade | Trades | Avg invested | Days in cash | Years beating live config |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Live config (backtest ADX) | 2.31% | -0.02% | 4.83% | 1.70% | -9.9% | 1.42 | 0.38% | 567 | 13.2% | 59.2% | - |
| ADX band [18,23) | 3.39% | 2.16% | 4.72% | 2.72% | -6.5% | 1.61 | 0.51% | 611 | 14.5% | 56.6% | 11/16 |
| ADX band [22,27) | 1.81% | 0.03% | 3.68% | 1.29% | -9.6% | 1.37 | 0.37% | 478 | 11.1% | 63.3% | 7/16 |
| ADX band [17,28) | 4.13% | 1.75% | 6.70% | 3.04% | -8.8% | 1.46 | 0.39% | 986 | 23.2% | 43.0% | 11/16 |
| Regime filter off | 5.22% | 2.82% | 7.80% | 3.59% | -14.0% | 1.35 | 0.31% | 1,484 | 34.8% | 27.6% | 11/16 |
| Regime off, 5 positions | 5.77% | 3.09% | 8.80% | 3.97% | -13.6% | 1.35 | 0.30% | 1,676 | 39.3% | 27.2% | 12/16 |
| Regime off, 6 positions | 6.03% | 3.05% | 9.16% | 4.23% | -14.3% | 1.37 | 0.32% | 1,695 | 39.6% | 26.9% | 12/16 |
| Regime off, 6 positions, 15% cap | 5.39% | 2.68% | 8.29% | 3.76% | -11.6% | 1.37 | 0.31% | 1,920 | 34.7% | 26.0% | 11/16 |
| Regime off, 5 positions, 25% cap | 6.09% | 3.34% | 8.90% | 4.00% | -15.7% | 1.36 | 0.32% | 1,482 | 41.5% | 27.5% | 11/16 |

"Live config (backtest ADX)" is not a replay of live: from 9/16 it rejected the
DIA and XLF entries that live took, because live's ADX differs (below).

Earlier one-lever runs on the filter-on config (`results/sweep_summary.csv`, run
before the cash check, which doesn't affect <=80% configs): 5-6 positions or a
25-30% cap add only 0.2-0.4 pt (capital use 14-18%), because signals, not slots,
are scarce. Limit entries beat open fills (open fills win only 4/16 years).
The "original 11 symbols" row is not evidence: the 27-symbol list was chosen
partly on backtested edge over this same history.

## Reading

- The regime filter keeps ~87% of capital idle (13% average invested, 59% of
  days fully in cash). It buys per-trade quality (PF 1.42 vs 1.35) and a
  shallower drawdown (-9.9% vs -14.0%) at a cost of ~3 pts of CAGR.
- Its edge depends on where the band sits: [18,23) is better on CAGR, PF and drawdown
  (2019-26 about equal), [22,27) is worse. Live ADX reads lower than the backtest's in 80% of 321
  symbol-days (median -1.3, IQR -4.3..-0.1; 12 trading days only), so the live
  band acts like a backtest band shifted up 1-4 pts, toward the weaker end.
- Filter off beats the live config in both halves (not a regime split) and in
  11/16 years; worse years: 2015, 2018 (-8.8% vs +4.3%), 2019, 2024, 2025.
- With the filter off, 5-6 positions help a little once cash is enforced.
  "6 positions, 15% cap" keeps drawdown at -11.6% for 5.4%.

## Live data issues found (separate from strategy) — FIXED in a3b8e16

The scanner now uses SIP, dividend-adjusted bars and completed weeks built from
daily bars. On 2026-09-25 data, all 27 symbols: weekly ADX median |diff| vs the
backtest 0.01, RSI(2) and SMA200 identical. A bad SPY print (2026-02-02, low
68.47) is clipped. With the fix, the "live config (backtest ADX)" row above now
describes what live does.


1. Scanner requests Alpaca bars with no `adjustment`, i.e. **raw**, from the
   **IEX** feed. Ex-dividend drops read as price drops: XLF ex-div 2026-09-21,
   RSI(2) on 9/22 was 0.88 raw vs 8.54 adjusted. Biases toward false "oversold"
   signals around ex-dates, and shifts ATR/SMA200 slightly.
2. Weekly ADX includes the unfinished week and IEX-only highs/lows, so it
   disagrees with the backtested ADX by ~3 points on a 5-point band.

## Follow-up: levers on top of option C (`sweep_levers.py`, `sweep_hold.py`)

Wins are years beating option C (yfinance 2011-2026 / Alpaca 2020-2026).
Only the time stop helps on both datasets; the rest lose or are coin flips.

| Variant | CAGR yf | DD yf | Years won yf | CAGR Alpaca | Years won Alpaca |
|---|---|---|---|---|---|
| Option C (live) | 5.39% | -11.6% | - | 9.44% | - |
| Time stop 10 days | 6.30% | -11.4% | 10/16 | 10.17% | 5/7 |
| RSI entry < 15 | 5.45% | -14.5% | 8/16 | 9.27% | 3/7 |
| Entry limit +1.0% | 5.49% | -12.3% | 7/16 | 9.76% | 3/7 |
| Rank most oversold | 5.44% | -11.9% | 8/16 | 9.10% | 3/7 |
| Stop 1.5 ATR | 5.28% | -9.6% | 7/16 | 8.94% | 2/7 |
| RSI exit 60 | 4.61% | -11.3% | 3/16 | 7.75% | 0/7 |
| RSI entry < 5 | 3.99% | -11.7% | 4/16 | 6.57% | 2/7 |

("No hard stop" isn't meaningful here: sizing divides by the stop distance.)

Time stop neighbours (CAGR): 7d 5.39 / 8d 6.13 / 9d 6.12 / 10d 6.30 / 12d 6.11 /
14d 6.45 on yfinance; each of 8-14 also beats 7 in 2011-18, 2019-26 and on
Alpaca 2020-26 (9.9-10.2 vs 9.44). A plateau, not a spike.

## Cash sweep: how much to keep in cash (`sweep_tbill3.py`)

BIL for SGOV, 1 bp per trade, buys only while the trailing yield is over 1%.
"Short" = a morning when the entries need more cash than is on hand, so the
ETF is sold at the open and those entries go in late.

| How cash is kept | Added, 2011-26 | Added, 2023-26 | Entry mornings short |
|---|---|---|---|
| Evening pre-sale, 2% buffer (live) | +0.46 pts/yr | +1.57 pts/yr | 0/1,125 (0%) |
| Fixed buffer 15% | +0.35 | +1.19 | 236 (21%) |
| Fixed buffer 30% | +0.25 | +0.86 | 108 (10%) |
| Fixed buffer 45% | +0.17 | +0.57 | 49 (4%) |
| Fixed buffer 60% | +0.10 | +0.33 | 22 (2%) |

The evening pre-sale is why the buffer can be 2%. If after-hours orders ever
stop filling, the fallback is the morning sale, and a fixed buffer trades
yield for fewer late entries as above. Paper pays no dividends (Alpaca's
paper rules), so cash_sweep.report() estimates them from SGOV's adjusted vs
raw prices: 3 ex-dates Jul-Sep 2026 at ~0.30% each, ~3.5%/yr.

## Events and ticker ranking (2026-09-27) — `event_study.py`, `event_holdcheck.py`, `sweep_rank.py`, `ticker_table.py`, `ticker_oos.py`

ETFs don't report earnings, so the MeansRev version of SeykotaBot's study uses
macro releases and market-wide earnings season. Sources: FOMC decisions from
federalreserve.gov calendar pages; CPI and jobs-report dates from the BLS
release archives via web.archive.org (bls.gov blocks scripts); earnings dates
from SeykotaBot's Nasdaq calendar (2016+). 1,853 trades, live config.

- Nothing actionable. An FOMC/CPI/jobs release on the signal day, or between
  the signal and the entry, changes the average trade by -0.54..+0.76 pts with
  every 95% CI spanning zero. Earnings season vs lull: no difference.
- "Held through a release" looked strong (jobs report +1.09 pts, 16/16 years)
  but is hold length: trades that last longer contain more releases. Same hold
  length: FOMC +0.05, CPI -0.02, jobs +0.40; held positions returned +0.18% on
  jobs-report days vs +0.15% on other days. No blackout, no event rule.

Ranking (which signal gets a slot; ~half of signal-days find none):

| Ranking | CAGR 2011-26 | 2011-18 | 2019-26 | Alpaca 2020-26 | Years beating live (yf / Alpaca) |
|---|---|---|---|---|---|
| Symbol order (live: broad indexes first) | 6.30% | 3.22% | 9.59% | 10.17% | - |
| Closest to the 200-day | 6.69% | 3.12% | 10.53% | 10.88% | 9/16, 5/7 |
| Own track record (past trades only) | 6.62% | 3.80% | 9.85% | 9.57% | 9/16, 3/7 |
| Most oversold | 6.28% | 3.41% | 9.34% | 9.49% | 7/16, 2/7 |
| Lowest IBS | 6.06% | 3.14% | 9.18% | 9.22% | 7/16, 2/7 |
| Nearest 52-week high (SeykotaBot's) | 5.86% | 2.94% | 8.97% | 9.48% | 7/16, 3/7 |
| Most volatile (ATR %) | 5.83% | 2.68% | 9.20% | 9.58% | 7/16, 2/7 |
| Reverse symbol order | 5.97% | 3.08% | 9.06% | 9.39% | 7/16, 3/7 |
| Biggest 5-day drop | 5.70% | 2.60% | 9.01% | 9.24% | 6/16, 2/7 |

No ranking beats the live order robustly; "closest to the 200-day" is the only
one ahead on both datasets but loses 2011-18 and wins 9/16 years.

Per ticker (`results/ticker_table.csv`): EFA, EEM and XLU lost in both halves.
But past results don't persist: dropping the tickers that lost in 2011-18
(IJH, XLE, GLD, ... later winners) cut 2019-26 from 9.59% to 7.55% and Alpaca
from 10.17% to 8.25%; demoting or re-ordering by 2011-18 results was a coin
flip. Keep the universe and the order.

## Rebound measures instead of the 52-week high (`rebound_measures.py`, `sweep_rebound_rank.py`)

Measures of "how stretched", all at the signal close. Step 1, trades split
into fifths by each measure: most minus least stretched fifth, average trade.

| Measure | 2011-18 | 2019-26 | All | Spearman (p) |
|---|---|---|---|---|
| % below 5-day average | -0.11 | +1.38 | +0.72 | 0.093 (0.000) |
| ConnorsRSI | -0.06 | +0.90 | +0.41 | 0.065 (0.005) |
| Rank of today's return (100 d) | -0.28 | +0.80 | +0.24 | 0.055 (0.018) |
| IBS | +0.54 | +0.19 | +0.29 | 0.045 (0.052) |
| Stretch: 10-day high to close in ATRs | +0.32 | +0.40 | +0.29 | 0.033 (0.158) |
| RSI(2) | -0.28 | +0.44 | +0.10 | 0.032 (0.163) |
| Down streak | +0.39 | -0.07 | +0.09 | 0.024 (0.301) |
| 20-day z-score | +0.15 | +0.29 | +0.17 | 0.017 (0.458) |
| Cumulative RSI(2), 2 days | +0.45 | +0.29 | +0.38 | 0.016 (0.488) |

The strongest ones split by regime (nothing in 2011-18); the consistent ones
are small and not significant. Step 2, as the slot ranking:

| Ranking | CAGR 2011-26 | 2011-18 | 2019-26 | Alpaca 2020-26 | Years beating live (yf / Alpaca) |
|---|---|---|---|---|---|
| Symbol order (live) | 6.30% | 3.22% | 9.59% | 10.17% | - |
| Cumulative RSI(2), 2 days | 6.40% | 3.36% | 9.65% | 10.32% | 7/16, 4/7 |
| ConnorsRSI | 5.98% | 3.08% | 9.09% | 9.33% | 5/16, 3/7 |
| 20-day z-score | 5.96% | 2.87% | 9.25% | 9.59% | 5/16, 3/7 |
| % below 5-day average | 5.90% | 3.14% | 8.83% | 8.97% | 7/16, 3/7 |
| Stretch in ATRs | 5.85% | 2.81% | 9.10% | 9.52% | 5/16, 2/7 |

None beats the live order. It puts broad index ETFs first, and those revert
best; ranking by stretch swaps in more sector funds.
