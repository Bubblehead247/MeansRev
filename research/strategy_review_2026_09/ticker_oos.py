"""Do per-ticker results persist? Choose on 2011-18, test on 2019-26 + Alpaca 2020-26."""
import pickle
from dataclasses import replace
from pathlib import Path
import pandas as pd
import live_engine as le, config

yf = pickle.loads(Path("prep.pkl").read_bytes()); al = pickle.loads(Path("prep_alpaca.pkl").read_bytes())
C = le.Params(symbols=list(config.SYMBOLS))
first = pd.DataFrame(le.run(yf, C, "2011-01-03", "2018-12-31")["trades"])
avg = first.groupby("symbol")["pct"].mean()
losers = sorted(avg[avg < 0].index)                       # chosen on 2011-18 only
ranked = list(avg.sort_values(ascending=False).index)
ranked += [s for s in config.SYMBOLS if s not in ranked]   # symbols with no 2011-18 trades go last
print("losers chosen on 2011-18:", losers)
variants = {
    "live list": C,
    "drop 2011-18 losers": replace(C, symbols=[s for s in config.SYMBOLS if s not in losers]),
    "demote 2011-18 losers to the end": replace(C, symbols=[s for s in config.SYMBOLS if s not in losers] + losers),
    "order by 2011-18 avg trade": replace(C, symbols=ranked),
}
rows = {}
base = {}
for n, p in variants.items():
    r1 = le.run(yf, p, "2019-01-02", "2026-09-25"); r2 = le.run(al, p, "2020-01-02", "2026-09-25")
    y1, y2 = le.yearly_returns(r1["equity"]), le.yearly_returns(r2["equity"])
    if n == "live list": base = {"y1": y1, "y2": y2}
    rows[n] = {"CAGR 2019-26 (yf)": le.summarize(r1)["cagr_pct"], "DD": le.summarize(r1)["max_dd_pct"],
               "CAGR Alpaca 2020-26": le.summarize(r2)["cagr_pct"],
               "yrs beat live": "-" if n == "live list" else
               f"{int(((y1 - base['y1']) > 0.005).sum())}/{len(y1)} yf, {int(((y2 - base['y2']) > 0.005).sum())}/{len(y2)} Alpaca"}
tab = pd.DataFrame(rows).T
print(tab.to_string())
tab.to_csv("results/ticker_oos.csv")
