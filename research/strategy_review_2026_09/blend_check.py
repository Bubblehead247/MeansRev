"""Robustness of the MeansRev + SPY-core blend: halves, Alpaca, monthly rebalance.
Also: the overnight move after a signal (why trading at the signal close lost)."""
import pickle
from pathlib import Path
import numpy as np, pandas as pd, yfinance as yf
import live_engine as le, config

spy_all = yf.download(["SPY", "BIL"], start="2010-12-01", end="2026-09-26", auto_adjust=True, progress=False)["Close"]

def curve(prep, a, b):
    return le.run(prep, le.Params(symbols=list(config.SYMBOLS)), a, b)["equity"]

def blend(mr, w):
    sp = spy_all["SPY"].reindex(mr.index).ffill()
    mr_r, sp_r = mr.pct_change().fillna(0), sp.pct_change().fillna(0)
    vals, a, b = [], w, 1 - w
    month = mr.index[0].month
    for d, x, y in zip(mr.index, mr_r, sp_r):
        if d.month != month:                     # rebalance on the first day of each month
            tot = a + b; a, b = w * tot, (1 - w) * tot; month = d.month
        a *= 1 + x; b *= 1 + y
        vals.append(a + b)
    return pd.Series(vals, index=mr.index)

def stats(c):
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    cagr = (c.iloc[-1] / c.iloc[0]) ** (1 / yrs) - 1
    dd = (c / c.cummax() - 1).min()
    return f"{cagr*100:5.2f}% / {dd*100:6.1f}% / {cagr/abs(dd):.2f}"

yfp = pickle.loads(Path("prep.pkl").read_bytes()); alp = pickle.loads(Path("prep_alpaca.pkl").read_bytes())
rows = {}
for lab, prep, a, b in [("2011-18", yfp, "2011-01-03", "2018-12-31"), ("2019-26", yfp, "2019-01-02", "2026-09-25"),
                        ("Alpaca 2020-26", alp, "2020-01-02", "2026-09-25"), ("2011-26", yfp, "2011-01-03", "2026-09-25")]:
    mr = curve(prep, a, b)
    rows[lab] = {"MeansRev alone": stats(mr / mr.iloc[0]),
                 **{f"{int(w*100)}/{int((1-w)*100)} MeansRev/SPY": stats(blend(mr, w)) for w in (0.8, 0.7, 0.5)},
                 "SPY alone": stats(blend(mr, 0.0))}
tab = pd.DataFrame(rows)
pd.set_option("display.width", 220)
print("CAGR / max DD / CAGR-to-DD, monthly rebalanced\n"); print(tab.to_string())
tab.to_csv("results/blend_check.csv")

# overnight move after a signal: close of signal day -> next open
tr = pd.DataFrame(le.run(yfp, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")["trades"])
dates = yfp["dates"]; gaps = []
for t in tr.itertuples():
    k = dates.searchsorted(pd.Timestamp(t.entry)); x = yfp["sym"][t.symbol]
    gaps.append((x["o"][k] / x["c"][k - 1] - 1) * 100)
print(f"\nsignal close -> next open, {len(gaps)} trades: mean {np.mean(gaps):+.3f}%, median {np.median(gaps):+.3f}%, "
      f"share lower {np.mean(np.array(gaps) < 0):.0%}")
