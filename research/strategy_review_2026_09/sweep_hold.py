"""Time-stop neighbours on top of option C: is 10 days a plateau or a spike?"""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

yf = pickle.loads(Path("prep.pkl").read_bytes())
al = le.prepare(pickle.loads(Path("bars_alpaca.pkl").read_bytes()))
C = le.Params(symbols=list(config.SYMBOLS))
rows = {}
for days in (7, 8, 9, 10, 12, 14):
    p = replace(C, max_hold_days=days)
    r = {}
    for lab, prep, a, b in [("yf 2011-26", yf, "2011-01-03", "2026-09-25"), ("yf 2011-18", yf, "2011-01-03", "2018-12-31"),
                            ("yf 2019-26", yf, "2019-01-02", "2026-09-25"), ("alpaca 2020-26", al, "2020-01-02", "2026-09-25")]:
        s = le.summarize(le.run(prep, p, a, b))
        r[lab] = s["cagr_pct"]
        if lab == "yf 2011-26":
            r["yf DD"] = s["max_dd_pct"]; r["avg hold (trades)"] = None
    rows[f"{days} days" + (" (live)" if days == 7 else "")] = r
tab = pd.DataFrame(rows).T.drop(columns=["avg hold (trades)"])
print(tab.to_string())
tab.to_csv("results/sweep_hold.csv")
