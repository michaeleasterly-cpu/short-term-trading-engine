"""Full-corpus FMP rebuild for ``platform.prices_daily``.

The 2026-05-22 corpus-fitness audit (PR #281) found that the existing
``platform.prices_daily`` is dual-sourced: pre-2026-05-22 history is
Alpaca-IEX (volume ~1/8 the consolidated CTA tape), post-2026-05-22 is
FMP-CTA. The mix yields a structural regime break at the switchover
date plus p95 OHLC drift of 1.5-2% on broad T2 sampling. AAPL's
2020-08-31 split day shows a 3.04% close disagreement between the two
sources — small enough to be missed by a 10-ticker mega-cap smoke test
yet large enough to bias every multi-year backtest that spans a split.

This module is THE single-source-of-truth restorer: it re-pulls full
historical OHLCV from FMP ``/stable/historical-price-eod/full`` for
every ticker known to ``platform.prices_daily`` (active + delisted),
upserts the rows back via the canonical (ticker, date) PK so Alpaca-
sourced rows are overwritten with FMP values, and emits a per-ticker
``CORPUS_REBUILD_TICKER_DONE`` event so a crash mid-run keeps completed
work.

Structurally symmetric to :mod:`tpcore.data.survivorship_backfill`
(PR #283 / #288) and :mod:`tpcore.data.insider_backfill` (PR #289):

* ``enumerate_corpus_universe`` — every ticker in
  ``platform.prices_daily`` (active + delisted). The FMP rebuild is a
  one-for-one re-source of what we already have, not a universe-
  expansion exercise.
* ``rebuild_one_ticker`` — pages FMP for one ticker over its full
  history (no date params = full FMP history, typically 2010+), upserts
  via the canonical PK + ON CONFLICT DO UPDATE so the prior rows are
  overwritten in place. Emits the progress event on completion.
* ``rebuild_universe`` — fan-out across the corpus universe with
  resume-via-application_log support.

Per the stream-long-running-output rule + the existing per-feed
audit-heal cascade: per-ticker completion is the resume granularity.

Wired into :mod:`scripts.ops` as the ``historical_prices_daily_fmp_rebuild``
stage (off-cycle, operator-on-demand). Run once after PR merge to
re-source the corpus to FMP-only; subsequent daily-bars runs (the daily
cadence already on FMP since PR #276) keep it single-source going
forward.

Per-ticker × ~7,000 tickers @ ~0.2s/call (200ms inter-request sleep,
300 req/min Starter cap) ≈ 25 min wall time.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from tpcore.data.ingest_fmp_bars import (
    _to_fmp_symbol,
    fetch_daily_bars_multi,
)
from tpcore.outage import DataProviderOutage

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Defaults — full-history pull horizon
# ──────────────────────────────────────────────────────────────────────


_DEFAULT_REBUILD_START = date(2010, 1, 1)
"""FMP Starter typically has 15 years of EOD history; 2010 captures the
post-GFC era which is the operative training window for every engine
in this codebase. Earlier rows in the corpus (e.g. orphan pre-2010
bars) are preserved by the (ticker, date) PK — this rebuild only
overwrites rows in [start, today]."""


# ──────────────────────────────────────────────────────────────────────
# Progress event — used by the resume probe
# ──────────────────────────────────────────────────────────────────────


PROGRESS_EVENT_TYPE = "CORPUS_REBUILD_TICKER_DONE"
"""Emitted to ``platform.application_log`` after each per-ticker
rebuild completes (including 0-row tickers). The resume probe queries
this event-type to skip already-done tickers on the next pass."""


async def already_completed_tickers(
    pool: asyncpg.Pool, *, lookback_days: int = 30,
) -> set[str]:
    """Tickers completed in the past N days (resume probe).

    30 days is far longer than any single rebuild run; it's the cushion
    for an interrupted multi-day operator workflow.
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
# Universe enumeration — every ticker in prices_daily (active + delisted)
# ──────────────────────────────────────────────────────────────────────


async def enumerate_corpus_universe(pool: asyncpg.Pool) -> list[str]:
    """Return every distinct ticker already present in
    ``platform.prices_daily`` (active + delisted).

    The rebuild is a one-for-one re-source of the existing corpus, NOT
    a universe expansion. New listings flow in via the daily-bars
    cadence; survivorship gaps are closed by the separate
    ``historical_delisted_universe`` stage. This stage only fixes the
    source-mix-discontinuity finding from PR #281.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            ORDER BY ticker
            """
        )
    return [r["ticker"] for r in rows]


# ──────────────────────────────────────────────────────────────────────
# Per-ticker rebuild — FMP fetch + canonical upsert
# ──────────────────────────────────────────────────────────────────────


def _upsert_sql() -> str:
    """Canonical (ticker, date) upsert for ``platform.prices_daily``.

    On conflict (same row already exists from any prior source — Alpaca
    or FMP), DO UPDATE so the FMP values overwrite the prior values.
    This is the load-bearing assertion of the rebuild: the corpus moves
    from dual-source to single-source-FMP without changing PK shape.

    ``delisted`` and ``delisting_date`` are NOT overwritten on conflict
    — those flags are owned by the survivorship-backfill stage; the
    rebuild only re-sources OHLCV + sets source='fmp'.
    """
    return """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'fmp')
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            source = 'fmp'
    """


def _physical_truth_rows(
    symbol: str,
    bars: list[dict[str, Any]],
) -> list[tuple]:
    """Translate FMP-shape bars to upsert rows, rejecting bad bars.

    Mirrors the gate in :func:`tpcore.data.ingest_alpaca_bars._upsert_bars`
    — close > 0, OHLC consistent, volume >= 0, no future dates. Bad
    rows are dropped (not zero-filled) so the corpus stays clean.
    """
    today = datetime.now(UTC).date()
    out: list[tuple] = []
    for b in bars:
        try:
            ts = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        session_date = ts.date()
        o = b.get("o")
        h = b.get("h")
        low = b.get("l")
        c = b.get("c")
        v = b.get("v")
        if o is None or h is None or low is None or c is None or v is None:
            continue
        try:
            of, hf, lf, cf = float(o), float(h), float(low), float(c)
        except (TypeError, ValueError):
            continue
        if cf <= 0 or cf > 1e8 or of <= 0 or hf <= 0 or lf <= 0:
            continue
        if hf < max(of, cf, lf) or lf > min(of, cf, hf):
            continue
        if session_date > today:
            continue
        out.append((
            symbol, session_date, of, hf, lf, cf, int(v),
            cf,  # adjusted_close — FMP returns adjusted close in /full
        ))
    return out


async def rebuild_one_ticker(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    *,
    start: date = _DEFAULT_REBUILD_START,
    end: date | None = None,
) -> int:
    """Re-pull full FMP history for ``symbol`` and overwrite the
    corresponding ``prices_daily`` rows.

    Writes a ``CORPUS_REBUILD_TICKER_DONE`` event on every successful
    per-ticker call so the resume probe sees the work. A permanent FMP
    failure (DataProviderOutage) propagates — the universe-level catch
    logs it and continues to the next ticker.

    Returns the number of bars upserted (0 if FMP has no data for the
    symbol — common for very-old delisted tickers).
    """
    end_date = end or datetime.now(UTC).date()
    bars_by_symbol = await fetch_daily_bars_multi(
        client, [symbol], start, end_date,
    )
    bars = bars_by_symbol.get(symbol, [])
    if not bars:
        await db_log.log(
            PROGRESS_EVENT_TYPE,
            f"corpus rebuild: {symbol} returned 0 bars from FMP",
            severity="INFO",
            data={
                "ticker": symbol,
                "bars_written": 0,
                "fmp_symbol": _to_fmp_symbol(symbol),
            },
        )
        return 0
    rows = _physical_truth_rows(symbol, bars)
    if not rows:
        await db_log.log(
            PROGRESS_EVENT_TYPE,
            f"corpus rebuild: {symbol} had bars but all rejected by "
            "physical-truth gate",
            severity="WARNING",
            data={
                "ticker": symbol,
                "bars_written": 0,
                "fmp_bars": len(bars),
            },
        )
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(_upsert_sql(), rows)
    await db_log.log(
        PROGRESS_EVENT_TYPE,
        f"corpus rebuild: {symbol} ← {len(rows)} bars",
        severity="INFO",
        data={
            "ticker": symbol,
            "bars_written": len(rows),
            "start_date": start.isoformat(),
            "end_date": end_date.isoformat(),
        },
    )
    return len(rows)


async def rebuild_universe(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    universe: list[str],
    *,
    start: date = _DEFAULT_REBUILD_START,
    end: date | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Rebuild every ticker in ``universe`` from FMP.

    Resumable by default — queries ``application_log`` for tickers
    already completed in the past 30 days and skips them. Per-ticker
    permanent failures are logged and the run continues; the final
    return dict carries counters + a failure sample.
    """
    if resume:
        done = await already_completed_tickers(pool)
        pending = [t for t in universe if t not in done]
        skipped = len(universe) - len(pending)
    else:
        pending = list(universe)
        skipped = 0
    total_bars = 0
    failures: list[str] = []
    succeeded: list[str] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for symbol in pending:
            try:
                bars = await rebuild_one_ticker(
                    pool, client, db_log, symbol,
                    start=start, end=end,
                )
            except DataProviderOutage as exc:
                logger.error(
                    "corpus_rebuild.ticker_outage",
                    ticker=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:outage")
                continue
            except Exception as exc:  # noqa: BLE001 — keep the run moving
                logger.error(
                    "corpus_rebuild.ticker_failed",
                    ticker=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            total_bars += bars
            succeeded.append(symbol)
    return {
        "universe_size": len(universe),
        "resumed_skipped": skipped,
        "tickers_attempted": len(pending),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failures),
        "bars_written": total_bars,
        "failures_sample": failures[:20],
    }


def _fmp_api_key_present() -> bool:
    """Soft check the FMP key is present without raising — the stage's
    own pre-flight raises for actionable error surfacing; this lets
    unit tests inspect the module without an env var."""
    return bool(os.environ.get("FMP_API_KEY"))


__all__ = [
    "PROGRESS_EVENT_TYPE",
    "already_completed_tickers",
    "enumerate_corpus_universe",
    "rebuild_one_ticker",
    "rebuild_universe",
]
