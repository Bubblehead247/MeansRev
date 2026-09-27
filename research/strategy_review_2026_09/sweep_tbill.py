"""T-bill cash sweep on top of the live config (option C + 10-day time stop).

BIL (yfinance adjusted close = total return incl. monthly dividends) stands
in for SGOV, which only exists from 2020. Each evening after the scan the
sweep is set to a target, one net trade per day:

* "evening" mechanism: target = cash - tomorrow's entry cost - buffer x equity.
  The shares for tomorrow's entries are sold the evening before (after hours),
  so the morning's entries are always funded.
* "fixed buffer" mechanism: target = cash - buffer x equity, no pre-sale. On a
  morning when entries cost more than the buffer, the gap has to be raised by
  selling at the open and those entries wait (counted, not modeled as a cost).

Trading cost: `cost_bps` of each trade's value (SGOV/BIL spread ~1 bp; after-
hours wider, so 3 bp base case, 10 bp stress).
"""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import live_engine as le
import config

prep = pickle.loads(Path("prep.pkl").read_bytes())
res = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")
eq, cash = res["equity"], res["cash"]
bil = yf.download("BIL", start="2010-12-01", auto_adjust=True, progress=False)["Close"].squeeze()
bil_ret = bil.pct_change().reindex(eq.index).fillna(0.0)


def simulate(buffer_pct, evening=True, cost_bps=3.0):
    held = 0.0          # BIL value held going into the day
    earned = costs = 0.0
    trades = short_days = short_entries_days = 0
    added = []          # cumulative sweep P&L by day
    entry_days = 0
    idx = eq.index
    for k, d in enumerate(idx):
        held_ret = held * bil_ret.iloc[k]
        earned += held_ret
        held += held_ret
        c = cash.loc[d, "cash"] + earned - costs   # strategy cash + what the sweep made
        nxt = cash.loc[d, "next_entry_cost"]
        buf = buffer_pct * (eq.iloc[k] + earned - costs)
        # cash that is not in BIL = c - held; target BIL:
        if evening:
            target = max(0.0, c - nxt - buf)
        else:
            target = max(0.0, c - buf)
            if nxt > 0:
                entry_days += 1
                if c - target < nxt:
                    short_days += 1
                    target = max(0.0, c - nxt)       # raised at the open instead
        trade = abs(target - held)
        if trade > 1.0:
            trades += 1
            costs += trade * cost_bps / 1e4
        held = target
        added.append(earned - costs)
    added = pd.Series(added, index=idx)
    total = eq + added
    yrs = (idx[-1] - idx[0]).days / 365.25
    cagr_with = (total.iloc[-1] / total.iloc[0]) ** (1 / yrs) - 1
    cagr_without = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    yearly = added.groupby(added.index.year).last().diff().fillna(added.groupby(added.index.year).last())
    avg_in_bil = None
    return {"added_cagr_pts": round((cagr_with - cagr_without) * 100, 2),
            "earned_$": round(earned), "costs_$": round(costs), "trades": trades,
            "trades_per_yr": round(trades / yrs, 1),
            "short_mornings": f"{short_days}/{entry_days}" if not evening else "0 (pre-sold)"}, yearly


rows, yearly = {}, {}
for b in (0.0, 0.02, 0.05, 0.10):
    rows[f"evening, buffer {b:.0%}"], yearly[f"evening {b:.0%}"] = simulate(b, True)
rows["evening, buffer 2%, 10 bp cost"], _ = simulate(0.02, True, 10.0)
for b in (0.0, 0.15, 0.30, 0.45):
    rows[f"fixed buffer {b:.0%}"], yearly[f"fixed {b:.0%}"] = simulate(b, False)
pd.set_option("display.width", 220)
print(f"strategy alone: CAGR {le.summarize(res)['cagr_pct']}%, final ${eq.iloc[-1]:,.0f}; "
      f"avg cash {cash['cash'].mean()/eq.mean():.0%} of equity")
print(pd.DataFrame(rows).T.to_string())
yr = pd.DataFrame(yearly)
bil_yr = (bil.groupby(bil.index.year).last().pct_change() * 100).round(2)
out = pd.DataFrame({"BIL total return %": bil_yr.reindex(yr.index),
                    "sweep $ (evening 2%)": yr["evening 2%"].round(0)})
print(); print(out.to_string())
pd.DataFrame(rows).T.to_csv("results/sweep_tbill.csv"); out.to_csv("results/sweep_tbill_yearly.csv")
