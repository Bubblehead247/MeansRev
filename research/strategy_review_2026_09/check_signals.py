"""Compare the engine's per-symbol entry signals with bot.log scan lines (9/10 on)."""
import pickle
import re
from pathlib import Path

import numpy as np
import pandas as pd

import live_engine  # noqa: F401  (puts the repo root on sys.path)

prep = pickle.loads(Path("prep.pkl").read_bytes())
pat = re.compile(r"^(2026-09-\d\d) 15:3\d.*\[(\w+)\] Close=([\d.]+).*?ADXw=([\d.]+) "
                 r"regime_ok=(\w+) \| RSI2=([\d.]+).*Entry=(\w+)")
rows = []
for line in open(Path("../../bot.log"), encoding="cp1252", errors="replace"):
    m = pat.search(line)
    if m and m.group(1) >= "2026-09-10":
        rows.append(m.groups())
dates = prep["dates"]
mism = n = 0
adx_diff, rsi_diff = [], []
for d, sym, close, adxw, rok, rsi2, entry in rows:
    i = dates.searchsorted(pd.Timestamp(d))
    x = prep["sym"].get(sym)
    if x is None or i >= len(dates) or dates[i] != pd.Timestamp(d):
        continue
    n += 1
    e_adx, e_rsi, e_c, e_s2 = x["wadx"][i], x["rsi2"][i], x["c"][i], x["sma200"][i]
    adx_diff.append(abs(e_adx - float(adxw)))
    rsi_diff.append(abs(e_rsi - float(rsi2)))
    e_entry = bool((e_rsi < 10) and (e_c > e_s2) and (20 <= e_adx < 25))
    live_entry = entry == "True"
    if e_entry != live_entry or live_entry:
        tag = "MISMATCH" if e_entry != live_entry else "both"
        mism += e_entry != live_entry
        print(f"{tag:8} {d} {sym:5} live: rsi={rsi2} adx={adxw} entry={entry} | "
              f"engine: rsi={e_rsi:.2f} adx={e_adx:.2f} >sma200={e_c > e_s2} entry={e_entry}")
print(f"compared {n} symbol-days, {mism} entry mismatches; ADX |diff| median "
      f"{np.median(adx_diff):.2f} max {np.max(adx_diff):.2f}; RSI2 |diff| median "
      f"{np.median(rsi_diff):.2f} max {np.max(rsi_diff):.2f}")
