"""Deep dive, wave 1: one lever at a time on the live config, both datasets.

Wins = calendar years beating the live config (yfinance 16, Alpaca 7), plus
the two yfinance halves as a regime-split check.
"""
import pickle, sys
from dataclasses import replace
from pathlib import Path
import pandas as pd
import live_engine as le, config

yf = pickle.loads(Path("prep.pkl").read_bytes()); al = pickle.loads(Path("prep_alpaca.pkl").read_bytes())
C = le.Params(symbols=list(config.SYMBOLS))
V = {
    "live config": C,
    # exits
    "exit: first close RSI(2) > 70": replace(C, exit_mode="rsi_above", rsi_exit=70),
    "exit: first close RSI(2) > 50": replace(C, exit_mode="rsi_above", rsi_exit=50),
    "exit: close > 5-day avg": replace(C, exit_mode="close_above_sma5"),
    "exit: close > 10-day avg": replace(C, exit_mode="close_above_sma10"),
    "exit: close > prior day's high": replace(C, exit_mode="higher_close"),
    # entry price
    "entry limit: close - 0.5 ATR": replace(C, entry_limit_atr=0.5),
    "entry limit: close - 1.0 ATR": replace(C, entry_limit_atr=1.0),
    "entry limit: close - 1%": replace(C, entry_limit_pct=-0.01),
    "entry limit: close - 2%": replace(C, entry_limit_pct=-0.02),
    # stops / trend / timing
    "no hard stop": replace(C, hard_stop=False),
    "trend: SMA100": replace(C, trend="sma100"),
    "trend: none": replace(C, trend="none"),
    "trade at the signal close": replace(C, on_close=True),
    # wave 2
    "hold: RSI falls back below 60": replace(C, rsi_exit=60),
    "hold: RSI falls back below 80": replace(C, rsi_exit=80),
    "hold: RSI falls back below 90": replace(C, rsi_exit=90),
    "hold: time stop only (10 d)": replace(C, exit_mode="none"),
    "trend: SPY > its 200-day (market)": replace(C, trend="spy"),
    "trend: own AND SPY > 200-day": replace(C, trend="own_and_spy"),
    "trend: none, 12% cap": replace(C, trend="none", max_position_pct=0.12),
    "scale-in: 2nd unit on a lower close": replace(C, scale_in=True),
    "8 positions x 12%": replace(C, max_positions=8, max_position_pct=0.12),
}
if len(sys.argv) > 1:
    V = {k: v for k, v in V.items() if k == "live config" or any(a in k for a in sys.argv[1:])}
res = {}
for n, p in V.items():
    for lab, prep, a, b in [("yf", yf, "2011-01-03", "2026-09-25"), ("h1", yf, "2011-01-03", "2018-12-31"),
                            ("h2", yf, "2019-01-02", "2026-09-25"), ("al", al, "2020-01-02", "2026-09-25")]:
        r = le.run(prep, p, a, b); res[(n, lab)] = (le.summarize(r), le.yearly_returns(r["equity"]))
rows = {}
for n in V:
    s = res[(n, "yf")][0]
    row = {"CAGR 11-26": s["cagr_pct"], "11-18": res[(n, "h1")][0]["cagr_pct"], "19-26": res[(n, "h2")][0]["cagr_pct"],
           "Alpaca 20-26": res[(n, "al")][0]["cagr_pct"], "DD": s["max_dd_pct"], "trades": s["trades"],
           "avg trade %": s["avg_trade_pct"], "win %": s["win_pct"], "invested %": s["avg_invested_pct"]}
    for lab in ("yf", "al"):
        d = (res[(n, lab)][1] - res[("live config", lab)][1]).dropna()
        row[f"yrs beat {lab}"] = "-" if n == "live config" else f"{int((d > 0.005).sum())}/{len(d)}"
    rows[n] = row
tab = pd.DataFrame(rows).T
pd.set_option("display.width", 260)
print(tab.to_string())
tab.to_csv("results/sweep_deep_wave1.csv")
