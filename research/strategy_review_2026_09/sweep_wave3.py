"""Wave 3: more ETFs, and idle cash in SPY vs T-bills.

Extra ETFs chosen before looking at results; appended to the END of the list
(lowest priority). Sector groups assigned here (sectors.csv doesn't know them).
"""
import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import live_engine as le
import config

NEW = ["IEF", "LQD", "HYG", "SMH", "KRE", "ITB", "GDX", "EWJ", "EWZ", "FXI"]
GROUP = {"IEF": "Fixed Income", "LQD": "Fixed Income", "HYG": "Fixed Income", "SMH": "Technology",
         "KRE": "Financials", "ITB": "Consumer Discretionary", "GDX": "Commodity",
         "EWJ": "International", "EWZ": "International", "FXI": "International"}


def load(path):
    prep = le.prepare(pickle.loads(Path(path).read_bytes()))
    for s, g in GROUP.items():
        prep["sym"][s]["sector"] = g
    return prep


yfp, alp = load("bars_yf_plus.pkl"), load("bars_alpaca_plus.pkl")
C = le.Params(symbols=list(config.SYMBOLS))
PLUS = replace(C, symbols=list(config.SYMBOLS) + NEW)
V = {"live config (27 ETFs)": C, "+10 ETFs (37)": PLUS,
     "+10 ETFs, 8 positions x 12%": replace(PLUS, max_positions=8, max_position_pct=0.12),
     "+10 ETFs, scale-in": replace(PLUS, scale_in=True)}
res = {}
for n, p in V.items():
    for lab, prep, a, b in [("yf", yfp, "2011-01-03", "2026-09-25"), ("h1", yfp, "2011-01-03", "2018-12-31"),
                            ("h2", yfp, "2019-01-02", "2026-09-25"), ("al", alp, "2020-01-02", "2026-09-25")]:
        r = le.run(prep, p, a, b); res[(n, lab)] = (le.summarize(r), le.yearly_returns(r["equity"]), r)
rows = {}
for n in V:
    s = res[(n, "yf")][0]
    row = {"CAGR 11-26": s["cagr_pct"], "11-18": res[(n, "h1")][0]["cagr_pct"], "19-26": res[(n, "h2")][0]["cagr_pct"],
           "Alpaca 20-26": res[(n, "al")][0]["cagr_pct"], "DD": s["max_dd_pct"], "trades": s["trades"],
           "avg trade %": s["avg_trade_pct"], "invested %": s["avg_invested_pct"]}
    for lab in ("yf", "al"):
        d = (res[(n, lab)][1] - res[("live config (27 ETFs)", lab)][1]).dropna()
        row[f"yrs beat {lab}"] = "-" if n.startswith("live") else f"{int((d > 0.005).sum())}/{len(d)}"
    rows[n] = row
pd.set_option("display.width", 260)
print(pd.DataFrame(rows).T.to_string())
pd.DataFrame(rows).T.to_csv("results/sweep_wave3_universe.csv")

# ── idle cash: T-bills (BIL) vs SPY, on the live config, evening pre-sale, 2% buffer ──
r = res[("live config (27 ETFs)", "yf")][2]
eq, cash = r["equity"], r["cash"]
px = yf.download(["BIL", "SPY"], start="2010-10-01", auto_adjust=True, progress=False)["Close"]
rows = {}
for name, sym in [("idle cash at 0% (no sweep)", None), ("idle cash in T-bills (BIL, live)", "BIL"), ("idle cash in SPY", "SPY")]:
    ret = px[sym].pct_change().reindex(eq.index).fillna(0.0) if sym else pd.Series(0.0, index=eq.index)
    held = earned = costs = 0.0
    total = []
    for d in eq.index:
        earned += held * ret.loc[d]; held *= 1 + ret.loc[d]
        e = eq.loc[d] + earned - costs
        target = max(0.0, cash.loc[d, "cash"] + earned - costs - cash.loc[d, "next_entry_cost"] - 0.02 * e) if sym else 0.0
        costs += abs(target - held) * 1e-4; held = target
        total.append(e)
    t = pd.Series(total, index=eq.index)
    yrs = (t.index[-1] - t.index[0]).days / 365.25
    rows[name] = {"CAGR 11-26": round(((t.iloc[-1] / t.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
                  "max DD": round((t / t.cummax() - 1).min() * 100, 1),
                  "worst year": round(le.yearly_returns(t).min(), 1),
                  "2022 (bear year)": round(le.yearly_returns(t).loc[2022], 1)}
print()
print(pd.DataFrame(rows).T.to_string())
pd.DataFrame(rows).T.to_csv("results/sweep_wave3_idle_cash.csv")
