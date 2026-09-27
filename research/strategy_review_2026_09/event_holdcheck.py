"""Is "a release while held" real, or just longer holds? Two checks.

1. Stratified: compare trades with/without the event inside the SAME hold
   length (calendar days), then average the gaps weighted by trades.
2. Position-days: the return of every held position on release days vs on
   other days — no hold-length confound at all.
"""
import json, pickle
from pathlib import Path
import numpy as np, pandas as pd
import live_engine as le, config

prep = pickle.loads(Path("prep.pkl").read_bytes()); dates = prep["dates"]
res = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")
tr = pd.DataFrame(res["trades"]); tr["entry"] = pd.to_datetime(tr["entry"]); tr["exit"] = pd.to_datetime(tr["exit"])
tr["hold"] = (tr["exit"] - tr["entry"]).dt.days
ev = Path("events"); bls = json.loads((ev / "bls_release_dates.json").read_text())
sets = {"FOMC": pd.to_datetime(json.loads((ev / "fomc_decisions.json").read_text())),
        "CPI": pd.to_datetime(bls["cpi"]), "Jobs report": pd.to_datetime(bls["empsit"])}
rows = []
for name, days in sets.items():
    evi = pd.DatetimeIndex(sorted(set(days)))
    held = evi.searchsorted((tr["exit"] - pd.Timedelta(days=1)).values, side="right") > evi.searchsorted(tr["entry"].values, side="right")
    tr["m"] = held
    gaps, w = [], []
    for h, g in tr.groupby("hold"):
        if g["m"].sum() >= 5 and (~g["m"]).sum() >= 5:
            gaps.append(g.loc[g["m"], "pct"].mean() - g.loc[~g["m"], "pct"].mean()); w.append(len(g))
    strat = np.average(gaps, weights=w) if gaps else np.nan
    # position-day returns (close-to-close) on release days vs other days
    on, off = [], []
    evset = set(evi)
    for t in tr.itertuples():
        x = prep["sym"][t.symbol]
        a, b = dates.searchsorted(t.entry), dates.searchsorted(t.exit)
        for k in range(a + 1, b):           # full days held after the entry day
            r = x["c"][k] / x["c"][k - 1] - 1
            (on if dates[k] in evset else off).append(r * 100)
    rows.append({"event": name, "raw gap (pts/trade)": round(tr.loc[tr.m, "pct"].mean() - tr.loc[~tr.m, "pct"].mean(), 2),
                 "same-hold-length gap": round(strat, 2), "hold-length buckets": len(gaps),
                 "held-day return on release days %": round(np.mean(on), 3),
                 "held-day return other days %": round(np.mean(off), 3), "release position-days": len(on)})
print(pd.DataFrame(rows).to_string(index=False))
print("\nhold-length mix: mean hold with a jobs report", round(tr.loc[tr.m, 'hold'].mean(), 1), "d vs without", round(tr.loc[~tr.m, 'hold'].mean(), 1), "d")
pd.DataFrame(rows).to_csv("results/event_holdcheck.csv", index=False)
