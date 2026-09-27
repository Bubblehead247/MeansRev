"""
config.py — Central configuration for the Mean Reversion Bot.
All tunable parameters live here. No need to touch other files for adjustments.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load credentials from alpaca.env in the same directory as this file.
#
# override=True makes this file authoritative over the ambient OS environment.
# Without it a variable already set at the OS level silently shadows the bot's
# own value — this machine has a user-level NTFY_TOPIC from an unrelated project,
# which sent MeansRev's alerts to the wrong topic.
load_dotenv(Path(__file__).resolve().parent / "alpaca.env", override=True)

# ── Alpaca Credentials ────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY", "YOUR_KEY_HERE")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "YOUR_SECRET_HERE")
PAPER      = True   # Set False when going live with real money

# ── Trading Universe (Connors-aligned liquid ETFs) ────────────────────────────
# Switched from the 30-name SIDEWAYS screen to a liquid ETF set (Larry Connors'
# canonical RSI(2) instruments). Rationale validated in backtest_connors.py /
# backtest_connors_pit.py: the sideways screen is the OPPOSITE of what Connors'
# dip-in-an-uptrend edge needs, and individual mega-caps carry survivorship bias.
# These ETFs are survivorship-clean, deeply liquid, and already sector-mapped in
# sectors.py (for the MAX_PER_SECTOR cap). To revert, restore the list above.
SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "RSP", "IJH", "EFA", "EEM",   # broad index (→ Broad/Index)
    "XLE", "XLF", "XLK", "XLV", "XLU", "XLI", "XLY", "XLP", "XLB", "XLRE", "XLC",  # sector ETFs (→ GICS sector)
    "XBI", "XOP", "XRT",                          # sub-sector siblings of XLV/XLE/XLY — added 2026-09-10:
                                                   # 12yr backtest showed a real edge (PF 4.45/3.79/1.28) and
                                                   # each opens a genuine second candidate in a sector that
                                                   # otherwise has only one symbol competing for its 2-position cap.
    "GLD", "SLV", "TLT",                          # commodity / long bonds
    "UNG", "DBMF",                                # natural gas / managed futures (low corr., see sectors.py)
]

# ── Indicator Parameters ──────────────────────────────────────────────────────
RSI_PERIOD          = 2
RSI_ENTRY_THRESHOLD = 10.0   # Entry threshold on RSI(2) — see ENTRY_MODE
RSI_EXIT_THRESHOLD  = 70.0   # Exit fires when RSI(2) crosses back below this
# Entry trigger style:
#   "oversold"  = Larry Connors: enter while RSI(2) is STILL below the threshold
#                 (buy the dip). Validated as the stronger entry (backtest_connors).
#   "crossback" = legacy live behaviour: enter only once RSI(2) crosses back ABOVE
#                 the threshold (buy the turn).
ENTRY_MODE          = "oversold"
SMA_DAILY           = 50     # Daily SMA — legacy daily gate (only used by weekly trend filter)
# Connors trend regime: long only when price closes ABOVE the daily 200-day SMA.
# This is the single biggest edge lever in testing — it puts back the "dip in an
# uptrend" regime the sideways universe had removed. Distinct from the weekly
# SMA50/200 gate (USE_TREND_FILTER), which stays OFF.
SMA_DAILY_TREND        = 200
USE_DAILY_SMA200_FILTER = True
SMA_WEEKLY_FAST     = 50     # Weekly SMA — trend gate (1-year)
SMA_WEEKLY_SLOW     = 200    # Weekly SMA — trend gate (4-year)
# Trend filter: when ON, entries require price above the daily SMA50 and weekly
# SMA50/200, and positions exit on a close below the weekly SMA200. OFF for the
# sideways universe — those low-trend names rarely sit above these averages, so
# both the entry uptrend gate and the weekly-SMA200 exit are disabled.
USE_TREND_FILTER    = False
ATR_PERIOD          = 14
VOLUME_MA_PERIOD    = 20     # Period for volume moving average
VOLUME_SPIKE_MULT   = 1.5    # When the filter is ON: volume must exceed this × 20-day average
# Volume-spike entry confirmation. Backtest (2010–2026, 16 ETFs) showed that
# requiring a volume spike on the exact RSI-cross-above day removes ~90% of
# signals and cuts ~12yr return from +51% to +11%. Left OFF by default; flip to
# True to re-test. Volume is still computed and logged either way.
USE_VOLUME_FILTER   = False
LOOKBACK_DAYS       = 260    # Daily bars — must exceed SMA_DAILY_TREND(200) + buffer for the Connors trend gate
WEEKLY_LOOKBACK_WEEKS = 220  # Weekly bars (SMA200=200 + buffer)

# ── Risk Management ───────────────────────────────────────────────────────────
RISK_PER_TRADE = 0.01        # 1% of account equity risked per trade
# Notional cap: no single position may exceed this fraction of equity. Stops the
# 1%-risk sizing formula from building huge, leveraged positions on low-vol names
# (tiny stop distance → giant share count). At 0.15, MAX_POSITIONS(6) × 15% =
# 90% of equity at full notional, no leverage possible (the account has none).
# 2026-09-27: 0.20 → 0.15 together with 6 positions and the regime filter off
# ("option C", research/strategy_review_2026_09). Backtest on Alpaca SIP adjusted
# bars, 2020-2026: 9.4%/yr, max DD -7.3%, vs 4.6%/yr, -9.1% for the old config,
# which left ~85% of capital idle.
MAX_POSITION_PCT = 0.15

# ── Limit Order Slippage Controls ─────────────────────────────────────────────
ENTRY_LIMIT_PCT = 0.005      # Max we'll pay above prior close for a LOO buy (0.5%)
EXIT_LIMIT_PCT  = 0.005      # Min we'll accept below prior close for a LOO sell (0.5%)

# ── Backtest Slippage Model ───────────────────────────────────────────────────
# Per-side slippage the BACKTESTER applies to model bid/ask spread + market
# impact on market-on-open and stop fills. Buys fill SLIPPAGE_PCT above the
# reference price, sells fill SLIPPAGE_PCT below it (a round trip costs ~2×).
# 0.0005 = 5 bps/side. Alpaca is commission-free, so spread/impact is the
# dominant friction. Set to 0.0 for a frictionless backtest.
# NOTE: only consumed by backtest.py — does not affect live order placement.
SLIPPAGE_PCT = 0.0005

# ── Stop Loss Variants ────────────────────────────────────────────────────────
# Both are tracked in logs. Only ACTIVE_STOP_MULT is actually executed.
# Toggle between 1.5 and 2.5 to compare performance in paper trading.
STOP_MULT_A      = 1.5
STOP_MULT_B      = 2.5
ACTIVE_STOP_MULT = 2.5       # ← Change to 1.5 to test the tighter stop variant

# ── Regime Filter ──────────────────────────────────────────────────────────────
# Gate entries on weekly trend STRENGTH. Validation (analysis.py) found the edge
# is positive in BOTH in/out-of-sample halves only in a moderate-trend band:
# dead-sideways (ADX<20) and runaway trends (ADX>=25) both underperformed.
# Enter only when weekly ADX is in [MIN, MAX). Enforced in BOTH backtest.py and
# scanner.py (live). Validation: filtered edge is positive in both in/out-of-
# sample halves and ~3× lower drawdown, but keeps only ~26% of signals and its
# bootstrap CI still includes zero — promising, not statistically proven.
# Turned ON 2026-09-10: the shadow-logged column (trades.csv,
# regime_band_ok_at_signal) added in 995a6c6 showed the 4 trades since then all
# had regime_band_ok=False and all lost money — consistent with, though far too
# small a sample to confirm, the backtest finding above.
# Turned OFF 2026-09-27 (user chose "option C"): with it on, ~85% of capital sat
# idle. The filter did improve per-trade quality (PF 1.42 vs 1.35), but its edge
# moved with the band (18-23 better, 22-27 worse), and with the filter off the
# strategy beat the filtered one in both halves of 2011-2026 (yfinance) and 5/7
# years of 2020-2026 (Alpaca). regime_band_ok is still shadow-logged per trade.
USE_REGIME_FILTER = False
REGIME_ADX_PERIOD = 14
REGIME_ADX_MIN    = 20.0
REGIME_ADX_MAX    = 25.0

# ── Sideways-Stock Screener (standalone — does NOT affect live trading) ───────
# Used only by screener.py to scan all of Alpaca for range-bound (low-trend)
# stocks and rank them by a combined "sideways score". None of these touch the
# live bot, which trades only the SYMBOLS list above.
SCREEN_MIN_PRICE         = 5.0          # Drop stocks priced below this
SCREEN_MIN_DOLLAR_VOLUME = 20_000_000   # Liquidity floor: close × volume (1 day)
SCREEN_LOOKBACK_DAYS     = 60           # Daily bars used for the sideways math
SCREEN_ADX_PERIOD        = 14           # ADX period for the trendlessness score
SCREEN_RANGE_SMA         = 50           # SMA the price must hug to count as range-bound
SCREEN_RANGE_BAND_PCT    = 0.05         # ±5% band around the SMA = "near the mean"
SCREEN_W_TREND           = 0.5          # Score weight: low ADX (trendlessness)
SCREEN_W_RANGE           = 0.3          # Score weight: price hugs its mean
SCREEN_W_LIQUIDITY       = 0.2          # Score weight: dollar volume
SCREEN_TOP_N             = 50           # How many top candidates to report
SCREEN_SNAPSHOT_CHUNK    = 1000         # Symbols per snapshot request (Stage 1)
SCREEN_BARS_CHUNK        = 100          # Symbols per bars request (Stage 2)
SCREEN_RESULTS_CSV       = "screener_results.csv"

# ── ntfy.sh Push Alerts ───────────────────────────────────────────────────────
# The topic comes from alpaca.env, never from this file. ntfy.sh is public and
# unauthenticated, so the topic name *is* the credential: anyone holding it can
# read every trade alert and publish forged ones. It was committed here in
# plaintext until 2026-07-30. Unset means alerts are off, which is better than a
# default that would be published in this file all over again.
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

# ── Exit Rules ────────────────────────────────────────────────────────────────
MAX_HOLD_DAYS = 7            # Time stop: force exit if trade is still open after 7 days
MAX_POSITIONS = 6            # Max concurrent positions (6% total risk cap at 1% each); was 4 until 2026-09-27
MAX_PER_SECTOR = 2           # Max concurrent positions in any one sector (see sectors.py)

# ── Scheduling (Central Time — CST is ET minus 1 hour) ───────────────────────
SCAN_TIME         = "15:30"  # Post-close scan       (4:30 PM ET)
EXECUTE_TIME      = "08:25"  # Pre-open order submit (9:25 AM ET)
FILL_CONFIRM_TIME = "08:45"  # Fill confirm + stops  (9:45 AM ET)
# Second confirm pass, 10 minutes before the close. Entries are DAY limits that
# stay live all session, so one filling late morning would otherwise sit without
# a hard stop until tomorrow. This pass also cancels anything still unfilled.
FINAL_CONFIRM_TIME = "14:50"  # Pre-close confirm     (3:50 PM ET)
