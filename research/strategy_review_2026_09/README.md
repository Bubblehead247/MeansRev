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
