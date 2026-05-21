"""Canonical 50-name backtest universe constant.

The default ticker set used by backtest harnesses, corporate-actions
cron jobs, and any other ops path that needs a stable, hand-curated
slice of liquid US equities + ETFs covering all eleven GICS sectors
plus the standard broad-market trio (SPY/QQQ/IWM).

Was previously hard-coded in ``scripts/backfill_backtest_universe.py``
and duplicated (with a sync-by-comment rationale) in
``ops/cron_corporate_actions.py``. Migrated here 2026-05-20 as part of
the orphan-scripts zero-allowlist sweep (catalog at
``docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md``) so
the constant has a single source of truth on the installed package
path and any caller can ``from stelib.backtest.universe import
DEFAULT_BACKTEST_UNIVERSE`` instead of duplicating the tuple.

Names that didn't exist for the full 2018-2025 window (IPOs after
2018: PLTR, UBER, ABNB, RBLX, RIVN, LCID) are deliberately included
— the consumers either tolerate partial coverage (backtests skip
warm-up windows automatically) or are insensitive to the listing
date (corporate-actions ingest is idempotent and skips missing
windows).
"""
from __future__ import annotations

DEFAULT_BACKTEST_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)
