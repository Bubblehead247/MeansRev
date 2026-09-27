"""Per-ticker results under the live config, yfinance 2011-2026 and Alpaca 2020-2026."""
import pickle
from pathlib import Path
import pandas as pd
import live_engine as le, config, sectors

C = le.Params(symbols=list(config.SYMBOLS))
out = {}
for lab, f, a in [("yf", "prep.pkl", "2011-01-03"), ("al", "prep_alpaca.pkl", "2020-01-02")]:
    tr = pd.DataFrame(le.run(pickle.loads(Path(f).read_bytes()), C, a, "2026-09-25")["trades"])
    tr["half"] = (tr["entry"] >= "2019-01-01").map({False: "11-18", True: "19-26"})
    out[lab] = tr
yf, al = out["yf"], out["al"]
g = yf.groupby("symbol")
tab = pd.DataFrame({
    "group": [sectors.sector_of(s) for s in g.size().index],
    "trades": g.size(), "win %": (g["pnl"].apply(lambda x: (x > 0).mean()) * 100).round(0),
    "avg trade %": g["pct"].mean().round(2),
    "avg 2011-18 %": yf[yf.half == "11-18"].groupby("symbol")["pct"].mean().round(2),
    "avg 2019-26 %": yf[yf.half == "19-26"].groupby("symbol")["pct"].mean().round(2),
    "avg Alpaca 20-26 %": al.groupby("symbol")["pct"].mean().round(2),
    "total $ (yf)": g["pnl"].sum().round(0),
}).sort_values("avg trade %", ascending=False)
tab["list order"] = [config.SYMBOLS.index(s) + 1 for s in tab.index]
pd.set_option("display.width", 250)
print(tab.to_string())
both = tab[(tab["avg 2011-18 %"] > 0) & (tab["avg 2019-26 %"] > 0) & (tab["avg Alpaca 20-26 %"] > 0)]
print(f"\npositive in both halves AND on Alpaca: {len(both)}/{len(tab)} -> {list(both.index)}")
tab.to_csv("results/ticker_table.csv")
