"""sec_insider_transactions monotone — per-ticker zero-tolerance
non-decrease invariant for Form 4 insider transactions.

``sec_filings_freshness`` validates that the union of Form 4 + 8-K is
*recent*. It is structurally blind to a vendor TRUNCATION on the
historical Form 4 set: a re-ingest that loses 30% of a ticker's older
Form 4 rows leaves the freshness check green (newest filing still
fresh) while the engines silently lose historical insider signal.

This check closes that hole with a *physical-truth invariant* mirroring
the prices_daily_completeness + corporate_actions_completeness shape but
keyed on per-ticker counts:

    For every ticker in ``platform.insider_transactions``, the live
    ``COUNT(*)`` must be ≥ the snapshot recorded on the prior run. ANY
    per-ticker negative delta → FAIL.

Why this exact shape:

* Form 4 transactions are *historical events* — once filed, a 2019
  Form 4 line does not unhappen. Rows are never legitimately deleted.
* A re-ingestion that yields fewer rows for ANY ticker is a vendor
  truncation / API contract change — exactly the BAMLH0A0HYM2 /
  Sigma 22-site-drift failure mode the lifecycle is designed to
  surface.
* Zero-tolerance, no knob: even ONE row lost on ONE ticker is a fail.
  No percentage threshold (would hide small-cap truncations under
  mega-cap noise), no window (the invariant has nothing to do with
  recency — that belongs in ``sec_filings_freshness``).

Architectural pair with corporate_actions_completeness:

* corp_actions uses the CSV-archive shrinkage primitive because the
  corp-actions ingest writes a CSV snapshot to
  ``tpcore.ingestion.csv_archive``. The SEC ingest does NOT — it pulls
  per-issuer submissions + 8-K item indexes, never staging to CSV. So
  the baseline must live in Postgres. The new
  ``platform.sec_insider_row_counts_snapshot`` table is that durable
  baseline. PRIMARY KEY ``ticker``, UPSERT-on-success, one row per
  ticker (not a history).
* The read + compare + UPSERT runs in a single transaction so a crash
  mid-update can't poison the next cycle's baseline (a partial UPSERT
  would lower some tickers' "prior" and silently green a real shrink
  on the next run).

The healer ``compute_sec_monotone_repair_targets`` calls the same
``_evaluate`` — detector and healer cannot disagree by construction.

The HealSpec re-pulls via the canonical ``sec_filings`` stage's
``repair=true`` mode (full T1+T2 stock universe, 200d lookback,
skip-guard off — the same path the ``sec_filings_freshness`` healer
already uses). Bounded by ``max_attempts=2``. Within those bounds the
invariant is absolute — there is deliberately no percentage knob, no
recency window. Those are exactly the knobs that let a vendor
truncation hide.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "sec_insider_monotone"

# Cap the per-failure surface in CheckResult.failures for log-size sanity.
# CheckResult.failed always carries the TRUE count so confidence reflects
# reality. Matches the corp_actions / fundamentals completeness cap.
MAX_REPORTED = 5

# Live per-ticker counts on platform.insider_transactions.
_LIVE_COUNTS_SQL = (
    "SELECT ticker, COUNT(*) AS rowcount "
    "FROM platform.insider_transactions "
    "GROUP BY ticker"
)

# Prior per-ticker baseline. Locked FOR UPDATE inside the transaction so
# two concurrent runs cannot read-then-overwrite each other's view of
# the prior (UPSERT race protection).
_PRIOR_COUNTS_SQL = (
    "SELECT ticker, rowcount "
    "FROM platform.sec_insider_row_counts_snapshot "
    "FOR UPDATE"
)

# UPSERT one row per ticker — PRIMARY KEY (ticker) drives the conflict
# target. ``snapshot_at`` is server-defaulted to now() on insert and
# explicitly bumped on conflict so a debugging operator can see when
# the baseline was last refreshed.
_UPSERT_SNAPSHOT_SQL = (
    "INSERT INTO platform.sec_insider_row_counts_snapshot "
    "(ticker, rowcount, snapshot_at) "
    "VALUES ($1, $2, now()) "
    "ON CONFLICT (ticker) DO UPDATE SET "
    "rowcount = EXCLUDED.rowcount, snapshot_at = EXCLUDED.snapshot_at"
)


@dataclass(frozen=True)
class _Evaluation:
    """One monotone evaluation — shared by check + healer.

    The 4-tuples in ``decreased_tickers`` are
    ``(ticker, prior_rowcount, current_rowcount, delta)`` where
    ``delta = current - prior`` (negative on a shrink).
    ``current_counts`` carries the FULL live-DB per-ticker snapshot —
    the check uses it to UPSERT the new baseline on PASS, and it is
    also informative to a triage operator on FAIL (universe size).
    """

    decreased_tickers: list[tuple[str, int, int, int]]
    universe_size: int
    tickers_with_history: int
    first_run: bool
    current_counts: dict[str, int] = field(default_factory=dict)


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once + UPSERT the new baseline atomically.

    Single source of truth for both ``check`` (detection) and
    ``compute_sec_monotone_repair_targets`` (healing). Wraps the read +
    compare + write in a single transaction so a partial UPSERT can't
    poison the next cycle's baseline.

    First-run behavior (snapshot table empty) returns
    ``first_run=True`` with an empty ``decreased_tickers`` list and
    seeds the baseline — subsequent runs gate against it. This is the
    deliberate symmetric bootstrap pattern: the check's FIRST visit
    sets the floor, every subsequent visit enforces it.
    """
    async with pool.acquire() as conn, conn.transaction():
        live_rows = await conn.fetch(_LIVE_COUNTS_SQL)
        prior_rows = await conn.fetch(_PRIOR_COUNTS_SQL)

        current_counts: dict[str, int] = {
            r["ticker"]: int(r["rowcount"] or 0) for r in live_rows
        }
        prior_counts: dict[str, int] = {
            r["ticker"]: int(r["rowcount"] or 0) for r in prior_rows
        }
        first_run = not prior_counts

        decreased: list[tuple[str, int, int, int]] = []
        if not first_run:
            for ticker, prior in prior_counts.items():
                current = current_counts.get(ticker, 0)
                if current < prior:
                    decreased.append(
                        (ticker, prior, current, current - prior)
                    )
            # Stable ordering — biggest absolute drop first for triage.
            decreased.sort(key=lambda t: (t[3], t[0]))

        # Only UPSERT the baseline when the compare passes. A FAIL is a
        # truncation event — refusing to write the lower count keeps
        # the historical floor in place so the healer can re-validate
        # against the ORIGINAL baseline, not the truncated one.
        if not decreased:
            for ticker, count in current_counts.items():
                await conn.execute(_UPSERT_SNAPSHOT_SQL, ticker, count)

    return _Evaluation(
        decreased_tickers=decreased,
        universe_size=len(current_counts),
        tickers_with_history=len(prior_counts),
        first_run=first_run,
        current_counts=current_counts,
    )


async def check_sec_insider_monotone(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every ticker's live row count ≥ its prior
    snapshot. First run seeds the baseline and passes."""
    del source
    started = time.perf_counter()
    ev = await _evaluate(pool)

    if not ev.decreased_tickers:
        if ev.first_run:
            logger.info(
                "tpcore.validation.sec_insider_monotone.seeded",
                universe_size=ev.universe_size,
            )
        else:
            logger.info(
                "tpcore.validation.sec_insider_monotone.ok",
                universe_size=ev.universe_size,
                tickers_with_history=ev.tickers_with_history,
            )
        return CheckResult(
            name=CHECK_NAME,
            passed=True,
            total=max(ev.universe_size, 1),
            failed=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[],
        )

    failures: list[FailureDetail] = []
    for ticker, prior, current, delta in ev.decreased_tickers[:MAX_REPORTED]:
        failures.append(FailureDetail(
            ticker=ticker,
            reason="rowcount_decreased",
            expected=(
                f"sec_insider_transactions COUNT(*) for {ticker} ≥ prior "
                f"snapshot ({prior})"
            ),
            observed=(
                f"current rowcount={current} (delta={delta}, "
                f"snapshot={prior}). Form 4 is append-only — a negative "
                f"per-ticker delta is vendor truncation / deletion event. "
                f"Heal via canonical sec_filings stage with repair=true."
            ),
        ))
    logger.warning(
        "tpcore.validation.sec_insider_monotone.decreased",
        offending_tickers=len(ev.decreased_tickers),
        universe_size=ev.universe_size,
        tickers_with_history=ev.tickers_with_history,
    )
    return CheckResult(
        name=CHECK_NAME,
        passed=False,
        total=max(ev.universe_size, 1),
        failed=len(ev.decreased_tickers),
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures,
    )


async def compute_sec_monotone_repair_targets(
    pool: asyncpg.Pool,
) -> list[str]:
    """Targets for the bounded auto-heal: the tickers whose Form 4
    rowcount decreased vs the prior snapshot.

    Returns ``[]`` when nothing to repair (clean OR first-run seed) —
    those are NOT a re-pull-fixable problem. Shares :func:`_evaluate`
    with the check; the healer can never target a different set than
    the detector reports.

    NOTE: the canonical ``sec_filings`` repair stage today re-pulls the
    full T1+T2 stock universe (``max_tickers=None``). The returned
    list is therefore advisory — for the orchestrator's telemetry and
    for the operator escalation surface, not for narrowing the stage's
    scope. If the SEC stage later gains a ``--tickers`` knob this list
    is already in the right shape to feed it.
    """
    ev = await _evaluate(pool)
    return [t for (t, _, _, _) in ev.decreased_tickers]


__all__ = [
    "CHECK_NAME",
    "MAX_REPORTED",
    "check_sec_insider_monotone",
    "compute_sec_monotone_repair_targets",
]
