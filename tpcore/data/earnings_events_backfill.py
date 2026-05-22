"""T1+T2 earnings-events backfill for ``platform.earnings_events``.

The 2026-05-13 Vector parameter-search produced ZERO trades on every
candidate because ``platform.earnings_events`` had no overlap with the
``platform.liquidity_tiers`` T1+T2 universe (44 tickers in
earnings_events at the time, zero in T1+T2). The 2026-05-14 follow-up
ran ``earnings_refresh`` and expanded the table to 137 tickers — still
far below the T1+T2 stock-class population of ~1500. Operator-quoted
gating decision (MASTER_PLAN.md §4.3):

    "Re-enabling Vector is gated on a one-time data-ingestion backfill
    (catalyst events for T1+T2 tickers from FMP earnings-history
    endpoint), not on any strategy work."

This module is the one-shot operator backfill that closes the gap. It
mirrors ``tpcore.data.survivorship_backfill`` faithfully:

* per-ticker FMP ``/stable/earnings?symbol=<t>`` GET (the same endpoint
  ``scripts/backfill_earnings_events.py`` uses — verified at the
  operator's $200/yr FMP Starter tier 2026-05-22; no batch endpoint at
  this tier);
* per-ticker progress events to ``platform.application_log``
  (``EARNINGS_BACKFILL_TICKER_DONE``) so a crash mid-run keeps
  completed work — the resume probe queries the log for tickers
  already done before kicking off the next pass;
* idempotent upsert into ``platform.earnings_events`` via the existing
  ``ON CONFLICT (ticker, event_date, event_type) DO NOTHING`` PK
  (no schema change required);
* classification REUSED from ``scripts.backfill_earnings_events._classify_earnings``
  — both ``EARNINGS_BEAT`` and ``EARNINGS_NO_BEAT`` rows are written so
  the per-ticker monotone invariant (``earnings_events_monotone``) is
  not biased by a partial run.

Why a new stage instead of just re-running ``earnings_refresh``: the
existing ``_stage_earnings_refresh`` (a) has a 6-day skip guard that
no-ops a same-week re-invocation, (b) delegates to the synchronous
``backfill_amain`` which has NO resume-progress emission, so a
mid-2400-ticker crash loses all completed work, and (c) reads its
universe from the FULL T1+T2 stock-class set without per-ticker
progress logging that would let the operator diagnose where coverage
landed. This module is the resumable, audit-trail-emitting one-shot
the survivorship-pattern PR (#283 / #288) established as the canonical
shape for heavy-lane backfills.

Wired into ``scripts/ops.py`` as one stage:

* ``historical_earnings_events_t1_t2`` — one-shot operator backfill.
  Runs once after PR merges to populate the T1+T2 catalyst-event
  coverage; not part of ``OPS_UPDATE_STAGES`` so the daily cadence
  stays bounded.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from scripts.backfill_earnings_events import (
    EARNINGS_URL,
    _classify_earnings,
    fetch_earnings,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


PROGRESS_EVENT_TYPE = "EARNINGS_BACKFILL_TICKER_DONE"
"""Per-ticker completion event. ``data->>'ticker'`` carries the symbol;
``data->>'beats_written'`` / ``data->>'no_beats_written'`` carry the
per-ticker insert counters. The resume probe selects DISTINCT ticker
from the past N days and skips those tickers on the next run so a crash
mid-backfill doesn't lose completed work — same pattern as
``tpcore.data.survivorship_backfill.PROGRESS_EVENT_TYPE``."""


_DEFAULT_BACKFILL_START = date(2018, 1, 1)
"""FMP earnings-history coverage starts ~2018 (per
``backfill_earnings_events.py`` script docstring and MASTER_PLAN.md
§4.3 note). Earlier dates simply return empty arrays — no harm but no
value either."""


INTER_SYMBOL_SLEEP_S = 0.4
"""Match the legacy ``scripts/backfill_earnings_events.py`` cadence —
the rate-limit budget at FMP Starter tier (300 req/min advertised) is
not the bottleneck; per-ticker latency dominates. ~2400 tickers ×
~0.6s/call (sleep + RTT) ≈ 25 min wall time, consistent with the
operator-spec budget."""


_INSERT_SQL = """
    INSERT INTO platform.earnings_events
        (ticker, event_date, event_type, magnitude_pct, source, recorded_at)
    VALUES ($1, $2, $3, $4, 'fmp', now())
    ON CONFLICT (ticker, event_date, event_type) DO NOTHING
"""


# ──────────────────────────────────────────────────────────────────────
# Universe enumeration — T1+T2 stock-class only
# ──────────────────────────────────────────────────────────────────────


async def enumerate_t1_t2_stock_universe(pool: asyncpg.Pool) -> list[str]:
    """Return the T1+T2 stock-class universe sorted alphabetically.

    Mirrors the existing ``_stage_earnings_refresh`` universe query
    (``scripts/ops.py``) so this stage and the weekly refresh see the
    same population — ETFs / SPACs / funds are excluded because they
    have no earnings to beat. The ``COALESCE(asset_class, 'stock')``
    treats unclassified rows as stock-class (the safe default at the
    FMP earnings layer: an unknown gets queried; FMP returns an empty
    array for genuinely-non-stock symbols which the upsert handles
    with no rows written).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
            ORDER BY lt.ticker
            """
        )
    return [r["ticker"] for r in rows]


# ──────────────────────────────────────────────────────────────────────
# Resumability — read prior-run ticker completion from application_log
# ──────────────────────────────────────────────────────────────────────


async def already_completed_tickers(
    pool: asyncpg.Pool, *, lookback_days: int = 30,
) -> set[str]:
    """Return tickers already marked done in the last N days.

    The 30-day default is far longer than any backfill run; it's there
    so an interrupted multi-day operator workflow resumes correctly.
    Mirrors ``survivorship_backfill.already_completed_tickers`` —
    column is ``recorded_at`` not ``timestamp`` (the PR #288 fix).
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
# Per-ticker backfill — FMP fetch + classify + upsert
# ──────────────────────────────────────────────────────────────────────


async def backfill_one_ticker(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    api_key: str,
    *,
    start: date,
    end: date,
) -> tuple[int, int]:
    """Fetch the full FMP earnings history for ``symbol`` and upsert
    every classified row inside [start, end] into
    ``platform.earnings_events``.

    Returns ``(beats_written, no_beats_written)``. Writes a single
    ``EARNINGS_BACKFILL_TICKER_DONE`` event per call so the resume
    probe sees the work even when FMP returned zero rows (a permanently
    earnings-free symbol — ETF / SPAC unit / non-issuer — must not be
    re-fetched on subsequent runs).
    """
    rows = await fetch_earnings(client, symbol, api_key)
    events: list[tuple[str, date, str, Decimal | None]] = []
    beats = 0
    no_beats = 0
    for r in rows:
        raw_date = r.get("date")
        if not raw_date:
            continue
        try:
            ev_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if ev_date < start or ev_date > end:
            continue
        classification = _classify_earnings(r)
        if classification is None:
            continue
        event_type, magnitude = classification
        events.append((symbol, ev_date, event_type, magnitude))
        if event_type == "EARNINGS_BEAT":
            beats += 1
        else:
            no_beats += 1
    if events:
        async with pool.acquire() as conn:
            await conn.executemany(_INSERT_SQL, events)
    await db_log.log(
        PROGRESS_EVENT_TYPE,
        f"earnings backfill: {symbol} ← {beats} beats + {no_beats} no_beats "
        f"({len(rows)} FMP rows, window {start}..{end})",
        severity="INFO",
        data={
            "ticker": symbol,
            "beats_written": beats,
            "no_beats_written": no_beats,
            "fmp_rows": len(rows),
        },
    )
    return beats, no_beats


async def backfill_universe(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    universe: list[str],
    *,
    start: date = _DEFAULT_BACKFILL_START,
    end: date | None = None,
    resume: bool = True,
    inter_symbol_sleep_s: float = INTER_SYMBOL_SLEEP_S,
) -> dict[str, Any]:
    """Backfill every ticker in ``universe``.

    Resumable by default — queries ``application_log`` for tickers
    already completed in the past 30 days and skips them. Per-ticker
    failures are logged and the run continues; the final return dict
    carries the per-source counters and the failure list.

    ``end`` defaults to "today" — the FMP endpoint includes future
    earnings calendar entries with ``epsActual=null``, which
    ``_classify_earnings`` correctly rejects, so the upper bound just
    bounds CPU/network not data quality.
    """
    import asyncio
    from datetime import UTC, datetime

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY not set — cannot run earnings backfill")
    end_date = end or datetime.now(UTC).date()
    if resume:
        done = await already_completed_tickers(pool)
        pending = [t for t in universe if t not in done]
        skipped = len(universe) - len(pending)
    else:
        pending = list(universe)
        skipped = 0
    total_beats = 0
    total_no_beats = 0
    failures: list[str] = []
    succeeded: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for symbol in pending:
            try:
                beats, no_beats = await backfill_one_ticker(
                    pool, client, db_log, symbol, api_key,
                    start=start, end=end_date,
                )
            except Exception as exc:  # noqa: BLE001 — keep the run moving
                logger.error(
                    "earnings_backfill.ticker_failed",
                    ticker=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            total_beats += beats
            total_no_beats += no_beats
            succeeded.append(symbol)
            await asyncio.sleep(inter_symbol_sleep_s)
    return {
        "universe_size": len(universe),
        "resumed_skipped": skipped,
        "tickers_attempted": len(pending),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failures),
        "beats_written": total_beats,
        "no_beats_written": total_no_beats,
        "failures_sample": failures[:20],
    }


__all__ = [
    "EARNINGS_URL",
    "PROGRESS_EVENT_TYPE",
    "already_completed_tickers",
    "backfill_one_ticker",
    "backfill_universe",
    "enumerate_t1_t2_stock_universe",
]
