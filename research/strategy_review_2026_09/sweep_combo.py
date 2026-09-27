"""Regime-off combos vs the live baseline; halves 2011-2018 / 2019-2026 as a split check."""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

prep = pickle.loads(Path("prep.pkl").read_bytes())
base = le.Params(symbols=list(config.SYMBOLS))
off = replace(base, regime_filter=False)
variants = {
    "baseline (live config)": base,
    "regime off": off,
    "regime off, 5 pos": replace(off, max_positions=5),
    "regime off, 6 pos": replace(off, max_positions=6),
    "regime off, 6 pos, 15% cap": replace(off, max_positions=6, max_position_pct=0.15),
    "regime off, 5 pos, 25% cap": replace(off, max_positions=5, max_position_pct=0.25),
}
rows, yearly = {}, {}
for name, p in variants.items():
    full = le.run(prep, p, "2011-01-03", "2026-09-25")
    s = le.summarize(full)
    for label, a, b in [("2011-18", "2011-01-03", "2018-12-31"), ("2019-26", "2019-01-02", "2026-09-25")]:
        s[f"cagr_{label}"] = le.summarize(le.run(prep, p, a, b))["cagr_pct"]
    rows[name] = s
    yearly[name] = le.yearly_returns(full["equity"])
yr = pd.DataFrame(yearly)
for name in variants:
    d = yr[name] - yr["baseline (live config)"]
    rows[name]["yrs_beat_base"] = "-" if name.startswith("baseline") else f"{int((d > 0.005).sum())}/16"
    d2 = yr[name] - yr["regime off"]
    rows[name]["yrs_beat_off"] = "-" if name in ("baseline (live config)", "regime off") else f"{int((d2 > 0.005).sum())}/16"
cols = ["cagr_pct", "cagr_2011-18", "cagr_2019-26", "max_dd_pct", "avg_invested_pct", "days_in_cash_pct",
        "trades", "win_pct", "pf", "yrs_beat_base", "yrs_beat_off"]
tab = pd.DataFrame(rows).T[cols]
pd.set_option("display.width", 250)
print(tab.to_string())
tab.to_csv("results/sweep_combo.csv")
yr.round(2).to_csv("results/sweep_combo_yearly.csv")
