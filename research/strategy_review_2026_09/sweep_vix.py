"""VIX stretch as an entry filter (market-wide fear), plus SPY buy-and-hold for reference."""
import pickle
from dataclasses import replace
from pathlib import Path
import numpy as np, pandas as pd, yfinance as yf
import live_engine as le, config

vix = yf.download("^VIX", start="2005-01-01", auto_adjust=False, progress=False)["Close"].squeeze()
def with_vix(path):
    prep = pickle.loads(Path(path).read_bytes())
    v = vix.reindex(prep["dates"]).ffill()
    prep["vix"] = {"c": v.values, "sma10": v.rolling(10).mean().values}
    return prep
yfp, alp = with_vix("prep.pkl"), with_vix("prep_alpaca.pkl")
C = le.Params(symbols=list(config.SYMBOLS))
V = {"live config": C, "VIX >= its 10-day avg": replace(C, vix_min_stretch=0.0),
     "VIX >= 5% above its 10-day avg": replace(C, vix_min_stretch=0.05),
     "VIX >= 10% above its 10-day avg": replace(C, vix_min_stretch=0.10)}
res = {}
for n, p in V.items():
    for lab, prep, a, b in [("yf", yfp, "2011-01-03", "2026-09-25"), ("h1", yfp, "2011-01-03", "2018-12-31"),
                            ("h2", yfp, "2019-01-02", "2026-09-25"), ("al", alp, "2020-01-02", "2026-09-25")]:
        r = le.run(prep, p, a, b); res[(n, lab)] = (le.summarize(r), le.yearly_returns(r["equity"]))
rows = {}
for n in V:
    s = res[(n, "yf")][0]
    row = {"CAGR 11-26": s["cagr_pct"], "11-18": res[(n, "h1")][0]["cagr_pct"], "19-26": res[(n, "h2")][0]["cagr_pct"],
           "Alpaca 20-26": res[(n, "al")][0]["cagr_pct"], "DD": s["max_dd_pct"], "trades": s["trades"],
           "avg trade %": s["avg_trade_pct"], "invested %": s["avg_invested_pct"]}
    for lab in ("yf", "al"):
        d = (res[(n, lab)][1] - res[("live config", lab)][1]).dropna()
        row[f"yrs beat {lab}"] = "-" if n == "live config" else f"{int((d > 0.005).sum())}/{len(d)}"
    rows[n] = row
spy = yf.download("SPY", start="2011-01-01", end="2026-09-26", auto_adjust=True, progress=False)["Close"].squeeze()
spy = spy[spy.index >= "2011-01-03"]
yrs = (spy.index[-1] - spy.index[0]).days / 365.25
rows["SPY buy and hold (reference)"] = {"CAGR 11-26": round(((spy.iloc[-1] / spy.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
                                        "DD": round((spy / spy.cummax() - 1).min() * 100, 1)}
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 260)
print(tab.to_string())
tab.to_csv("results/sweep_vix.csv")
