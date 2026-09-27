"""
live_engine.py — portfolio backtest of MeansRev that follows the LIVE rules.

Written for the 2026-09 strategy review. ``backtest_portfolio.py`` predates
several live changes, so its numbers do not describe the bot as it trades now.
Differences it closes (each one matches scanner.py / executor.py /
meansrev_main.py as of commit 8e82aad):

* Regime filter: weekly ADX(14) must sit in [REGIME_ADX_MIN, REGIME_ADX_MAX),
  and a symbol needs >= SMA_WEEKLY_SLOW + 10 weekly bars or the gate fails.
* Entries are DAY limits at prior close x (1 + ENTRY_LIMIT_PCT). A signal fills
  next day only if that day's low reaches the limit, at min(open, limit).
  Unfilled entries are dropped (live cancels them before the close).
* Time stop counts CALENDAR days: the scan on day d queues the exit when
  (d - entry_date).days >= MAX_HOLD_DAYS; it fills at the next open.
* Signals are taken in config.SYMBOLS order (live iterates the scan results in
  that order), not most-oversold first.
* Slots and sector caps are counted at the scan, with positions queued to exit
  tomorrow still counted as open (live does the same).

Known remaining approximations (stated in the report):

* Weekly ADX uses completed weeks only; live includes the week in progress.
* Sizing uses realized equity; live uses the account's marked-to-market equity.
* Exits fill at the open less slippage; live's opening-auction sells don't fill
  on paper, so it sells at market ~09:45. ``exit_extra_slip`` stress-tests that
  (14 live 09:45 fills averaged +0.07% vs the open, i.e. no measurable cost).
* Entries the cash cannot cover are rejected (no margin), like live.
* Data: yfinance adjusted daily bars (live uses Alpaca, which starts in 2016).
"""
from __future__ import annotations

import pickle
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config                      # noqa: E402
import sectors                     # noqa: E402
from indicators import adx, atr, rsi, sma   # noqa: E402

ORIGINAL_11 = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF", "XLK", "XLV", "XLU", "GLD", "TLT"]


# ── Data ──────────────────────────────────────────────────────────────────────

def load_bars(symbols: list[str], start: str, cache: Path) -> dict[str, pd.DataFrame]:
    """Daily adjusted OHLC per symbol from yfinance, cached to a pickle."""
    if cache.exists():
        bars = pickle.loads(cache.read_bytes())
        if all(s in bars for s in symbols):
            return bars
    import yfinance as yf
    raw = yf.download(symbols, start=start, interval="1d", auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    bars = {}
    for s in symbols:
        df = raw[s].rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
        if len(df):
            bars[s] = df
    cache.write_bytes(pickle.dumps(bars))
    return bars


def prepare(bars: dict[str, pd.DataFrame]) -> dict:
    """Align every symbol on one date index and precompute the indicators."""
    idx = sorted(set().union(*(df.index for df in bars.values())))
    idx = pd.DatetimeIndex(idx)
    out = {"dates": idx, "n": len(idx), "sym": {}}
    for s, df in bars.items():
        df = df.reindex(idx)
        c, h, l = df["close"], df["high"], df["low"]
        valid = df.dropna()
        wk = pd.DataFrame({
            "high":  valid["high"].resample("W-FRI").max(),
            "low":   valid["low"].resample("W-FRI").min(),
            "close": valid["close"].resample("W-FRI").last(),
        }).dropna()
        wk_adx = adx(wk["high"], wk["low"], wk["close"], config.REGIME_ADX_PERIOD)
        wk_count = pd.Series(np.arange(1, len(wk) + 1), index=wk.index, dtype=float)
        out["sym"][s] = {
            "o": df["open"].values, "h": h.values, "l": l.values, "c": c.values,
            "rsi2":   rsi(c.dropna(), config.RSI_PERIOD).reindex(idx).values,
            "sma200": sma(c.dropna(), config.SMA_DAILY_TREND).reindex(idx).values,
            "atr14":  atr(valid["high"], valid["low"], valid["close"],
                          config.ATR_PERIOD).reindex(idx).values,
            "wadx":   wk_adx.reindex(idx, method="ffill").values,
            "wcount": wk_count.reindex(idx, method="ffill").fillna(0).values,
            "sector": sectors.sector_of(s),
        }
    return out


# ── Engine ────────────────────────────────────────────────────────────────────

@dataclass
class Params:
    symbols: list[str]
    max_positions: int = config.MAX_POSITIONS
    max_per_sector: int = config.MAX_PER_SECTOR
    max_position_pct: float = config.MAX_POSITION_PCT
    risk_per_trade: float = config.RISK_PER_TRADE
    regime_filter: bool = config.USE_REGIME_FILTER
    adx_min: float = config.REGIME_ADX_MIN
    adx_max: float = config.REGIME_ADX_MAX
    rsi_entry: float = config.RSI_ENTRY_THRESHOLD
    rsi_exit: float = config.RSI_EXIT_THRESHOLD
    max_hold_days: int = config.MAX_HOLD_DAYS
    stop_mult: float = config.ACTIVE_STOP_MULT
    entry_limit_pct: float = config.ENTRY_LIMIT_PCT
    limit_entries: bool = True          # False = fill every signal at the open
    rank: str = "symbol_order"          # or "most_oversold"
    slip: float = config.SLIPPAGE_PCT
    exit_extra_slip: float = 0.0


def run(prep: dict, p: Params, start: str, end: str, equity0: float = 100_000.0) -> dict:
    dates = prep["dates"]
    lo = int(dates.searchsorted(pd.Timestamp(start)))
    hi = int(dates.searchsorted(pd.Timestamp(end), side="right"))
    S = {s: prep["sym"][s] for s in p.symbols if s in prep["sym"]}
    order = [s for s in p.symbols if s in S]

    equity = equity0
    positions: dict[str, dict] = {}
    pending: list[tuple] = []           # (symbol, limit, atr, rsi) signalled at the prior close
    trades, curve, invested, cashflow = [], [], [], []
    signals = unfilled = blocked = rejected_cash = 0

    for i in range(max(lo, 2), hi):
        d = dates[i]
        # 1. Entries from yesterday's scan: DAY limit at prior close x (1 + pct)
        for sym, limit, sig_atr, _ in pending:
            x = S[sym]
            op, low = x["o"][i], x["l"][i]
            if np.isnan(op) or np.isnan(low):
                continue
            if p.limit_entries:
                if low > limit:
                    unfilled += 1
                    continue
                px = min(op, limit)
            else:
                px = op * (1 + p.slip)
            stop_dist = p.stop_mult * sig_atr
            shares = int(equity * p.risk_per_trade / stop_dist)
            shares = min(shares, int(equity * p.max_position_pct / px))
            if shares <= 0:
                continue
            # No margin (account multiplier 1): Alpaca rejects an order the cash
            # cannot cover, as it did XLE on 2026-08-26. Cash = realized equity
            # less the cost of open positions.
            cash = equity - sum(q["px"] * q["shares"] for q in positions.values())
            if px * shares > cash:
                rejected_cash += 1
                continue
            positions[sym] = {"entry_i": i, "entry_date": d, "px": px,
                              "stop": px - stop_dist, "shares": shares}
        pending = []

        # 2. Exits: hard stop intraday; time/RSI exits decided at the prior scan
        for sym in list(positions):
            pos, x = positions[sym], S[sym]
            op, low = x["o"][i], x["l"][i]
            if np.isnan(op) or np.isnan(low):
                continue
            reason = px = None
            if i > pos["entry_i"] and (dates[i - 1] - pos["entry_date"]).days >= p.max_hold_days:
                reason, px = "time_stop", op * (1 - p.slip - p.exit_extra_slip)
            elif (i - 1 > pos["entry_i"] and x["rsi2"][i - 2] >= p.rsi_exit
                  and x["rsi2"][i - 1] < p.rsi_exit):
                reason, px = "rsi_exit", op * (1 - p.slip - p.exit_extra_slip)
            elif low <= pos["stop"]:
                reason, px = "hard_stop", min(pos["stop"], op) * (1 - p.slip)
            if reason:
                pnl = (px - pos["px"]) * pos["shares"]
                equity += pnl
                trades.append({"symbol": sym, "entry": str(pos["entry_date"].date()),
                               "exit": str(d.date()), "entry_px": pos["px"], "exit_px": px,
                               "shares": pos["shares"], "pnl": pnl,
                               "pct": (px / pos["px"] - 1) * 100, "reason": reason})
                del positions[sym]

        # 3. Scan at this close; entries fill at the next open
        queued, sec_count = [], {}
        for s in positions:
            sec_count[S[s]["sector"]] = sec_count.get(S[s]["sector"], 0) + 1
        cands = []
        for s in order:
            if s in positions:
                continue
            x = S[s]
            r, c, s2, a = x["rsi2"][i], x["c"][i], x["sma200"][i], x["atr14"][i]
            if np.isnan(r) or np.isnan(c) or np.isnan(s2) or np.isnan(a):
                continue
            if not (r < p.rsi_entry and c > s2):
                continue
            if p.regime_filter:
                w = x["wadx"][i]
                if x["wcount"][i] < config.SMA_WEEKLY_SLOW + 10 or np.isnan(w):
                    continue
                if not (p.adx_min <= w < p.adx_max):
                    continue
            cands.append((s, c * (1 + p.entry_limit_pct), a, r))
        if p.rank == "most_oversold":
            cands.sort(key=lambda t: t[3])
        for cand in cands:
            signals += 1
            sec = S[cand[0]]["sector"]
            if len(positions) + len(queued) >= p.max_positions or sec_count.get(sec, 0) >= p.max_per_sector:
                blocked += 1
                continue
            queued.append(cand)
            sec_count[sec] = sec_count.get(sec, 0) + 1
        pending = queued
        cost_open = sum(q["px"] * q["shares"] for q in positions.values())
        need = 0.0
        for sym, limit, sig_atr, _ in queued:
            sh = min(int(equity * p.risk_per_trade / (p.stop_mult * sig_atr)),
                     int(equity * p.max_position_pct / limit))
            need += max(sh, 0) * limit
        cashflow.append((d, equity - cost_open, need))

        # 4. Mark to market
        held = sum(S[s]["c"][i] * q["shares"] for s, q in positions.items() if not np.isnan(S[s]["c"][i]))
        cost = sum(q["px"] * q["shares"] for s, q in positions.items())
        mtm = equity + held - cost
        curve.append((d, mtm))
        invested.append(held / mtm if mtm > 0 else 0.0)

    eq = pd.Series([v for _, v in curve], index=[k for k, _ in curve])
    cash = pd.DataFrame(cashflow, columns=["date", "cash", "next_entry_cost"]).set_index("date")
    return {"trades": trades, "equity": eq, "invested": pd.Series(invested, index=eq.index),
            "cash": cash,
            "signals": signals, "unfilled": unfilled, "blocked": blocked,
            "rejected_cash": rejected_cash}


def summarize(res: dict) -> dict:
    eq, tr = res["equity"], res["trades"]
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    wins = [t for t in tr if t["pnl"] > 0]
    gl = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    return {
        "cagr_pct": round(cagr * 100, 2),
        "max_dd_pct": round(dd * 100, 1),
        "trades": len(tr),
        "win_pct": round(len(wins) / len(tr) * 100, 1) if tr else 0.0,
        "pf": round(sum(t["pnl"] for t in wins) / gl, 2) if gl else float("inf"),
        "avg_trade_pct": round(np.mean([t["pct"] for t in tr]), 2) if tr else 0.0,
        "avg_invested_pct": round(res["invested"].mean() * 100, 1),
        "days_in_cash_pct": round((res["invested"] == 0).mean() * 100, 1),
        "unfilled_entries": res["unfilled"],
        "slot_blocked": res["blocked"],
        "cash_rejected": res["rejected_cash"],
        "max_invested_pct": round(res["invested"].max() * 100, 1),
    }


def yearly_returns(eq: pd.Series) -> pd.Series:
    ye = eq.groupby(eq.index.year).last()
    first = eq.iloc[0]
    prev = ye.shift(1).fillna(first)
    return (ye / prev - 1) * 100
