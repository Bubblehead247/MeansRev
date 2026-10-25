"""review_analysis.py - 2026-10 monthly review analysis for MeansRev (read-only).

Usage:  python review_analysis.py <BARS_PICKLE> <OUT_DIR>
Inputs: bars pickle from fetch_bars.py, ../../trades.csv, ../../positions.json, ../../cash_sweep.jsonl,
        ~/.quantcore/equity/meansrev.csv (broker daily equity), ../../bot.log (logged scan signals).
Outputs: JSON/CSV files in OUT_DIR and a text summary on stdout.

Sections
  A  trade statistics (all / old config / new config)
  B  equity, drawdown, benchmarks (SGOV floor, SPY, exposure-matched SPY+SGOV)
  C  signal fidelity: recompute entry signals from bars, compare with bot.log
  D  replay of the live rules 2026-09-25..10-23: (1) with the two lost scans, (2) with every scan; difference = outage cost
  E  execution: entry/exit fills vs open and vs prior close
  F  sweep and dividends
"""
import csv
import json
import math
import pickle
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import sectors  # noqa: E402
from quantcore.indicators import atr, rsi, sma  # noqa: E402

BARS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
D = pickle.load(open(BARS, "rb"))
ADJ, RAW = D["adj"], D["raw"]
HOME = Path.home() / ".quantcore" / "equity" / "meansrev.csv"

CONFIG_CHANGE = date(2026, 9, 28)      # first live day of the 2026-09-27 config
LOST_SCANS = [date(2026, 9, 28), date(2026, 10, 14)]
RESULTS = {}


def d(s):
    return date.fromisoformat(s)


# ───────────────────────── inputs ─────────────────────────
trades = pd.read_csv(ROOT / "trades.csv")
trades["entry_date"] = trades["entry_date"].map(d)
trades["exit_date"] = trades["exit_date"].map(d)
equity = pd.read_csv(HOME)
equity["date"] = pd.to_datetime(equity["date"]).dt.date
EQ = dict(zip(equity["date"], equity["equity"]))
positions = json.load(open(ROOT / "positions.json"))
sweep = [json.loads(x) for x in open(ROOT / "cash_sweep.jsonl")]
TDAYS = [x.date() for x in ADJ["SPY"].index]


def stats(df, label):
    if df.empty:
        return {"label": label, "n": 0}
    pnl, pct = df["realized_pnl"], df["realized_pnl_pct"]
    win, loss = df[pnl > 0], df[pnl <= 0]
    gp, gl = win["realized_pnl"].sum(), -loss["realized_pnl"].sum()
    return {
        "label": label, "n": int(len(df)), "win_rate": round(len(win) / len(df), 3),
        "avg_win_pct": round(win["realized_pnl_pct"].mean(), 2) if len(win) else None,
        "avg_loss_pct": round(loss["realized_pnl_pct"].mean(), 2) if len(loss) else None,
        "payoff": round(win["realized_pnl_pct"].mean() / -loss["realized_pnl_pct"].mean(), 2) if len(win) and len(loss) else None,
        "profit_factor": round(gp / gl, 2) if gl else None,
        "expectancy_pct": round(pct.mean(), 2), "sd_pct": round(pct.std(), 2),
        "t_stat_vs_zero": round(pct.mean() / (pct.std() / math.sqrt(len(df))), 2) if len(df) > 2 else None,
        "total_pnl": round(pnl.sum(), 0),
        "avg_days_held": round(df["days_held"].mean(), 1), "worst_pct": round(pct.min(), 2),
        "worst_5pct_mean": round(pct.nsmallest(max(1, int(round(len(df) * 0.05)))).mean(), 2),
        "exits": df["exit_reason"].value_counts().to_dict(),
    }


# ───────────────────────── A. trade stats ─────────────────────────
live = trades[trades["entry_date"] >= date(2026, 6, 9)]            # signal-driven era (backfill rows excluded)
live_pre = live[live["entry_date"] < CONFIG_CHANGE]
# XLF entered 9/25 under the old config but exited 10/1; keep it in "old"
live_new = live[live["entry_date"] >= CONFIG_CHANGE]
RESULTS["A_trade_stats"] = [stats(live, "all signal-era trades (>=2026-06-09)"),
                            stats(live_pre, "old config (entered before 9/28)"),
                            stats(live_new, "new config (entered 9/28 on) - closed only")]
# win/loss by ETF family for the new config
RESULTS["A_new_config_trades"] = live_new[["symbol", "entry_date", "exit_date", "days_held", "realized_pnl", "realized_pnl_pct", "exit_reason"]].astype(str).to_dict("records")

# open positions at 10/23 (unrealized, raw closes)
last = TDAYS[-1]
open_rows = []
for s, p in positions.items():
    px = float(RAW[s]["close"].iloc[-1])
    open_rows.append({"symbol": s, "shares": p["shares"], "entry": p["entry_price"], "last": px,
                      "unrealized": round((px - p["entry_price"]) * p["shares"], 0),
                      "unrealized_pct": round((px / p["entry_price"] - 1) * 100, 2), "entry_date": p["entry_date"]})
RESULTS["A_open_positions"] = open_rows

# ───────────────────────── B. equity, exposure, benchmarks ─────────────────────────
eq = pd.Series(EQ).sort_index()
sgov_adj = ADJ["SGOV"]["close"].copy(); sgov_adj.index = [x.date() for x in sgov_adj.index]
spy_adj = ADJ["SPY"]["close"].copy(); spy_adj.index = [x.date() for x in spy_adj.index]
spy_r, sgov_r = spy_adj.pct_change(), sgov_adj.pct_change()

# positions timeline from trades.csv + open positions (held at close t if entry<=t<exit)
book = [(r.symbol, r.entry_date, r.exit_date, float(r.shares)) for r in trades.itertuples()]
book += [(s, d(p["entry_date"]), date(2100, 1, 1), float(p["shares"])) for s, p in positions.items()]
rawclose = {s: RAW[s]["close"].copy() for s in config.SYMBOLS if s in RAW}
for s in rawclose:
    rawclose[s].index = [x.date() for x in rawclose[s].index]


def invested(t):
    v = 0.0
    for s, e, x, sh in book:
        if s in rawclose and e <= t < x and t in rawclose[s].index:   # KRE/GDX (May backfill) are outside the universe
            v += sh * float(rawclose[s].loc[t])
    return v


days = [t for t in eq.index if t in spy_r.index]
expo = pd.Series({t: invested(t) / eq[t] for t in days})
sweep_value = defaultdict(float)   # SGOV position value at close t, from ledger fills
qty = 0.0
sw = sorted(sweep, key=lambda r: r["timestamp"])
for t in days:
    for r in sw:
        if r["timestamp"][:10] == str(t):
            qty += r["qty"] if r["side"] == "buy" else -r["qty"]
    sweep_value[t] = qty * float(RAW["SGOV"]["close"].loc[pd.Timestamp(t)])
RESULTS["B_exposure"] = {
    "avg_stock_exposure_since_0529": round(expo[[t for t in days if t >= date(2026, 5, 29)]].mean(), 3),
    "avg_stock_exposure_since_0928": round(expo[[t for t in days if t >= CONFIG_CHANGE]].mean(), 3),
    "avg_stock_exposure_old_0529_0925": round(expo[[t for t in days if date(2026, 5, 29) <= t <= date(2026, 9, 25)]].mean(), 3),
    "avg_sgov_share_since_0929": round(np.mean([sweep_value[t] / eq[t] for t in days if t >= date(2026, 9, 29)]), 3),
    "days_with_zero_stock_exposure_since_0529": int(sum(1 for t in days if t >= date(2026, 5, 29) and expo[t] == 0)),
}


def window(a, b, label):
    sub = [t for t in days if a <= t <= b]
    e = eq[sub]
    r = e.pct_change().dropna()
    rf = sgov_r.reindex(r.index).fillna(0)
    ex = (r - rf)
    spy = spy_r.reindex(r.index).fillna(0)
    ret = e.iloc[-1] / e.iloc[0] - 1
    # benchmarks over the same window, base = first day close
    sgov_ret = float((1 + sgov_r.reindex(r.index).fillna(0)).prod() - 1)
    spy_ret = float((1 + spy).prod() - 1)
    ex_lag = expo.shift(1).reindex(r.index).fillna(0)
    match = float((1 + ex_lag * spy + (1 - ex_lag) * sgov_r.reindex(r.index).fillna(0)).prod() - 1)
    peak = e.cummax(); dd = (e / peak - 1)
    beta = float(np.cov(r, spy)[0, 1] / np.var(spy, ddof=1)) if len(r) > 5 else None
    corr = float(np.corrcoef(r, spy)[0, 1]) if len(r) > 5 and r.std() > 0 else None
    sd = r.std()
    return {
        "label": label, "from": str(sub[0]), "to": str(sub[-1]), "trading_days": len(r),
        "total_return_pct": round(ret * 100, 2),
        "T-bill floor (SGOV total return) pct": round(sgov_ret * 100, 2),
        "SPY total return pct": round(spy_ret * 100, 2),
        "SPY at the bot's own daily exposure + SGOV for the rest pct": round(match * 100, 2),
        "ann_vol_pct": round(sd * math.sqrt(252) * 100, 2),
        "sharpe_vs_sgov": round(ex.mean() / ex.std() * math.sqrt(252), 2) if ex.std() > 0 else None,
        "max_dd_pct": round(dd.min() * 100, 2), "current_dd_pct": round(dd.iloc[-1] * 100, 2),
        "beta_spy": round(beta, 3) if beta is not None else None, "corr_spy": round(corr, 3) if corr is not None else None,
        "worst_day_pct": round(r.min() * 100, 2), "best_day_pct": round(r.max() * 100, 2),
        "zero_return_days": int((r.abs() < 1e-9).sum()),
    }


RESULTS["B_windows"] = [
    window(date(2026, 4, 9), last, "since inception (4/9)"),
    window(date(2026, 5, 29), last, "since 5/29 (first full month of signal-era trading)"),
    window(date(2026, 5, 29), date(2026, 9, 25), "old config 5/29-9/25"),
    window(date(2026, 9, 25), last, "new config 9/25 close - 10/23"),
]
peak_date = eq.idxmax()
RESULTS["B_peak"] = {"peak_equity": float(eq.max()), "peak_date": str(peak_date), "last_equity": float(eq.iloc[-1]),
                     "calendar_days_under_water": (last - peak_date).days}
# long-run 'cash-only' check: first-order estimate of yield forgone while the book was in zero-yield cash (pre-9/29)
idle = [(t, 1 - expo[t]) for t in days if date(2026, 5, 29) <= t < date(2026, 9, 29)]
sgov_ann = float((1 + sgov_r.loc[date(2026, 6, 1):date(2026, 9, 28)]).prod() - 1)
RESULTS["B_forgone_tbill_yield_pre_sweep"] = {
    "avg_idle_share": round(np.mean([x for _, x in idle]), 3),
    "sgov_return_6/1-9/28_pct": round(sgov_ann * 100, 2),
    "approx_forgone_usd": round(np.mean([x for _, x in idle]) * 105000 * sgov_ann, 0),
}

# ───────────────────────── C. signal fidelity ─────────────────────────
def signals_on(t):
    out = []
    for s in config.SYMBOLS:
        df = ADJ[s]; df = df[df.index <= pd.Timestamp(t)]
        if len(df) < 210 or df.index[-1].date() != t:
            continue
        r2 = rsi(df["close"], 2).iloc[-1]; s200 = sma(df["close"], 200).iloc[-1]
        if r2 < config.RSI_ENTRY_THRESHOLD and df["close"].iloc[-1] > s200:
            out.append((s, round(float(r2), 2)))
    return out


logged = defaultdict(list)
scan_days_logged = set()
for line in open(ROOT / "bot.log", encoding="utf-8", errors="replace"):
    if line[:10] < "2026-09-25":
        continue
    m = re.search(r"\[(\w+)\] Close=.*?RSI2=([\d.]+).*Entry=(\w+)", line)
    if m:
        scan_days_logged.add(line[:10])
        if m.group(3) == "True":
            logged[line[:10]].append(m.group(1))
fid = []
for t in [x for x in TDAYS if date(2026, 9, 25) <= x <= last]:
    mine = [s for s, _ in signals_on(t)]
    lg = sorted(logged.get(str(t), []))
    fid.append({"date": str(t), "scan_logged": str(t) in scan_days_logged, "recomputed": sorted(mine), "bot_log": lg,
                "match": (sorted(mine) == lg) if str(t) in scan_days_logged else None})
RESULTS["C_signal_fidelity"] = fid

# ───────────────────────── D. replay of the live rules ─────────────────────────
def prev_trading_day(t):
    i = TDAYS.index(t); return TDAYS[i - 1]


def replay(skip_scans, sector_cap=None):
    sector_cap = config.MAX_PER_SECTOR if sector_cap is None else sector_cap
    pos = {}           # sym -> dict
    closed, pend = [], []
    start_state = {"XLF": {"entry_date": date(2026, 9, 25), "entry": 54.71, "shares": 385, "stop": 52.91}}
    pos.update(start_state)
    for t in [x for x in TDAYS if CONFIG_CHANGE <= x <= last]:
        o = lambda s: float(RAW[s]["open"].loc[pd.Timestamp(t)])
        lo = lambda s: float(RAW[s]["low"].loc[pd.Timestamp(t)])
        # morning: exits then entries from the previous scan's queue
        for p in pend:
            s = p["symbol"]
            if p["action"] == "EXIT" and s in pos:
                px = o(s); q = pos.pop(s)
                closed.append({"symbol": s, "entry_date": str(q["entry_date"]), "exit_date": str(t), "entry": q["entry"], "exit": px,
                               "shares": q["shares"], "pnl": round((px - q["entry"]) * q["shares"], 2), "reason": p["reason"]})
            elif p["action"] == "ENTRY" and s not in pos:
                prev_close_raw = float(RAW[s]["close"].loc[pd.Timestamp(prev_trading_day(t))])
                limit = prev_close_raw * (1 + config.ENTRY_LIMIT_PCT)
                # DAY limit order: fills at the open when the open is at or under the limit, otherwise at the
                # limit if the day's low reaches it (XOP 2026-09-30 filled at its limit 34 minutes after the open)
                if o(s) <= limit or lo(s) <= limit:
                    eqv = EQ[prev_trading_day(t)]
                    stop_dist = config.ACTIVE_STOP_MULT * p["atr"]
                    sh = min(math.floor(eqv * config.RISK_PER_TRADE / stop_dist), math.floor(eqv * config.MAX_POSITION_PCT / prev_close_raw))
                    px = o(s) if o(s) <= limit else limit
                    pos[s] = {"entry_date": t, "entry": px, "shares": sh, "stop": px - stop_dist}
        pend = []
        # intraday stops
        for s in list(pos):
            if pos[s]["entry_date"] <= t and lo(s) <= pos[s]["stop"] and not (pos[s]["entry_date"] == date(2026, 9, 25) and t == date(2026, 9, 25)):
                q = pos.pop(s); px = min(o(s), pos[s]["stop"]) if s in pos else min(o(s), q["stop"])
                closed.append({"symbol": s, "entry_date": str(q["entry_date"]), "exit_date": str(t), "entry": q["entry"], "exit": px,
                               "shares": q["shares"], "pnl": round((px - q["entry"]) * q["shares"], 2), "reason": "hard_stop"})
        if t in skip_scans:
            continue
        # evening scan
        n_queued = 0
        sec_q = defaultdict(int)
        for s in config.SYMBOLS:
            df = ADJ[s]; df = df[df.index <= pd.Timestamp(t)]
            if len(df) < 210 or df.index[-1].date() != t:
                continue
            r = rsi(df["close"], 2); r2, pr = float(r.iloc[-1]), float(r.iloc[-2])
            s200 = float(sma(df["close"], 200).iloc[-1]); a14 = float(atr(df["high"], df["low"], df["close"], 14).iloc[-1])
            c = float(df["close"].iloc[-1])
            if s in pos:
                if (t - pos[s]["entry_date"]).days >= config.MAX_HOLD_DAYS:
                    pend.append({"symbol": s, "action": "EXIT", "reason": "time_stop"})
                elif pr >= config.RSI_EXIT_THRESHOLD and r2 < config.RSI_EXIT_THRESHOLD:
                    pend.append({"symbol": s, "action": "EXIT", "reason": "rsi_exit"})
            elif r2 < config.RSI_ENTRY_THRESHOLD and c > s200:
                sec = sectors.sector_of(s)
                sec_count = sum(1 for x in pos if sectors.sector_of(x) == sec) + sec_q[sec]
                if len(pos) + n_queued >= config.MAX_POSITIONS or sec_count >= sector_cap:
                    continue
                pend.append({"symbol": s, "action": "ENTRY", "atr": a14}); n_queued += 1; sec_q[sec] += 1
    mtm = 0.0
    for s, q in pos.items():
        mtm += (float(RAW[s]["close"].iloc[-1]) - q["entry"]) * q["shares"]
    return {"closed": closed, "open": {s: {**q, "entry_date": str(q["entry_date"])} for s, q in pos.items()},
            "realized": round(sum(c["pnl"] for c in closed), 0), "open_mtm": round(mtm, 0)}


rep_actual = replay(set(LOST_SCANS))
rep_full = replay(set())
RESULTS["D_replay_with_lost_scans"] = rep_actual
RESULTS["D_replay_all_scans"] = rep_full
RESULTS["D_outage_cost_usd"] = {
    "realized_diff": rep_full["realized"] - rep_actual["realized"],
    "mtm_diff": rep_full["open_mtm"] - rep_actual["open_mtm"],
    "total_diff": (rep_full["realized"] + rep_full["open_mtm"]) - (rep_actual["realized"] + rep_actual["open_mtm"]),
    "note": "difference in 9/28-10/23 P&L (realized + open MTM at 10/23 close) between a replay that runs every scan and one that skips 9/28 and 10/14",
}
rep_nocap = replay(set(LOST_SCANS), sector_cap=99)
RESULTS["D_sector_cap_effect"] = {
    "note": "same lost-scan replay with the 2-per-sector cap removed (signals it blocked: RSP 9/30, EFA 10/1 per bot.log)",
    "with_cap": {"realized": rep_actual["realized"], "open_mtm": rep_actual["open_mtm"]},
    "no_cap": {"realized": rep_nocap["realized"], "open_mtm": rep_nocap["open_mtm"], "trades": [(c["symbol"], c["entry_date"], c["exit_date"], c["pnl"]) for c in rep_nocap["closed"]
               if c["symbol"] not in {x["symbol"] for x in rep_actual["closed"]}],
               "open": list(rep_nocap["open"])},
}
RESULTS["D_signals_on_lost_days"] = {str(t): signals_on(t) for t in LOST_SCANS}
actual_closed = live[(live["exit_date"] >= CONFIG_CHANGE)]
RESULTS["D_actual_pnl_since_0928"] = {
    "realized": round(float(actual_closed["realized_pnl"].sum()), 0),
    "open_mtm": round(sum(x["unrealized"] for x in open_rows), 0),
}

# ───────────────────────── E. execution ─────────────────────────
rows = []
for r in live.itertuples():
    for side, dt_, px in (("buy", r.entry_date, r.entry_price), ("sell", r.exit_date, r.exit_price)):
        if r.exit_reason in ("hard_stop", "closed_externally") and side == "sell":
            continue                      # stop fills are not a market-order decision
        ts = pd.Timestamp(dt_)
        if ts not in RAW[r.symbol].index:
            continue
        o_, pc = float(RAW[r.symbol].loc[ts, "open"]), float(RAW[r.symbol]["close"].shift(1).loc[ts])
        rows.append({"symbol": r.symbol, "side": side, "date": str(dt_), "era": "new" if dt_ >= CONFIG_CHANGE else "old", "fill": px, "open": o_, "prev_close": pc,
                     "vs_open_bps": round((px / o_ - 1) * 1e4 * (1 if side == "buy" else -1), 1),
                     "vs_prev_close_bps": round((px / pc - 1) * 1e4 * (1 if side == "buy" else -1), 1),
                     "exit_reason": r.exit_reason if side == "sell" else ""})
for s, p in positions.items():
    ts = pd.Timestamp(p["entry_date"]); o_ = float(RAW[s].loc[ts, "open"]); pc = float(RAW[s]["close"].shift(1).loc[ts])
    rows.append({"symbol": s, "side": "buy", "date": p["entry_date"], "era": "new", "fill": p["entry_price"], "open": o_, "prev_close": pc,
                 "vs_open_bps": round((p["entry_price"] / o_ - 1) * 1e4, 1), "vs_prev_close_bps": round((p["entry_price"] / pc - 1) * 1e4, 1),
                 "exit_reason": ""})
ex = pd.DataFrame(rows)
ex.to_csv(OUT / "execution_fills.csv", index=False)
RESULTS["E_execution"] = {
    "buys_n": int((ex.side == "buy").sum()), "sells_n": int((ex.side == "sell").sum()),
    "buys_vs_open_bps_mean": round(ex[ex.side == "buy"].vs_open_bps.mean(), 1),
    "buys_vs_prev_close_bps_mean": round(ex[ex.side == "buy"].vs_prev_close_bps.mean(), 1),
    "sells_vs_open_bps_mean": round(ex[ex.side == "sell"].vs_open_bps.mean(), 1),
    "sells_vs_open_bps_median": round(ex[ex.side == "sell"].vs_open_bps.median(), 1),
    "sells_vs_prev_close_bps_mean": round(ex[ex.side == "sell"].vs_prev_close_bps.mean(), 1),
    "sells_vs_open_bps_sd": round(ex[ex.side == "sell"].vs_open_bps.std(), 1),
    "buys_vs_open_bps_sd": round(ex[ex.side == "buy"].vs_open_bps.std(), 1),
    "by_era": ex.groupby(["era", "side"]).vs_open_bps.agg(["count", "mean", "median"]).round(1).reset_index().to_dict("records"),
    "note": "positive = worse for us (paid more / received less). Exits are the 9:45 ET market fallback after the opening-auction limit sell fails to fill on paper.",
}

# ───────────────────────── F. sweep and dividends ─────────────────────────
sg = pd.DataFrame({"adj": ADJ["SGOV"]["close"], "raw": RAW["SGOV"]["close"]})
sg["div_ps"] = sg["raw"].shift(1) * (sg["adj"].pct_change() - sg["raw"].pct_change())
divs = sg[sg["div_ps"] > 0.01]["div_ps"].loc["2026-06-01":]
RESULTS["F_sgov_ex_dates"] = {str(k.date()): round(float(v), 4) for k, v in divs.items()}
# validate the method on DIA (broker paid 1.19242/share, rec date 2026-09-18)
dia = pd.DataFrame({"adj": ADJ["DIA"]["close"], "raw": RAW["DIA"]["close"]})
dia["div_ps"] = dia["raw"].shift(1) * (dia["adj"].pct_change() - dia["raw"].pct_change())
RESULTS["F_method_check_DIA"] = {str(k.date()): round(float(v), 4) for k, v in dia[dia["div_ps"] > 0.05]["div_ps"].loc["2026-08-01":].items()}
# SGOV P&L from the ledger: FIFO-free average cost
qty = cost = real = 0.0
for r in sw:
    if r["side"] == "buy":
        qty += r["qty"]; cost += r["qty"] * r["price"]
    else:
        avg = cost / qty; real += (r["price"] - avg) * r["qty"]; cost -= avg * r["qty"]; qty -= r["qty"]
px = float(RAW["SGOV"]["close"].iloc[-1])
RESULTS["F_sweep"] = {"holding_qty": round(qty, 4), "avg_cost": round(cost / qty, 4), "last": px,
                      "realized_trading_pnl": round(real, 2), "unrealized_pnl": round((px - cost / qty) * qty, 2)}
exp_div = []
held = 0.0
for ex_date, dps in RESULTS["F_sgov_ex_dates"].items():
    exd = d(ex_date)
    q = 0.0
    for r in sw:
        if d(r["timestamp"][:10]) < exd:               # held at the close before ex-date
            q += r["qty"] if r["side"] == "buy" else -r["qty"]
    if q > 0:
        exp_div.append({"ex_date": ex_date, "per_share": dps, "shares_held": round(q, 2), "expected_usd": round(q * dps, 2)})
RESULTS["F_sgov_expected_dividends"] = exp_div


# ───────────────────────── G. equity vs ledger reconciliation (9/25 close -> 10/23 close) ─────────────────────────
xlf_close_0925 = float(RAW["XLF"]["close"].loc[pd.Timestamp("2026-09-25")])
fees_since_0925 = 4.75          # sum of FEE activities 9/26-10/23 from the broker (REG/TAF/CAT)
dia_div = 48.89                 # broker DIV activity 2026-10-13 (DIA, rec date 9/18)
ledger = {
    "XLF 9/25 close mark -> 10/1 stop (385 sh)": round((52.91 - xlf_close_0925) * 385, 2),
    "closed trades entered 9/28 on (trades.csv)": round(float(live_new["realized_pnl"].sum()), 2),
    "open EEM + DBMF mark at 10/23 close": round(sum(x["unrealized"] for x in open_rows), 2),
    "SGOV realized + unrealized (broker avg cost)": round(-39.33 + 55.24, 2),
    "DIA dividend credited 10/13": dia_div,
    "fees (REG/TAF/CAT)": -fees_since_0925,
}
ledger_total = sum(ledger.values())
eq_change = EQ[last] - EQ[date(2026, 9, 25)]
RESULTS["G_reconciliation_0925_1023"] = {"components": ledger, "ledger_total": round(ledger_total, 2),
                                         "broker_equity_change": round(eq_change, 2), "unexplained": round(eq_change - ledger_total, 2),
                                         "note": "the DIA 9/25 sale (-$135.67) happened before the 9/25 close so is excluded"}

# ───────────────────────── H. did the ETF picks add anything over SPY on the same trades? ─────────────────────────
rowsH = []
for r in live.itertuples():
    if r.exit_reason == "closed_externally" or r.entry_date == r.exit_date:
        continue
    try:
        a = float(RAW["SPY"]["open"].loc[pd.Timestamp(r.entry_date)]); b = float(RAW["SPY"]["open"].loc[pd.Timestamp(r.exit_date)])
    except KeyError:
        continue
    notional = r.entry_price * r.shares
    rowsH.append({"symbol": r.symbol, "era": "new" if r.entry_date >= CONFIG_CHANGE else "old", "notional": notional, "pnl": r.realized_pnl,
                  "spy_same_window_pnl": notional * (b / a - 1)})
for x in open_rows:
    a = float(RAW["SPY"]["open"].loc[pd.Timestamp(x["entry_date"])]); b = float(RAW["SPY"]["close"].iloc[-1])
    n_ = x["entry"] * x["shares"]
    rowsH.append({"symbol": x["symbol"], "era": "new", "notional": n_, "pnl": x["unrealized"], "spy_same_window_pnl": n_ * (b / a - 1)})
H = pd.DataFrame(rowsH)
H.to_csv(OUT / "selection_vs_spy.csv", index=False)
RESULTS["H_picks_vs_spy_same_windows"] = {
    "note": "dollars the same notional would have made in SPY over each trade's own entry->exit window (open to open; open positions marked at 10/23 close)",
    "all": {"n": int(len(H)), "bot_pnl": round(H.pnl.sum(), 0), "spy_same_window_pnl": round(H.spy_same_window_pnl.sum(), 0),
            "excess": round((H.pnl - H.spy_same_window_pnl).sum(), 0)},
    "by_era": H.groupby("era").agg(n=("pnl", "size"), bot_pnl=("pnl", "sum"), spy=("spy_same_window_pnl", "sum")).round(0).reset_index().to_dict("records"),
    "trades_where_pick_beat_spy": int((H.pnl > H.spy_same_window_pnl).sum()),
}

json.dump(RESULTS, open(OUT / "review_results.json", "w"), indent=1, default=str)
print(json.dumps(RESULTS, indent=1, default=str))
