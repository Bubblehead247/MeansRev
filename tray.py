"""
tray.py — System-tray status icon for the Mean Reversion Bot.

Shows what the bot has actually done today, rather than merely that a process
exists. Those are not the same thing, and the difference has cost money: the
08:45 fill-confirm job silently failed to run on 2026-07-15 and nothing showed
it, which is how a phantom XLV trade ended up in the ledger.

The scheduled jobs now run as separate short-lived processes under Windows Task
Scheduler, whether or not anyone is logged in. This icon only reports on them.
Closing it does not stop the bot, and a failing job cannot take it down.

    grey    not scheduled yet, or no recent reconciliation
    green   every job ran, and the books agree with the broker
    amber   a job that should have run today has not
    red     a job failed, or the books disagree with the broker

Right-click for a per-job breakdown, "Run now", and the log.

Run with:  py -3.14 tray.py

The implementation is shared by all three bots — see ``quantcore/tray.py`` — so
they look and behave identically and there is one place to fix.
"""

from quantcore.tray import run_tray

if __name__ == "__main__":
    run_tray("meansrev", label="MeansRev", letter="MR", colour=(234, 88, 12))
