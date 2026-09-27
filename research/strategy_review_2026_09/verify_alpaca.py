"""Re-run option C against the live config on Alpaca SIP adjusted bars (2016-).

The weekly ADX needs 210 completed weeks before the regime gate can pass, so on
data starting 2016-01 the live-config baseline can only trade from ~2020. The
comparison is therefore reported for 2020-2026 (both variants fully warmed up),
plus 2017-2026 for option C alone.
"""
import pickle
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import live_engine as le
import config
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

cache = Path("bars_alpaca.pkl")
if cache.exists():
    bars = pickle.loads(cache.read_bytes())
else:
    cl = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
    df = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=list(config.SYMBOLS), timeframe=TimeFrame.Day,
        start=datetime(2016, 1, 1, tzinfo=timezone.utc),
        end=datetime.now(timezone.utc) - timedelta(minutes=16),
        feed=DataFeed.SIP, adjustment=Adjustment.ALL)).df
    bars = {}
    for s in config.SYMBOLS:
        d = df.xs(s, level="symbol")[["open", "high", "low", "close"]].copy()
        d.index = d.index.tz_convert("America/New_York").tz_localize(None).normalize()
        # same bad-print guard as scanner.py
        lo, hi = d[["open", "close"]].min(axis=1), d[["open", "close"]].max(axis=1)
        d.loc[d["low"] < 0.5 * lo, "low"] = lo
        d.loc[d["high"] > 2 * hi, "high"] = hi
        bars[s] = d
    cache.write_bytes(pickle.dumps(bars))
print({s: str(b.index[0].date()) for s, b in bars.items() if b.index[0].year > 2016})
prep = le.prepare(bars)

base = le.Params(symbols=list(config.SYMBOLS))
C = replace(base, regime_filter=False, max_positions=6, max_position_pct=0.15)
rows, yearly = {}, {}
for name, p, start in [("live config", base, "2020-01-02"), ("option C", C, "2020-01-02"),
                       ("option C (2017-)", C, "2017-01-03")]:
    res = le.run(prep, p, start, "2026-09-25")
    s = le.summarize(res)
    s["cagr_exit+0.10%"] = le.summarize(le.run(prep, replace(p, exit_extra_slip=0.001), start, "2026-09-25"))["cagr_pct"]
    rows[name] = s
    yearly[name] = le.yearly_returns(res["equity"])
yr = pd.DataFrame(yearly)
d = (yr["option C"] - yr["live config"]).dropna()
rows["option C"]["yrs_beat_live"] = f"{int((d > 0.005).sum())}/{len(d)}"
cols = ["cagr_pct", "cagr_exit+0.10%", "max_dd_pct", "pf", "avg_trade_pct", "trades",
        "avg_invested_pct", "max_invested_pct", "days_in_cash_pct", "cash_rejected", "yrs_beat_live"]
tab = pd.DataFrame(rows).T.reindex(columns=cols)
pd.set_option("display.width", 250)
print(tab.to_string()); print(); print(yr.round(1).to_string())
tab.to_csv("results/verify_alpaca.csv"); yr.round(2).to_csv("results/verify_alpaca_yearly.csv")
