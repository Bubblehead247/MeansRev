"""fetch_bars.py - pull daily SIP bars (adjusted and raw) for the MeansRev universe, SGOV and SPY.

Read-only: market data only, no orders. Credentials come from alpaca.env and are never printed.
Usage:  python fetch_bars.py <OUT_PICKLE>
The pickle (not committed) holds {"adj": {sym: DataFrame}, "raw": {sym: DataFrame}} indexed by ET date.
"""
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402  (constants only; no logging handlers are attached on import)

START = datetime(2025, 5, 1, tzinfo=timezone.utc)


def fetch(client, symbols, adjustment):
    end = datetime.now(timezone.utc) - timedelta(minutes=16)  # free plan refuses newer SIP
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=START, end=end,
                           feed=DataFeed.SIP, adjustment=adjustment)
    df = client.get_stock_bars(req).df
    out = {}
    for sym in symbols:
        d = df.xs(sym, level="symbol").sort_index().copy()
        d.index = d.index.tz_convert("America/New_York").normalize().tz_localize(None)
        out[sym] = d
    return out


def main(out_path):
    env = dotenv_values(ROOT / "alpaca.env")
    client = StockHistoricalDataClient(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])
    symbols = list(config.SYMBOLS) + ["SGOV"]
    data = {"adj": fetch(client, symbols, Adjustment.ALL), "raw": fetch(client, symbols, Adjustment.RAW),
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump(data, f)
    last = {s: str(d.index[-1].date()) for s, d in data["adj"].items()}
    print("symbols", len(symbols), "last bar dates:", sorted(set(last.values())))


if __name__ == "__main__":
    main(sys.argv[1])
