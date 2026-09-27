"""Which signal gets a slot when there are more signals than free slots?

Live config (option C + 10-day stop). Live takes signals in config.SYMBOLS
order. About half of all signal-days find no free slot, so the order matters.
Wins = calendar years the ranking beats symbol order.
"""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

yf = pickle.loads(Path("prep.pkl").read_bytes())
al = pickle.loads(Path("prep_alpaca.pkl").read_bytes())
C = le.Params(symbols=list(config.SYMBOLS))
names = ["symbol_order"] + list(le.RANKS) + ["reverse_order", "track_record"]
runs = {}
for n in names:
    p = replace(C, rank=n)
    for lab, prep, a in [("yf", yf, "2011-01-03"), ("h1", yf, "2011-01-03"), ("h2", yf, "2019-01-02"), ("al", al, "2020-01-02")]:
        b = "2018-12-31" if lab == "h1" else "2026-09-25"
        r = le.run(prep, p, a, b)
        runs[(n, lab)] = (le.summarize(r), le.yearly_returns(r["equity"]))
rows = {}
for n in names:
    row = {}
    for lab, col in [("yf", "2011-26"), ("h1", "2011-18"), ("h2", "2019-26"), ("al", "Alpaca 2020-26")]:
        row[f"CAGR {col}"] = runs[(n, lab)][0]["cagr_pct"]
    row["DD 2011-26"] = runs[(n, "yf")][0]["max_dd_pct"]
    row["avg trade %"] = runs[(n, "yf")][0]["avg_trade_pct"]
    for lab, tot in [("yf", 16), ("al", 7)]:
        d = (runs[(n, lab)][1] - runs[("symbol_order", lab)][1]).dropna()
        row[f"yrs beat live ({lab})"] = "-" if n == "symbol_order" else f"{int((d > 0.005).sum())}/{len(d)}"
    rows["symbol order (live)" if n == "symbol_order" else n] = row
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 250)
print(tab.to_string())
tab.to_csv("results/sweep_rank.csv")
