"""Final table: cash-checked engine, +0.10% exit stress on every row, ADX band shifts."""
import pickle
from dataclasses import replace
from pathlib import Path

import pandas as pd

import live_engine as le
import config

prep = pickle.loads(Path("prep.pkl").read_bytes())
base = le.Params(symbols=list(config.SYMBOLS))
off = replace(base, regime_filter=False)
BASE = "live config (backtest ADX)"
variants = {
    BASE: base,
    "band [18,23)": replace(base, adx_min=18, adx_max=23),
    "band [22,27)": replace(base, adx_min=22, adx_max=27),
    "band [17,28)": replace(base, adx_min=17, adx_max=28),
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
    s["cagr_exit+0.10%"] = le.summarize(le.run(prep, replace(p, exit_extra_slip=0.001),
                                               "2011-01-03", "2026-09-25"))["cagr_pct"]
    rows[name] = s
    yearly[name] = le.yearly_returns(full["equity"])
yr = pd.DataFrame(yearly)
for name in variants:
    d = yr[name] - yr[BASE]
    rows[name]["yrs_beat_base"] = "-" if name == BASE else f"{int((d > 0.005).sum())}/16"
cols = ["cagr_pct", "cagr_2011-18", "cagr_2019-26", "cagr_exit+0.10%", "max_dd_pct", "pf", "avg_trade_pct",
        "trades", "avg_invested_pct", "max_invested_pct", "days_in_cash_pct", "cash_rejected", "yrs_beat_base"]
tab = pd.DataFrame(rows).T[cols]
pd.set_option("display.width", 260)
print(tab.to_string())
tab.to_csv("results/sweep_final.csv")
yr.round(2).to_csv("results/sweep_final_yearly.csv")
