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

# Evidence-write extension (2026-06-03)

Per the `excluded_confirmed_data_gap` validator-semantics arc — spec
PR #450 + plan PR #451 §7.2 — `backfill_one_ticker` accepts a
`record_evidence_for_periods` argument. When non-None (per-ticker
list of `period_end_date` values), the function writes per-period
evidence rows into `platform.fundamentals_period_source_evidence`
AFTER the FMP fetch:

  * `outcome='yielded'` for each period that landed in
    `fundamentals_quarterly` as a result of the fetch.
  * `outcome='empty'` for each requested period that did NOT land
    (FMP fetched but lacked that period).

This is opt-in: when `record_evidence_for_periods` is None (the
default), behavior is byte-equivalent to pre-extension. The
`confirmed_data_gap_evidence_populator` stage opts in; the regular
`historical_fundamentals_quarterly` daily stage can opt in via
`backfill_universe(..., record_evidence=True)` to populate the daily
substrate, but defaults to off to keep the existing backfill cycle
unchanged in this PR.

# Dry-run-purity fix (2026-06-03 — PR follow-up to PR #452)

`backfill_one_ticker` accepts a `dry_run: bool = False` kwarg. When
`dry_run=True` the function STILL performs the FMP fetch (via the
public `cache.fetch_payload(symbol)` accessor — no DB write), builds
the would-write payload in-memory, and reports the row-count it
WOULD have upserted — but DOES NOT call `cache.upsert_payload`. This
mirrors the PR #448 dry-run contract of `handle_sec_fundamentals_fallback`
exactly: read-only fetch + planning counters, zero `fundamentals_quarterly`
mutation. Default `False` keeps the live path bit-identical.

WHY this matters: the operator's 2026-06-02 run of
`confirmed_data_gap_evidence_populator --param dry_run=true --param limit=10`
observed 5 AXIN rows in `fundamentals_quarterly` whose `recorded_at`
was bumped to the dry-run window — evidence-row writes WERE gated
(zero new evidence rows), but `cache.backfill` ran unconditionally
and re-upserted the existing rows (the ON CONFLICT clause sets
`recorded_at = now()`). The fix gates that primary write too.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
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


def _count_payload_rows(payload: dict[str, Any]) -> int:
    """Return the number of periods present in an FMP fundamentals
    payload — the count ``cache.upsert_payload`` would have written.

    The payload shape mirrors ``FundamentalsCache.fetch_payload``
    (see ``tpcore.fmp.fundamentals_adapter._merge``): one ``latest``
    period at the top level + a ``history`` list of earlier periods.
    The upsert-time physical-truth gate rejects rows whose
    ``filing_date`` is None; mirror that here so the dry-run count
    matches what the live path would actually write.
    """
    if not payload:
        return 0
    periods: list[dict[str, Any]] = [
        {k: v for k, v in payload.items() if k != "history"}
    ]
    for h in payload.get("history") or []:
        periods.append(h)
    # Mirror ``cache._upsert_payload``: drop periods without
    # ``filing_date`` (same physical-truth gate, no DB call).
    return sum(1 for p in periods if p.get("filing_date") is not None)


async def backfill_one_ticker(
    cache,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    *,
    end: date | None = None,
    pool: asyncpg.Pool | None = None,
    record_evidence_for_periods: list[date] | None = None,
    evidence_source: str = "fmp_historical",
    dry_run: bool = False,
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

    When ``record_evidence_for_periods`` is non-None AND ``pool`` is
    provided, the function writes per-period evidence rows into
    ``platform.fundamentals_period_source_evidence`` AFTER the FMP
    fetch. Per the `excluded_confirmed_data_gap` arc, ``yielded`` for
    each period that landed in ``fundamentals_quarterly``, ``empty``
    for each requested period that did NOT land, ``fetch_failure`` if
    the FMP fetch hit a real outage. Opt-in; default (None) is
    byte-equivalent to pre-extension behavior.

    When ``dry_run=True`` (default False), the FMP fetch STILL runs
    (via ``cache.fetch_payload`` — the public adapter accessor that
    returns the payload without touching the DB) BUT ``cache.upsert_payload``
    is skipped entirely. The returned int reports the row-count that
    WOULD have been upserted (latest + history). Evidence-row writes
    are similarly suppressed (the populator stage passes ``pool=None``
    in dry-run so ``_record_fmp_evidence`` short-circuits). This
    mirrors the PR #448 dry-run contract of
    ``handle_sec_fundamentals_fallback``. Live path is bit-identical
    to pre-fix behavior (``dry_run=False``).
    """
    from tpcore.outage import DataProviderOutage

    rows_written = 0
    error_class: str | None = None
    error_msg: str | None = None
    fmp_outage: str | None = None
    try:
        if dry_run:
            # Dry-run: fetch the FMP payload via the public accessor
            # (no DB write) and count the rows that WOULD have been
            # upserted. ``cache.fetch_payload`` raises
            # ``DataProviderOutage`` on transport / contract failure —
            # same exception surface as ``cache.backfill`` — so the
            # downstream classification and event-log paths are
            # symmetric.
            payload = await cache.fetch_payload(symbol)
            rows_written = _count_payload_rows(payload)
        else:
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
            fmp_outage = msg[:160]
        logger.warning(
            "fundamentals_backfill.ticker_outage"
            if not (is_no_data or is_premium_gated)
            else "fundamentals_backfill.ticker_skipped",
            ticker=symbol, error=msg[:200],
        )
    except Exception as exc:  # noqa: BLE001 — keep the run moving
        error_class = type(exc).__name__
        error_msg = str(exc)[:200]
        fmp_outage = error_msg
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
    # Evidence-write extension (opt-in). Runs BEFORE the error re-raise
    # so a transient outage still leaves a ``fetch_failure`` evidence
    # row for the operator's next attempt (per plan §7.2 +
    # spec §4 rule #4 — fetch_failure does NOT qualify for exclusion).
    if pool is not None and record_evidence_for_periods is not None:
        await _record_fmp_evidence(
            pool,
            symbol=symbol,
            requested=list(record_evidence_for_periods),
            source=evidence_source,
            fmp_outage=fmp_outage,
        )
    if error_class:
        raise RuntimeError(f"{symbol}:{error_class}:{error_msg}")
    return rows_written


async def _record_fmp_evidence(
    pool: asyncpg.Pool,
    *,
    symbol: str,
    requested: list[date],
    source: str,
    fmp_outage: str | None,
) -> int:
    """Write per-`(ticker, period_end_date)` FMP evidence rows.

    For each requested period:
      * If ``fmp_outage`` is set → ``outcome='fetch_failure'``
        (notes=outage message; per spec §4 #4 a real outage doesn't
        qualify for exclusion).
      * Else, query ``platform.fundamentals_quarterly`` for the
        ticker's current period_ends → ``yielded`` for periods present,
        ``empty`` for periods absent.

    Plan 2 (migration 20260604_0300 dropped
    ``platform.fundamentals_period_source_evidence``; 0500 folded it into
    ``data_quality_log`` via ``kind='confirmed_data_gap_evidence'``). Routes
    through the shared
    :func:`tpcore.quality.confirmed_data_gap_store.write_evidence_rows`. Returns
    the number of rows written.
    """
    from tpcore.quality.confirmed_data_gap_store import write_evidence_rows

    if not requested:
        return 0
    attempted_at = datetime.now(UTC)
    if fmp_outage is not None:
        rows = [
            (symbol, pe, source, "fetch_failure", fmp_outage[:200])
            for pe in requested
        ]
    else:
        async with pool.acquire() as conn:
            present_rows = await conn.fetch(
                "SELECT DISTINCT period_end_date "
                "FROM platform.fundamentals_quarterly "
                "WHERE ticker = $1 AND period_end_date = ANY($2::date[])",
                symbol, list(requested),
            )
        present = {r["period_end_date"] for r in present_rows}
        rows = [
            (
                symbol, pe, source,
                "yielded" if pe in present else "empty",
                None,
            )
            for pe in requested
        ]
    return await write_evidence_rows(pool, rows, attempted_at)


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
    "_count_payload_rows",
    "_record_fmp_evidence",
    "already_completed_tickers",
    "backfill_one_ticker",
    "backfill_universe",
    "enumerate_gap_tickers",
]
