"""Rank signals by rebound measures (step 2). Live config, both datasets.

The measures from rebound_measures.py are recomputed on each dataset and put
on the engine's per-symbol arrays; ranking takes the most stretched first.
"""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config
import rebound_measures as rm

PICK = ["% below 5-day average", "ConnorsRSI", "stretch: 10-day high to close, in ATRs",
        "cumulative RSI(2), 2 days", "20-day z-score"]


def attach(prep):
    rm.dates = prep["dates"]
    for s in config.SYMBOLS:
        m = rm.measures(prep["sym"][s])
        for name in PICK:
            prep["sym"][s]["m:" + name] = (rm.MORE_STRETCHED[name] * m[name]).values
    return prep


for name in PICK:
    le.RANKS["m:" + name] = ("m:" + name, -1)          # most stretched first
yf = attach(pickle.loads(Path("prep.pkl").read_bytes()))
al = attach(pickle.loads(Path("prep_alpaca.pkl").read_bytes()))
C = le.Params(symbols=list(config.SYMBOLS))
names = ["symbol_order"] + ["m:" + n for n in PICK]
res = {}
for n in names:
    p = replace(C, rank=n)
    for lab, prep, a, b in [("yf", yf, "2011-01-03", "2026-09-25"), ("h1", yf, "2011-01-03", "2018-12-31"),
                            ("h2", yf, "2019-01-02", "2026-09-25"), ("al", al, "2020-01-02", "2026-09-25")]:
        r = le.run(prep, p, a, b)
        res[(n, lab)] = (le.summarize(r), le.yearly_returns(r["equity"]))
rows = {}
for n in names:
    row = {c: res[(n, lab)][0]["cagr_pct"] for lab, c in [("yf", "CAGR 2011-26"), ("h1", "2011-18"), ("h2", "2019-26"), ("al", "Alpaca 2020-26")]}
    row["DD"] = res[(n, "yf")][0]["max_dd_pct"]
    for lab in ("yf", "al"):
        d = (res[(n, lab)][1] - res[("symbol_order", lab)][1]).dropna()
        row[f"yrs beat ({lab})"] = "-" if n == "symbol_order" else f"{int((d > 0.005).sum())}/{len(d)}"
    rows["symbol order (live)" if n == "symbol_order" else n[2:]] = row
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 250)
print(tab.to_string())
tab.to_csv("results/sweep_rebound_rank.csv")
