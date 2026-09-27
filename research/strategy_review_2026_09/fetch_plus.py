"""Bars for 10 extra ETFs (chosen before looking at results), yfinance + Alpaca SIP."""
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

import live_engine  # noqa: F401  (puts the repo root on sys.path)
import config
import yfinance as yf
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

NEW = ["IEF", "LQD", "HYG", "SMH", "KRE", "ITB", "GDX", "EWJ", "EWZ", "FXI"]
bars = pickle.loads(Path("bars_yf.pkl").read_bytes())
raw = yf.download(NEW, start="2005-01-01", interval="1d", auto_adjust=True, progress=False,
                  threads=True, group_by="ticker")
for s in NEW:
    bars[s] = raw[s].rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    print(s, bars[s].index[0].date(), len(bars[s]))
Path("bars_yf_plus.pkl").write_bytes(pickle.dumps(bars))

cl = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
df = cl.get_stock_bars(StockBarsRequest(
    symbol_or_symbols=NEW, timeframe=TimeFrame.Day, start=datetime(2016, 1, 1, tzinfo=timezone.utc),
    end=datetime.now(timezone.utc) - timedelta(minutes=16), feed=DataFeed.SIP,
    adjustment=Adjustment.ALL)).df
ab = pickle.loads(Path("bars_alpaca.pkl").read_bytes())
for s in NEW:
    d = df.xs(s, level="symbol")[["open", "high", "low", "close"]].copy()
    d.index = d.index.tz_convert("America/New_York").tz_localize(None).normalize()
    lo, hi = d[["open", "close"]].min(axis=1), d[["open", "close"]].max(axis=1)
    d.loc[d["low"] < 0.5 * lo, "low"] = lo
    d.loc[d["high"] > 2 * hi, "high"] = hi
    ab[s] = d
Path("bars_alpaca_plus.pkl").write_bytes(pickle.dumps(ab))
print("saved")
