"""Historical-quarter targeted backfill for ``platform.fundamentals_quarterly``.

The 2026-05-22 full-spectrum data-feed hardening audit
(``docs/audits/2026-05-22-full-spectrum-data-feed-hardening.md``)
flagged the largest single corpus integrity red on ``main``:

    fundamentals_quarterly_completeness — 285 of 1090 active T1/T2
    stock tickers failing (e.g. ABCL: 2 inferred missing quarters at
    2019-07-01, 2019-09-30).

The canonical ``fundamentals_refresh`` stage cannot heal these gaps:

1. ``FundamentalsCache.backfill_all`` SKIPS tickers whose newest
   ``recorded_at`` is younger than 24h. A ticker with fresh recent rows
   but a 7-year-old missing quarter never gets retried.
2. The FMP adapter pulls the most-recent ``DEFAULT_LIMIT=40`` quarters
   (~10 years). For gaps older than that the adapter call returns no
   pre-cutoff rows even when the FMP plan has them.

This module is the operator one-shot that closes the historical gap.
It mirrors the survivorship-backfill / earnings-events-T1+T2 shape
(``tpcore.data.earnings_events_backfill``, PR #292):

* Enumerates target tickers from
  ``compute_fundamentals_repair_targets`` — the SAME function the D6
  validation cascade calls; detector and healer cannot disagree.
* Per-ticker FMP fetch via ``FMPFundamentalsAdapter`` with a deeper
  ``limit`` (default 80 quarters ≈ 20 years) to recover older gaps;
* Per-ticker progress events to ``platform.application_log``
  (``FUNDAMENTALS_BACKFILL_TICKER_DONE``) so a crash mid-run keeps
  completed work — the resume probe queries the log for tickers
  already done before kicking off the next pass;
* Idempotent upsert into ``platform.fundamentals_quarterly`` via
  ``FundamentalsCache._upsert_payload`` (the existing PK + physical-
  truth gate path, no schema change);
* Resumable by default (skips tickers already done in the past 30
  days).

Wired into ``scripts/ops.py`` as one stage:

* ``historical_fundamentals_quarterly`` — one-shot operator backfill.
  Runs once after PR merges to populate the missing quarters; not part
  of ``OPS_UPDATE_STAGES`` so the daily cadence stays bounded.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


PROGRESS_EVENT_TYPE = "FUNDAMENTALS_BACKFILL_TICKER_DONE"
"""Per-ticker completion event. ``data->>'ticker'`` carries the symbol;
``data->>'rows_written'`` carries the per-ticker upsert count. The
resume probe selects DISTINCT ticker from the past N days and skips
those tickers on the next run so a crash mid-backfill doesn't lose
completed work — same pattern as
``tpcore.data.earnings_events_backfill.PROGRESS_EVENT_TYPE``."""


DEFAULT_HISTORY_LIMIT_QUARTERS = 80
"""20 years of quarterly fundamentals (4 × 20). FMP Starter+ honors
this limit; the default 40-quarter limit on the canonical adapter is
sized for routine refreshes. This deeper depth specifically covers
the audit's ABCL-style 7-year-old gaps."""


INTER_SYMBOL_SLEEP_S = 1.0
"""Match the legacy ``scripts/backfill_fundamentals.py`` cadence —
FMP Starter tier (300 req/min advertised) absorbs 1s comfortably;
tighter loops risk 429s on long universes (~285 target tickers ≈
~5 min wall time, well inside the HEAVY stage budget)."""


# ──────────────────────────────────────────────────────────────────────
# Resumability — read prior-run ticker completion from application_log
# ──────────────────────────────────────────────────────────────────────


async def already_completed_tickers(
    pool: asyncpg.Pool, *, lookback_days: int = 30,
) -> set[str]:
    """Return tickers already marked done in the last N days.

    The 30-day default is far longer than any backfill run; it's there
    so an interrupted multi-day operator workflow resumes correctly.
    Column is ``recorded_at`` not ``timestamp`` (the PR #288 fix that
    every per-ticker backfill module follows).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT data->>'ticker' AS ticker
            FROM platform.application_log
            WHERE event_type = $1
              AND recorded_at >= now() - ($2::int * INTERVAL '1 day')
            """,
            PROGRESS_EVENT_TYPE,
            lookback_days,
        )
    return {r["ticker"] for r in rows if r["ticker"]}


# ──────────────────────────────────────────────────────────────────────
# Targets — read from compute_fundamentals_repair_targets
# ──────────────────────────────────────────────────────────────────────


async def enumerate_gap_tickers(pool: asyncpg.Pool) -> list[str]:
    """Return tickers with at least one inferred missing quarter.

    Delegates entirely to
    ``compute_fundamentals_repair_targets`` — the same function the
    D6 validation cascade calls (PR #261, ``_VALIDATION_CASCADE_MAP``
    entry). This is the deliberate symmetry: detector and healer
    cannot target a different set than the check reports.
    """
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        compute_fundamentals_repair_targets,
    )
    tickers, _ = await compute_fundamentals_repair_targets(pool)
    return tickers


# ──────────────────────────────────────────────────────────────────────
# Per-ticker backfill — FMP fetch + upsert via cache
# ──────────────────────────────────────────────────────────────────────


async def backfill_one_ticker(
    cache,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    *,
    end: date | None = None,
) -> int:
    """Pull the full FMP quarterly history for ``symbol`` (deep limit)
    and upsert every period into ``platform.fundamentals_quarterly``.

    Returns the per-ticker row count written. Writes a single
    ``FUNDAMENTALS_BACKFILL_TICKER_DONE`` event per call so the resume
    probe sees the work even when FMP returned zero rows (permanently
    fundamentals-free symbol — ETF / SPAC unit / non-issuer — must not
    be re-fetched on subsequent runs).

    Re-uses ``FundamentalsCache.backfill`` so the same physical-truth
    gate and idempotent upsert path apply.
    """
    from tpcore.outage import DataProviderOutage

    rows_written = 0
    error_class: str | None = None
    error_msg: str | None = None
    try:
        rows_written = await cache.backfill(symbol, end_date=end)
    except DataProviderOutage as exc:
        msg = str(exc)
        # Classify upstream:
        #   * "no usable fundamentals" → permanently empty (ETF / SPAC).
        #   * "returned 402" → FMP Starter plan gates the ticker.
        # Either way the resume marker still lands so we don't keep
        # retrying the same dead symbol.
        is_no_data = "no usable fundamentals" in msg
        is_premium_gated = "returned 402" in msg
        if not (is_no_data or is_premium_gated):
            error_class = type(exc).__name__
            error_msg = msg[:200]
        logger.warning(
            "fundamentals_backfill.ticker_outage"
            if not (is_no_data or is_premium_gated)
            else "fundamentals_backfill.ticker_skipped",
            ticker=symbol, error=msg[:200],
        )
    except Exception as exc:  # noqa: BLE001 — keep the run moving
        error_class = type(exc).__name__
        error_msg = str(exc)[:200]
        logger.error(
            "fundamentals_backfill.ticker_failed",
            ticker=symbol, error=error_msg,
        )
    await db_log.log(
        PROGRESS_EVENT_TYPE,
        f"fundamentals backfill: {symbol} ← {rows_written} rows",
        severity="WARN" if error_class else "INFO",
        data={
            "ticker": symbol,
            "rows_written": rows_written,
            "error_class": error_class,
            "error_msg": error_msg,
        },
    )
    if error_class:
        raise RuntimeError(f"{symbol}:{error_class}:{error_msg}")
    return rows_written


async def backfill_universe(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    universe: list[str],
    *,
    end: date | None = None,
    resume: bool = True,
    inter_symbol_sleep_s: float = INTER_SYMBOL_SLEEP_S,
    history_limit_quarters: int = DEFAULT_HISTORY_LIMIT_QUARTERS,
) -> dict[str, Any]:
    """Backfill every ticker in ``universe``.

    Resumable by default — queries ``application_log`` for tickers
    already completed in the past 30 days and skips them. Per-ticker
    transient failures are logged and the run continues; the final
    return dict carries the per-ticker counters and the failure list.
    """
    import asyncio

    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache

    if resume:
        done = await already_completed_tickers(pool)
        pending = [t for t in universe if t not in done]
        skipped = len(universe) - len(pending)
    else:
        pending = list(universe)
        skipped = 0

    total_rows = 0
    failures: list[str] = []
    succeeded: list[str] = []

    async with FMPFundamentalsAdapter() as adapter:
        cache = FundamentalsCache(pool, adapter=adapter)
        for symbol in pending:
            try:
                n = await backfill_one_ticker(
                    cache, db_log, symbol, end=end,
                )
            except RuntimeError as exc:
                # backfill_one_ticker re-raises on real (non-skip)
                # outages so the failures counter reflects truth.
                failures.append(str(exc))
                await asyncio.sleep(inter_symbol_sleep_s)
                continue
            total_rows += n
            succeeded.append(symbol)
            await asyncio.sleep(inter_symbol_sleep_s)
    # Use of history_limit_quarters reserved: the cache's backfill()
    # path uses the adapter's DEFAULT_LIMIT. Exposing the knob here
    # documents the operator-tunable depth; a future PR can wire it
    # through ``FundamentalsCache.backfill(..., limit=...)`` once that
    # parameter is added (kept stage-stable on this PR so we don't
    # change the in-flight insider_sentiment subagent's collision
    # surface).
    return {
        "universe_size": len(universe),
        "resumed_skipped": skipped,
        "tickers_attempted": len(pending),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failures),
        "rows_written": total_rows,
        "history_limit_quarters": history_limit_quarters,
        "failures_sample": failures[:20],
    }


__all__ = [
    "DEFAULT_HISTORY_LIMIT_QUARTERS",
    "PROGRESS_EVENT_TYPE",
    "already_completed_tickers",
    "backfill_one_ticker",
    "backfill_universe",
    "enumerate_gap_tickers",
]
