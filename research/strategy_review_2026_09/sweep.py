"""Baseline (live config) vs one-lever variants, 2011-01-03 .. 2026-09-25, yfinance data."""
import json
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

prep = pickle.loads(Path("prep.pkl").read_bytes())
START, END = "2011-01-03", "2026-09-25"
base = le.Params(symbols=list(config.SYMBOLS))
variants = {
    "baseline (live config)": base,
    "regime filter off": replace(base, regime_filter=False),
    "max positions 5": replace(base, max_positions=5),
    "max positions 6": replace(base, max_positions=6),
    "position cap 25%": replace(base, max_position_pct=0.25),
    "position cap 30%": replace(base, max_position_pct=0.30),
    "original 11 symbols": replace(base, symbols=le.ORIGINAL_11),
    "entries at open (no limit)": replace(base, limit_entries=False),
    "exits +0.10% slip (9:45 sells)": replace(base, exit_extra_slip=0.001),
}
out, yearly = {}, {}
for name, p in variants.items():
    res = le.run(prep, p, START, END)
    out[name] = le.summarize(res)
    yearly[name] = le.yearly_returns(res["equity"])
yr = pd.DataFrame(yearly)
b = yr["baseline (live config)"]
for name in variants:
    if name.startswith("baseline"):
        out[name]["years_beating_baseline"] = "-"
        continue
    diff = yr[name] - b
    out[name]["years_beating_baseline"] = f"{int((diff > 0.005).sum())}/{int((diff.abs() > 0.005).sum())} (of {len(diff)})"
tab = pd.DataFrame(out).T
pd.set_option("display.width", 250)
print(tab.to_string())
print()
print(yr.round(1).to_string())
Path("results").mkdir(exist_ok=True)
tab.to_csv("results/sweep_summary.csv")
yr.to_csv("results/sweep_yearly.csv")
