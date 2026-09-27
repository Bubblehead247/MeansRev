"""
scanner.py — Fetches daily and weekly OHLCV bars and evaluates entry/exit signals.

Entry (ALL must pass):
  Weekly gate:   Close > SMA(50,W)  AND  Close > SMA(200,W)
                 (only enforced when config.USE_TREND_FILTER is True — OFF)
  Daily trend:   Close > SMA(200,D)  — Connors regime gate
                 (only enforced when config.USE_DAILY_SMA200_FILTER is True)
  RSI signal:    config.ENTRY_MODE — "oversold" = RSI(2) < 10 (Connors, buy the
                 dip); "crossback" = RSI(2) crossed back above 10 (legacy)
  Volume:        Optional — today's volume > 1.5× the 20-day average
                 (only enforced when config.USE_VOLUME_FILTER is True)
  Regime:        Optional — weekly ADX in [REGIME_ADX_MIN, REGIME_ADX_MAX)
                 (only enforced when config.USE_REGIME_FILTER is True)

Exit (first triggered wins, checked in this priority order):
  Weekly break:  Close < SMA(200,W)  — structural trend broken        [High]
  RSI target:    RSI(2) crossed back BELOW 70 (prev >= 70, now < 70)  [Standard]
  Time stop:     Position held >= 7 calendar days                     [Standard] (checked in meansrev_main.py)
  Hard stop:     ATR-based GTC order on exchange                      [Highest]  (handled by Alpaca)
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

import config
from indicators import sma, rsi, atr, adx

logger = logging.getLogger(__name__)

_data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)


# ── Data Fetching ─────────────────────────────────────────────────────────────

#: The free data plan serves consolidated (SIP) bars only when the request ends
#: at least 15 minutes ago. The scan runs at 16:30 ET, so the day's bar is in.
_SIP_DELAY = timedelta(minutes=16)

_ET = ZoneInfo("America/New_York")


def _fetch_bars(symbol: str, timeframe: TimeFrame, calendar_days_back: int) -> pd.DataFrame:
    """Bars from the consolidated (SIP) feed, adjusted for splits AND dividends.

    Until 2026-09-27 this asked for the IEX feed with no adjustment. Unadjusted
    bars read an ex-dividend drop as a price fall: XLF went ex on 2026-09-21 and
    RSI(2) on 9/22 read 0.88 instead of 8.54 — a false "oversold". IEX-only
    highs and lows also moved the weekly ADX a median of ~3 points off the
    backtest's, on a regime band only 5 points wide. The backtests use
    consolidated, dividend-adjusted data; this now matches them. The latest
    bar's close is the same either way, so entry limits and stops don't move.
    """
    end   = datetime.now(timezone.utc) - _SIP_DELAY
    start = end - timedelta(days=calendar_days_back)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=DataFeed.SIP,
        adjustment=Adjustment.ALL,
    )
    bars = _data_client.get_stock_bars(request).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")
    return _clip_bad_prints(symbol, bars.sort_index())


def _clip_bad_prints(symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
    """Pull an impossible high or low back to the bar's open/close.

    The consolidated feed kept one misplaced-decimal print: SPY on 2026-02-02
    shows a low of 68.47 with an open of 684.20 and a close of 689.99. Through
    ATR and Wilder smoothing that single tick held SPY's weekly ADX near 40
    (instead of ~13) seven months later. A low under half of min(open, close),
    or a high over twice max(open, close), is treated as a bad print. Real
    ETF flash-crash lows (2015-08-24) fell ~40%, inside that limit.
    """
    if bars.empty:
        return bars
    body_lo = bars[["open", "close"]].min(axis=1)
    body_hi = bars[["open", "close"]].max(axis=1)
    bad_lo = bars["low"] < 0.5 * body_lo
    bad_hi = bars["high"] > 2.0 * body_hi
    if bad_lo.any() or bad_hi.any():
        bars = bars.copy()
        for ts in bars.index[bad_lo | bad_hi]:
            logger.warning(
                f"{symbol}: bad print on {ts.date()} "
                f"(O={bars.at[ts, 'open']} H={bars.at[ts, 'high']} "
                f"L={bars.at[ts, 'low']} C={bars.at[ts, 'close']}) — clipped to open/close."
            )
        bars.loc[bad_lo, "low"] = body_lo[bad_lo]
        bars.loc[bad_hi, "high"] = body_hi[bad_hi]
    return bars


def fetch_bars(symbol: str) -> pd.DataFrame:
    """Daily OHLCV — enough for SMA(50), ATR(14), and volume MA(20)."""
    return _fetch_bars(
        symbol, TimeFrame.Day, int(config.LOOKBACK_DAYS * 1.6)
    ).tail(config.LOOKBACK_DAYS)


def _et_date(ts: pd.Timestamp) -> str:
    """Trading date of a bar timestamp as ISO text (Alpaca stamps daily bars at
    04:00 UTC). Text, not a date: queued entries carry the scan result into
    pending.json, whose encoder has no date support."""
    if ts.tzinfo is not None:
        ts = ts.tz_convert(_ET)
    return ts.date().isoformat()


def completed_weeks(daily: pd.DataFrame) -> pd.DataFrame:
    """Weekly OHLCV (weeks ending Friday) built from daily bars, completed weeks only.

    A week whose Friday label is later than the last daily bar is still in
    progress and is dropped. That is the rule the backtests use (a ``W-FRI``
    resample forward-filled onto the daily index), so the live regime gate sees
    the ADX that was tested. Alpaca's own weekly bars include the unfinished
    week, which is part of why the live ADX drifted from the backtest's.
    """
    days = daily.copy()
    idx = days.index
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    days.index = idx.normalize()
    weekly = pd.DataFrame({
        "open":   days["open"].resample("W-FRI").first(),
        "high":   days["high"].resample("W-FRI").max(),
        "low":    days["low"].resample("W-FRI").min(),
        "close":  days["close"].resample("W-FRI").last(),
        "volume": days["volume"].resample("W-FRI").sum(),
    }).dropna(subset=["close"])
    return weekly[weekly.index <= days.index[-1]]


def fetch_weekly_bars(symbol: str) -> pd.DataFrame:
    """Weekly OHLCV — enough for SMA(50,W), SMA(200,W) and the weekly ADX."""
    calendar_days = int((config.WEEKLY_LOOKBACK_WEEKS + 2) * 7 * 1.05)
    daily = _fetch_bars(symbol, TimeFrame.Day, calendar_days)
    return completed_weeks(daily).tail(config.WEEKLY_LOOKBACK_WEEKS)


# ── Signal Evaluation ─────────────────────────────────────────────────────────

def evaluate_symbol(symbol: str) -> dict:
    """
    Evaluate entry and exit conditions for a symbol.

    Returns a dict with keys:
        symbol, close,
        sma50_daily, sma50_weekly, sma200_weekly,
        rsi2, prev_rsi2, atr14, volume, volume_ma20,
        stop_a, stop_b, active_stop,
        entry_signal (bool), exit_signal (bool), weekly_exit_signal (bool)
    """
    # ── Daily bars ────────────────────────────────────────────────────────────
    bars = fetch_bars(symbol)

    if len(bars) < 60:
        logger.warning(f"{symbol}: Insufficient daily data ({len(bars)} bars). Skipping.")
        return {
            "symbol": symbol, "entry_signal": False,
            "exit_signal": False, "weekly_exit_signal": False,
            "error": "insufficient_data",
        }

    close  = bars["close"]
    high   = bars["high"]
    low    = bars["low"]
    volume = bars["volume"]

    sma50_d  = sma(close, config.SMA_DAILY)
    sma200_d = sma(close, config.SMA_DAILY_TREND)
    rsi2     = rsi(close, config.RSI_PERIOD)
    atr14    = atr(high, low, close, config.ATR_PERIOD)
    vol_ma20 = sma(volume, config.VOLUME_MA_PERIOD)

    last_close    = round(float(close.iloc[-1]),   2)
    last_sma50_d  = round(float(sma50_d.iloc[-1]), 2)
    last_sma200_d = float(sma200_d.iloc[-1])   # NaN if < 200 bars available
    last_rsi2     = round(float(rsi2.iloc[-1]),    2)
    prev_rsi2     = round(float(rsi2.iloc[-2]),    2)
    last_atr      = round(float(atr14.iloc[-1]),   2)
    last_volume   = int(volume.iloc[-1])
    last_vol_ma20 = float(vol_ma20.iloc[-1])

    above_daily_sma50  = last_close > last_sma50_d
    # Connors trend regime: long only above the daily 200-day SMA. A NaN SMA200
    # (too few bars) fails closed — no entry — which is the conservative choice.
    above_daily_sma200 = (not pd.isna(last_sma200_d)) and (last_close > last_sma200_d)
    last_sma200_d      = round(last_sma200_d, 2) if not pd.isna(last_sma200_d) else 0.0

    # Entry trigger style (config.ENTRY_MODE):
    #   oversold  — Connors: RSI(2) still below the threshold (buy the dip)
    #   crossback — legacy: RSI(2) crossed back above the threshold (buy the turn)
    rsi_crossed_above = (prev_rsi2 <= config.RSI_ENTRY_THRESHOLD) and (last_rsi2 > config.RSI_ENTRY_THRESHOLD)
    rsi_oversold      = last_rsi2 < config.RSI_ENTRY_THRESHOLD
    rsi_entry_trigger = rsi_oversold if config.ENTRY_MODE == "oversold" else rsi_crossed_above
    # Exit: RSI(2) was >= 70 last bar, now crossed back below 70
    rsi_crossed_below = (prev_rsi2 >= config.RSI_EXIT_THRESHOLD)  and (last_rsi2 < config.RSI_EXIT_THRESHOLD)
    volume_spike      = last_volume > (config.VOLUME_SPIKE_MULT * last_vol_ma20)

    # ── Weekly bars ───────────────────────────────────────────────────────────
    # Fetched unconditionally, even though the trend gate and regime gate are
    # both OFF live: this is also how the ADX regime filter gets watched without
    # being turned on — every entry logs the weekly ADX it saw, so trades.csv can
    # be split into "would have passed the band" vs. not, using genuinely new
    # forward data rather than another resample of the same backtest window.
    above_weekly_sma50  = False
    above_weekly_sma200 = False
    last_weekly_sma50   = 0.0
    last_weekly_sma200  = 0.0
    last_weekly_adx     = 0.0
    #: Did the weekly figures actually come from weekly bars?
    #
    # This has to be tracked separately, because the defaults above are read in
    # opposite directions by the two gates. For an *entry*, "not above the weekly
    # SMA200" blocks the trade — missing data is conservative. For an *exit*, the
    # identical value means "trend has broken, get out". So a failed weekly fetch
    # with the trend filter on would emit an exit for **every held symbol at
    # once** — a whole-book liquidation caused by a network error rather than by
    # anything the market did.
    weekly_data_ok = False

    try:
        weekly_bars = fetch_weekly_bars(symbol)
        if len(weekly_bars) >= config.SMA_WEEKLY_SLOW + 10:
            w_close  = weekly_bars["close"]
            w_sma50  = sma(w_close, config.SMA_WEEKLY_FAST)
            w_sma200 = sma(w_close, config.SMA_WEEKLY_SLOW)
            w_adx    = adx(weekly_bars["high"], weekly_bars["low"], w_close, config.REGIME_ADX_PERIOD)
            last_weekly_sma50  = round(float(w_sma50.iloc[-1]),  2)
            last_weekly_sma200 = round(float(w_sma200.iloc[-1]), 2)
            last_weekly_adx    = round(float(w_adx.iloc[-1]),    2)
            above_weekly_sma50  = last_close > last_weekly_sma50
            above_weekly_sma200 = last_close > last_weekly_sma200
            weekly_data_ok      = True
        else:
            logger.warning(
                f"{symbol}: Insufficient weekly data ({len(weekly_bars)} bars) "
                f"— weekly gate FAILED (need {config.SMA_WEEKLY_SLOW + 10})."
            )
    except Exception as e:
        logger.error(f"{symbol}: Weekly bar fetch failed: {e}", exc_info=True)

    weekly_trend_ok = above_weekly_sma50 and above_weekly_sma200

    # ── Stops ─────────────────────────────────────────────────────────────────
    stop_a      = round(last_close - (config.STOP_MULT_A      * last_atr), 2)
    stop_b      = round(last_close - (config.STOP_MULT_B      * last_atr), 2)
    active_stop = round(last_close - (config.ACTIVE_STOP_MULT * last_atr), 2)

    # ── Signal logic ──────────────────────────────────────────────────────────
    # Trend filter: require an uptrend (price above weekly SMA50/200 and daily
    # SMA50) to enter. When OFF (config.USE_TREND_FILTER), the strategy trades
    # range-bound names with no trend requirement, so the weekly-SMA200
    # structural exit is disabled too — otherwise it would dump sideways names
    # that naturally sit below their 4-year average.
    trend_ok           = (weekly_trend_ok and above_daily_sma50) if config.USE_TREND_FILTER else True
    # Connors daily 200-SMA trend gate (see config.USE_DAILY_SMA200_FILTER).
    daily_trend_ok     = above_daily_sma200 if config.USE_DAILY_SMA200_FILTER else True
    # Volume spike is an optional confirmation (see config.USE_VOLUME_FILTER).
    volume_ok          = volume_spike if config.USE_VOLUME_FILTER else True
    # Raw ADX band membership, independent of whether the filter is enforced —
    # this is the shadow signal logged to trades.csv (see position_tracker.add).
    # None (not False) when weekly data is missing, so a fetch failure reads as
    # "unknown" in the ledger rather than silently as "band missed".
    regime_band_ok      = (config.REGIME_ADX_MIN <= last_weekly_adx < config.REGIME_ADX_MAX
                           ) if weekly_data_ok else None
    # Regime gate: weekly ADX must sit in the moderate-trend band (see
    # config.USE_REGIME_FILTER). If weekly data was missing, last_weekly_adx is
    # 0.0 → fails the band, blocking entry (conservative — matches weekly gate).
    regime_ok          = (config.REGIME_ADX_MIN <= last_weekly_adx < config.REGIME_ADX_MAX
                          ) if config.USE_REGIME_FILTER else True
    prereqs_ok         = trend_ok and daily_trend_ok and volume_ok and regime_ok
    entry_signal       = prereqs_ok and rsi_entry_trigger
    exit_signal        = rsi_crossed_below
    # An exit must be driven by data, never by the absence of it: `weekly_data_ok`
    # is what stops a failed weekly fetch from reading as "trend broken" for every
    # symbol at once. Missing data means hold, and says so loudly.
    weekly_exit_signal = (
        config.USE_TREND_FILTER and weekly_data_ok and not above_weekly_sma200
    )
    if config.USE_TREND_FILTER and not weekly_data_ok:
        logger.error(
            f"{symbol}: weekly data unavailable — holding rather than exiting. "
            f"The weekly trend exit is disabled for this symbol this cycle."
        )

    result = {
        "symbol":              symbol,
        "bar_date":            _et_date(bars.index[-1]),
        "close":               last_close,
        "sma50_daily":         last_sma50_d,
        "sma200_daily":        last_sma200_d,
        "above_daily_sma200":  above_daily_sma200,
        "sma50_weekly":        last_weekly_sma50,
        "sma200_weekly":       last_weekly_sma200,
        "weekly_adx":          last_weekly_adx,
        "rsi2":                last_rsi2,
        "prev_rsi2":           prev_rsi2,
        "atr14":               last_atr,
        "volume":              last_volume,
        "volume_ma20":         round(last_vol_ma20, 0),
        "above_daily_sma50":   above_daily_sma50,
        "above_weekly_sma50":  above_weekly_sma50,
        "above_weekly_sma200": above_weekly_sma200,
        "weekly_data_ok":      weekly_data_ok,
        "regime_ok":           regime_ok,
        "regime_band_ok":      regime_band_ok,
        "volume_spike":        volume_spike,
        "stop_a":              stop_a,
        "stop_b":              stop_b,
        "active_stop":         active_stop,
        "prereqs_ok":          prereqs_ok,
        "entry_signal":        entry_signal,
        "exit_signal":         exit_signal,
        "weekly_exit_signal":  weekly_exit_signal,
        "timestamp":           datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"[{symbol}] Close={last_close} | "
        f"SMA50d={last_sma50_d} above={above_daily_sma50} | "
        f"SMA200d={last_sma200_d} above200d={above_daily_sma200} | "
        f"SMA50w={last_weekly_sma50} SMA200w={last_weekly_sma200} "
        f"above50w={above_weekly_sma50} above200w={above_weekly_sma200} | "
        f"ADXw={last_weekly_adx} regime_ok={regime_ok} | "
        f"RSI2={last_rsi2:.2f} prev={prev_rsi2:.2f} trigger={rsi_entry_trigger}({config.ENTRY_MODE}) | "
        f"Vol={last_volume:,} VolMA={last_vol_ma20:,.0f} spike={volume_spike} | "
        f"ATR={last_atr:.2f} Stop_A={stop_a} Stop_B={stop_b} | "
        f"Entry={entry_signal} RsiExit={exit_signal} WeeklyExit={weekly_exit_signal}"
    )

    if entry_signal:
        logger.info(f"  ✅ ENTRY SIGNAL: {symbol} | Active stop ({config.ACTIVE_STOP_MULT}×ATR): {active_stop}")
    if exit_signal:
        logger.info(
            f"  📤 RSI EXIT: {symbol} | RSI(2) crossed below {config.RSI_EXIT_THRESHOLD} "
            f"({prev_rsi2:.2f} → {last_rsi2:.2f})"
        )
    if weekly_exit_signal:
        logger.info(
            f"  📤 WEEKLY EXIT: {symbol} | Close={last_close} below weekly SMA(200)={last_weekly_sma200}"
        )

    return result


# ── Full Scan ─────────────────────────────────────────────────────────────────

def run_scan() -> dict[str, dict]:
    """Scan all configured symbols. Returns dict of symbol → evaluation result."""
    logger.info("=" * 60)
    logger.info(f"Post-close scan started | {datetime.now(timezone.utc).isoformat()}")

    results = {}
    today = datetime.now(_ET).date().isoformat()
    for symbol in config.SYMBOLS:
        try:
            result = evaluate_symbol(symbol)
            # Bars now come from the SIP feed with a 16-minute delay. If today's
            # bar is not there yet, every signal would be yesterday's, so switch
            # them off. Not marked as an error: post_close_scan skips errored
            # symbols entirely, and that would also skip the time stop, which
            # doesn't depend on bars. (The scan only runs on trading days.)
            if not result.get("error") and result.get("bar_date") != today:
                logger.error(
                    f"{symbol}: latest daily bar is {result.get('bar_date')}, not {today} — "
                    f"stale data, no entry or RSI/weekly exit signals this scan."
                )
                result.update(entry_signal=False, exit_signal=False,
                              weekly_exit_signal=False, stale_bars=True)
            results[symbol] = result
        except Exception as e:
            logger.error(f"Scan error for {symbol}: {e}", exc_info=True)
            results[symbol] = {
                "symbol": symbol, "entry_signal": False,
                "exit_signal": False, "weekly_exit_signal": False,
                "error": str(e),
            }

    entries       = [s for s, r in results.items() if r.get("entry_signal")]
    rsi_exits     = [s for s, r in results.items() if r.get("exit_signal")]
    weekly_exits  = [s for s, r in results.items() if r.get("weekly_exit_signal")]

    logger.info(
        f"Scan complete | Entries: {entries} | "
        f"RSI exits: {rsi_exits} | Weekly exits: {weekly_exits}"
    )
    logger.info("=" * 60)

    return results
