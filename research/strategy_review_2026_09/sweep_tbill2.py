"""T-bill sweep with rebalancing bands and a yield gate (follow-up to sweep_tbill.py).

Policy each evening (after the scan knows tomorrow's entries):
  free = cash - tomorrow's entry cost - buffer x equity
  * free < 0 (can't fund tomorrow):  sell BIL to bring free back to 0   (always)
  * free > band x equity:            buy BIL with all of `free`         (only then)
  * otherwise:                       no trade
Yield gate: only buy when BIL's trailing 21-day return, annualized, is above
`min_yield`; below it the sweep only sells (drains to cash).
"""
import pickle
from pathlib import Path

import pandas as pd
import yfinance as yf

import live_engine as le
import config

prep = pickle.loads(Path("prep.pkl").read_bytes())
res = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")
eq, cash = res["equity"], res["cash"]
bil = yf.download("BIL", start="2010-10-01", auto_adjust=True, progress=False)["Close"].squeeze()
bil_ret = bil.pct_change().reindex(eq.index).fillna(0.0)
trail_yield = ((bil / bil.shift(21)) ** (252 / 21) - 1).reindex(eq.index).fillna(0.0)


def simulate(buffer_pct=0.02, band=0.05, min_yield=0.01, cost_bps=1.0, start=None):
    idx = eq.index if start is None else eq.index[eq.index >= start]
    held = earned = costs = 0.0
    trades = 0
    bil_share = []
    for d in idx:
        r = bil_ret.loc[d]
        earned += held * r
        held *= 1 + r
        e = eq.loc[d] + earned - costs
        c = cash.loc[d, "cash"] + earned - costs
        free = c - held - cash.loc[d, "next_entry_cost"] - buffer_pct * e   # cash not in BIL, after needs
        trade = 0.0
        if free < 0:
            trade = -min(held, -free)
        elif free > band * e and trail_yield.loc[d] > min_yield:
            trade = free
        if abs(trade) > 1.0:
            held += trade
            trades += 1
            costs += abs(trade) * cost_bps / 1e4
        bil_share.append(held / e if e > 0 else 0.0)
    yrs = (idx[-1] - idx[0]).days / 365.25
    base = eq.loc[idx]
    total_end = base.iloc[-1] + earned - costs
    cagr_with = (total_end / base.iloc[0]) ** (1 / yrs) - 1
    cagr_without = (base.iloc[-1] / base.iloc[0]) ** (1 / yrs) - 1
    return {"added_pts/yr": round((cagr_with - cagr_without) * 100, 2),
            "earned_$": round(earned), "costs_$": round(costs), "trades/yr": round(trades / yrs, 1),
            "avg_in_tbills": f"{pd.Series(bil_share).mean():.0%}"}


rows = {}
for label, start in [("2011-2026", None), ("2023-2026 (rates ~4-5%)", "2023-01-03")]:
    for kw in [dict(band=0.0, min_yield=-1), dict(band=0.05, min_yield=-1), dict(band=0.10, min_yield=-1),
               dict(band=0.05, min_yield=0.01), dict(band=0.10, min_yield=0.01),
               dict(band=0.05, min_yield=0.01, cost_bps=3.0), dict(band=0.05, min_yield=0.01, buffer_pct=0.0),
               dict(band=0.05, min_yield=0.01, buffer_pct=0.05)]:
        full = dict(buffer_pct=0.02, band=0.05, min_yield=0.01, cost_bps=1.0); full.update(kw)
        name = (f"{label} | buffer {full['buffer_pct']:.0%}, band {full['band']:.0%}, "
                f"gate {'off' if full['min_yield'] < 0 else '1%'}, {full['cost_bps']:.0f}bp")
        rows[name] = simulate(start=start, **full)
pd.set_option("display.width", 220); pd.set_option("display.max_colwidth", 80)
print(pd.DataFrame(rows).T.to_string())
pd.DataFrame(rows).T.to_csv("results/sweep_tbill_bands.csv")
