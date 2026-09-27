"""How much cash to keep: evening pre-sale vs a fixed buffer (1 bp, 1% yield gate).

Fixed buffer = no evening pre-sale. Cash kept = buffer x equity; on a morning
when tomorrow's entries cost more than the cash on hand, the gap is sold at the
open and those entries go in late (~09:45). Counted as "short mornings".
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
trail = ((bil / bil.shift(21)) ** (252 / 21) - 1).reindex(eq.index).fillna(0.0)


def simulate(buffer_pct, presell, start=None, band=0.05, min_yield=0.01, cost_bps=1.0):
    idx = eq.index if start is None else eq.index[eq.index >= start]
    held = earned = costs = 0.0
    trades = short = entry_days = 0
    for d in idx:
        r = bil_ret.loc[d]; earned += held * r; held *= 1 + r
        e = eq.loc[d] + earned - costs
        c = cash.loc[d, "cash"] + earned - costs
        need = cash.loc[d, "next_entry_cost"]
        reserve = (need if presell else 0.0) + buffer_pct * e
        free = c - held - reserve
        trade = 0.0
        if free < 0:
            trade = -min(held, -free)
        elif free > band * e and trail.loc[d] > min_yield:
            trade = free
        if abs(trade) > 1:
            held += trade; trades += 1; costs += abs(trade) * cost_bps / 1e4
        if need > 0:
            entry_days += 1
            if not presell and c - held < need:          # not enough cash at 09:25
                short += 1
                sell = min(held, need - (c - held))
                held -= sell; trades += 1; costs += sell * cost_bps / 1e4
    yrs = (idx[-1] - idx[0]).days / 365.25
    base = eq.loc[idx]
    add = ((base.iloc[-1] + earned - costs) / base.iloc[0]) ** (1 / yrs) - (base.iloc[-1] / base.iloc[0]) ** (1 / yrs)
    return round(add * 100, 2), f"{short}/{entry_days} ({short / max(entry_days, 1):.0%})"


rows = {}
for name, b, pre in [("evening pre-sale, 2% buffer (live)", 0.02, True),
                     ("fixed buffer 15%", 0.15, False), ("fixed buffer 30%", 0.30, False),
                     ("fixed buffer 45%", 0.45, False), ("fixed buffer 60%", 0.60, False)]:
    a1, s1 = simulate(b, pre)
    a2, s2 = simulate(b, pre, start="2023-01-03")
    rows[name] = {"added pts/yr 2011-26": a1, "added pts/yr 2023-26": a2,
                  "entry mornings short of cash 2011-26": s1}
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 200)
print(tab.to_string())
tab.to_csv("results/sweep_tbill_buffers.csv")
