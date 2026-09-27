"""Do macro releases or earnings season change MeansRev's trades?

Live rules (option C + 10-day time stop), yfinance 2011-2026. Each trade is
tagged by what fell on its signal day, just after it (before the entry), or
while it was held, then its return is compared with the trades without that
event: difference in mean trade %, a bootstrap 95% CI, and in how many years
the tagged trades did better.

Event dates (events/):
* FOMC decisions: federalreserve.gov FOMC calendar pages (2011-2026).
* CPI and jobs report (Employment Situation): BLS release archives, read via
  web.archive.org (bls.gov blocks scripts).
* Earnings intensity: SeykotaBot's Nasdaq earnings calendar (2016-2026) —
  reports market-wide in the 5 sessions after the signal.
"""
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

import live_engine as le
import config

rng = np.random.default_rng(7)
prep = pickle.loads(Path("prep.pkl").read_bytes())
dates = prep["dates"]
res = le.run(prep, le.Params(symbols=list(config.SYMBOLS)), "2011-01-03", "2026-09-25")
tr = pd.DataFrame(res["trades"])
tr["entry"] = pd.to_datetime(tr["entry"]); tr["exit"] = pd.to_datetime(tr["exit"])
pos = dates.searchsorted(tr["entry"])
tr["signal"] = dates[pos - 1]
tr["year"] = tr["entry"].dt.year

ev = Path("events")
fomc = pd.to_datetime(json.loads((ev / "fomc_decisions.json").read_text()))
bls = json.loads((ev / "bls_release_dates.json").read_text())
cpi, nfp = pd.to_datetime(bls["cpi"]), pd.to_datetime(bls["empsit"])
earn = pd.read_parquet(Path("../../../SeykotaBot/research/2026-10-monthly-review/data/earnings_calendar.parquet"))
earn = earn.dropna(subset=["symbol"])
per_day = earn.groupby(pd.to_datetime(earn["date"])).size()


def tag(event_days):
    ev = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(event_days).normalize())))
    on_signal = tr["signal"].isin(set(ev))

    def any_between(lo, hi):          # any event with lo < d <= hi
        return ev.searchsorted(hi.values, side="right") > ev.searchsorted(lo.values, side="right")

    # "ahead": after the signal close, up to and including the entry day
    # (CPI and the jobs report publish at 08:30 ET, before the entry fills).
    ahead = pd.Series(any_between(tr["signal"], tr["entry"]), index=tr.index)
    # "while held": after the entry day, before the exit day
    during = pd.Series(any_between(tr["entry"], tr["exit"] - pd.Timedelta(days=1)), index=tr.index)
    return on_signal, ahead, during


def compare(mask, label):
    a, b = tr.loc[mask, "pct"], tr.loc[~mask, "pct"]
    if len(a) < 20 or len(b) < 20:
        return {"label": label, "n_with": len(a), "note": "too few"}
    diffs = [rng.choice(a.values, len(a)).mean() - rng.choice(b.values, len(b)).mean() for _ in range(2000)]
    yrs = tr.assign(m=mask).groupby("year")
    wins = sum(1 for _, g in yrs if g["m"].sum() >= 3 and (~g["m"]).sum() >= 3
               and g.loc[g["m"], "pct"].mean() > g.loc[~g["m"], "pct"].mean())
    n_years = sum(1 for _, g in yrs if g["m"].sum() >= 3 and (~g["m"]).sum() >= 3)
    return {"label": label, "n_with": len(a), "n_without": len(b),
            "mean_with_%": round(a.mean(), 3), "mean_without_%": round(b.mean(), 3),
            "diff_pts": round(a.mean() - b.mean(), 3),
            "ci95": f"{np.percentile(diffs, 2.5):+.2f}..{np.percentile(diffs, 97.5):+.2f}",
            "years_better": f"{wins}/{n_years}"}


rows = []
for name, days in [("FOMC", fomc), ("CPI", cpi), ("Jobs report", nfp)]:
    on_sig, ahead, during = tag(days)
    rows.append(compare(on_sig, f"{name} on the signal day (the dip happened on it)"))
    rows.append(compare(ahead, f"{name} between signal and entry"))
    rows.append(compare(during, f"{name} while held"))
any_macro = pd.Series(False, index=tr.index)
for days in (fomc, cpi, nfp):
    any_macro |= tag(days)[0]
rows.append(compare(any_macro, "any macro release on the signal day"))

# Earnings season: reports market-wide in the 5 sessions after the signal (2016+)
sub = tr["signal"] >= "2016-01-04"
counts = []
for s in tr["signal"]:
    k = dates.searchsorted(s)
    window = dates[k + 1:k + 6]
    counts.append(int(per_day.reindex(window).fillna(0).sum()))
tr["earn5"] = counts
q = tr.loc[sub, "earn5"].quantile([1 / 3, 2 / 3]).values
tr_all = tr
tr = tr_all[sub].copy()
rows.append(compare(tr["earn5"] >= q[1], f"earnings season: top third of reports in next 5 days (>= {q[1]:.0f})"))
rows.append(compare(tr["earn5"] <= q[0], f"earnings lull: bottom third (<= {q[0]:.0f})"))
tr = tr_all

out = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 70)
print(f"{len(tr)} trades, mean {tr['pct'].mean():+.3f}% per trade\n")
print(out.to_string(index=False))
out.to_csv("results/event_study.csv", index=False)
