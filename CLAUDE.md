# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mean Reversion trading bot. See `SETUP.md` for setup and run instructions.

## Environment

Credentials are loaded from environment variables (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). The `PAPER` flag in `config.py` controls paper vs. live trading.

## Notifications: ntfy and Discord (changed 2026-10-24)

Routine and urgent messages now go to different places.

- **Discord** gets the daily status report. It is built by the shared `quantcore.report` module, so all four bots (MeansRev, SeykotaBot, Meridian, VWAP Pullback) use the same layout: headline fields, a narrow position table, an equity-curve chart, a pinned "current status" card edited in place, and the `#all-bots` dashboard.
- **ntfy** keeps the urgent alerts (failed jobs, books vs broker mismatch, queued order never sent), plus the existing RSI warming/signal alerts and eval checkpoint. It is also the **fallback** for the daily report if no Discord webhook is set or Discord is down, so a report is never silently dropped.
- The webhook is `DISCORD_WEBHOOK_URL` in `alpaca.env` (read in `config.py`). It is a credential: anyone holding the URL can post to the channel. Never commit it.
- Code: `notifier.send_daily_status()` builds a `DailyReport`; `executor.get_account_snapshot()` now also returns `cash`; `meansrev_main.status_report()` passes the queued-action count. The old Cowork task `meansrev-daily-status` duplicated this push and was retired (local copy moved to `_set_aside/`); the live task must also be disabled in the Claude app.
- Tests replace `quantcore.report._post_discord`, so a test can never write fake equity into the real history or overwrite a real status card. Real state lives in `~/.quantcore/` (`equity/`, `snapshots/`, `discord_state.json`, `discord.json`).
