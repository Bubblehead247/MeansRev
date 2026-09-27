"""Untested levers, one at a time, on top of option C (live since 2026-09-28)."""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

C_NAME = "option C (live)"
runs = {}
for label, cache in [("yf", "prep.pkl"), ("alpaca", None)]:
    if cache:
        prep = pickle.loads(Path(cache).read_bytes())
    else:
        prep = le.prepare(pickle.loads(Path("bars_alpaca.pkl").read_bytes()))
    C = le.Params(symbols=list(config.SYMBOLS))   # config now holds option C
    variants = {
        C_NAME: C,
        "RSI entry < 5": replace(C, rsi_entry=5), "RSI entry < 15": replace(C, rsi_entry=15),
        "RSI exit 50": replace(C, rsi_exit=50), "RSI exit 60": replace(C, rsi_exit=60),
        "time stop 5d": replace(C, max_hold_days=5), "time stop 10d": replace(C, max_hold_days=10),
        "stop 1.5 ATR": replace(C, stop_mult=1.5), "stop 3.5 ATR": replace(C, stop_mult=3.5),
        "no hard stop": replace(C, stop_mult=50),
        "rank most oversold": replace(C, rank="most_oversold"),
        "entry limit +1.0%": replace(C, entry_limit_pct=0.01),
    }
    start = "2011-01-03" if label == "yf" else "2020-01-02"
    for name, p in variants.items():
        res = le.run(prep, p, start, "2026-09-25")
        runs[(label, name)] = (le.summarize(res), le.yearly_returns(res["equity"]))
rows = {}
for name in [k[1] for k in runs if k[0] == "yf"]:
    r = {}
    for label in ("yf", "alpaca"):
        s, yr = runs[(label, name)]
        base = runs[(label, C_NAME)][1]
        d = (yr - base).dropna()
        r[f"{label}_cagr"] = s["cagr_pct"]; r[f"{label}_dd"] = s["max_dd_pct"]
        r[f"{label}_beats"] = "-" if name == C_NAME else f"{int((d > 0.005).sum())}/{len(d)}"
    r["yf_pf"] = runs[("yf", name)][0]["pf"]; r["yf_invested"] = runs[("yf", name)][0]["avg_invested_pct"]
    rows[name] = r
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 220)
print(tab.to_string())
tab.to_csv("results/sweep_levers.csv")
