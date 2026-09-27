"""Which "how stretched is it" measure predicts a better rebound?

Step 1: tag each live-config trade (1,853, yfinance 2011-2026) with the
measure at its signal close, split into fifths, and compare the average trade
of the most vs least stretched fifth in each half. Spearman rank correlation
with the trade's return as a summary.
Step 2 (rank sweep): see sweep_rebound_rank.py.
All measures use data up to the signal close only.
"""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

import live_engine as le
import config
from indicators import rsi

prep = pickle.loads(Path("prep.pkl").read_bytes())
dates = prep["dates"]


def measures(x):
    c = pd.Series(x["c"], index=dates)
    h, l = pd.Series(x["h"], index=dates), pd.Series(x["l"], index=dates)
    valid = c.notna()
    cv = c[valid]
    ret1 = cv.pct_change()
    # ConnorsRSI: RSI(3) of price, RSI(2) of the up/down streak, percent rank of the 1-day return
    streak = np.zeros(len(cv))
    for k in range(1, len(cv)):
        d = cv.iloc[k] - cv.iloc[k - 1]
        streak[k] = (streak[k - 1] + 1 if streak[k - 1] >= 0 else 1) if d > 0 else \
                    (streak[k - 1] - 1 if streak[k - 1] <= 0 else -1) if d < 0 else 0
    streak = pd.Series(streak, index=cv.index)
    prank = ret1.rolling(100).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True)
    crsi = (rsi(cv, 3) + rsi(streak, 2) + prank) / 3
    atr14 = pd.Series(x["atrp"], index=dates)[valid] * cv
    out = pd.DataFrame({
        "RSI(2)": pd.Series(x["rsi2"], index=dates)[valid],
        "cumulative RSI(2), 2 days": rsi(cv, 2) + rsi(cv, 2).shift(1),
        "ConnorsRSI": crsi,
        "down streak (days)": -streak.clip(upper=0),
        "stretch: 10-day high to close, in ATRs": (cv.rolling(10).max() - cv) / atr14,
        "20-day z-score": (cv - cv.rolling(20).mean()) / cv.rolling(20).std(),
        "% below 5-day average": cv / cv.rolling(5).mean() - 1,
        "rank of today's return (100 days)": prank,
        "IBS (close in day's range)": ((c - l) / (h - l))[valid],
    })
    return out.reindex(dates)


#: For each measure, which direction is "more stretched" (+1: higher = more).
MORE_STRETCHED = {
    "RSI(2)": -1, "cumulative RSI(2), 2 days": -1, "ConnorsRSI": -1, "down streak (days)": 1,
    "stretch: 10-day high to close, in ATRs": 1, "20-day z-score": -1, "% below 5-day average": -1,
    "rank of today's return (100 days)": -1, "IBS (close in day's range)": -1,
}

if __name__ == "__main__":
    M = {s: measures(prep["sym"][s]) for s in config.SYMBOLS}
    pickle.dump(M, open("rebound_measures.pkl", "wb"))
    res = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")
    tr = pd.DataFrame(res["trades"]); tr["entry"] = pd.to_datetime(tr["entry"])
    tr["signal"] = dates[dates.searchsorted(tr["entry"]) - 1]
    tr["half"] = np.where(tr["entry"] < "2019-01-01", "2011-18", "2019-26")
    rows = []
    for name, sign in MORE_STRETCHED.items():
        v = np.array([M[s].at[d, name] for s, d in zip(tr["symbol"], tr["signal"])], dtype=float)
        t = tr.assign(v=sign * v).dropna(subset=["v"])
        row = {"measure": name, "trades": len(t)}
        for half in ("2011-18", "2019-26", "all"):
            g = t if half == "all" else t[t["half"] == half]
            q = pd.qcut(g["v"].rank(method="first"), 5, labels=False)
            top, bot = g.loc[q == 4, "pct"].mean(), g.loc[q == 0, "pct"].mean()
            row[f"most-least stretched {half} (pts)"] = round(top - bot, 2)
        rho, pval = spearmanr(t["v"], t["pct"])
        row["spearman"] = round(rho, 3); row["p"] = round(pval, 3)
        rows.append(row)
    out = pd.DataFrame(rows).sort_values("spearman", ascending=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 45)
    print(f"{len(tr)} trades, mean {tr['pct'].mean():+.3f}%\n")
    print(out.to_string(index=False))
    out.to_csv("results/rebound_measures.csv", index=False)
