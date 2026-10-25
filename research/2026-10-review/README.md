# MeansRev monthly review, 2026-10 (analysis folder)

Report: `C:\Users\Cody\claude\monthly-reviews\2026-10\meansrev.md`. Read-only analysis; nothing here places orders or changes settings.

| File | What it does |
|---|---|
| `fetch_bars.py` | Daily SIP bars, dividend-adjusted and raw, for the 27-symbol universe, SGOV and SPY (credentials from `alpaca.env`, never printed). Writes a pickle (not committed). |
| `review_analysis.py` | Sections A–H: trade stats, equity/benchmarks/exposure, signal recompute vs `bot.log`, replay of the live rules (with and without the two lost scans, with and without the sector cap), execution vs open/prior close, SGOV and dividend checks, equity-vs-ledger reconciliation, picks vs SPY on the same windows. |
| `results/review_results.json` | Every number used in the report. |
| `results/execution_fills.csv`, `results/selection_vs_spy.csv` | Per-fill and per-trade detail. |

Reproduce:

```
python fetch_bars.py <DIR>/bars.pkl
python review_analysis.py <DIR>/bars.pkl results
```

Model notes (`review_analysis.py`, section D replay):
- Rules mirrored from `meansrev_main.post_close_scan` and `scanner.evaluate_symbol`: RSI(2)<10 and close>SMA200, symbols in `config.SYMBOLS` order, 6 positions, 2 per sector, limit at prior close +0.5% (fills at the open, or at the limit if the day's low reaches it), stop 2.5 ATR, RSI cross back below 70 exit, 10-calendar-day time stop, sizing min(1% risk, 15% cap) on prior-day equity.
- Validation: with the two lost scans (9/28, 10/14) skipped, the replay reproduces the live trade set and entry/exit dates exactly (9 positions). Dollar P&L differs by about −$300 because the replay exits at the open, while live exits at 9:45 ET and averaged 11.9 bps better.
- Known approximations: exits at the open; no intraday timing of stop placement; adjusted bars fetched on 10/25 (tiny restatement of RSI for dividends since). The 10/14 "outage cost" is one episode, not an expected value.
- Dividend method: per-share distribution = prior raw close × (adjusted return − raw return). Reproduces the broker's DIA dividend to 0.4%.

Do not commit `bars.pkl`.
