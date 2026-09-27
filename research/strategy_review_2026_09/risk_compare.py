"""MeansRev (live config) vs SPY: risk-adjusted, correlation, and blends."""
import pickle
from pathlib import Path
import numpy as np, pandas as pd, yfinance as yf
import live_engine as le, config

prep = pickle.loads(Path("prep.pkl").read_bytes())
eq = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")["equity"]
spy = yf.download("SPY", start="2010-12-01", end="2026-09-26", auto_adjust=True, progress=False)["Close"].squeeze().reindex(eq.index).ffill()
bil = yf.download("BIL", start="2010-12-01", end="2026-09-26", auto_adjust=True, progress=False)["Close"].squeeze().reindex(eq.index).ffill()
rf = bil.pct_change().fillna(0)

def stats(curve, label):
    r = curve.pct_change().fillna(0)
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs) - 1
    dd = (curve / curve.cummax() - 1).min()
    sharpe = (r - rf).mean() / (r - rf).std() * np.sqrt(252)
    return {"CAGR %": round(cagr * 100, 2), "max DD %": round(dd * 100, 1),
            "Sharpe": round(sharpe, 2), "CAGR / |DD|": round(cagr / abs(dd), 2)}

mr = eq / eq.iloc[0]; sp = spy / spy.iloc[0]
rows = {"MeansRev live config": stats(mr, ""), "SPY buy and hold": stats(sp, "")}
for w in (0.5, 0.7):
    blend = (w * mr.pct_change().fillna(0) + (1 - w) * sp.pct_change().fillna(0)).add(1).cumprod()   # daily rebalanced
    rows[f"{int(w*100)}% MeansRev / {int((1-w)*100)}% SPY"] = stats(blend, "")
tab = pd.DataFrame(rows).T
print(tab.to_string())
print("\ncorrelation of daily returns, MeansRev vs SPY:", round(mr.pct_change().corr(sp.pct_change()), 2))
print("MeansRev in SPY's 5 worst months:", (mr.resample("ME").last().pct_change() * 100).round(1)
      .loc[(sp.resample("ME").last().pct_change()).nsmallest(5).index].to_dict())
tab.to_csv("results/risk_compare.csv")
