"""earnings_events monotone — per-ticker zero-tolerance non-decrease
invariant on EARNINGS_BEAT row counts.

``earnings_events_freshness`` validates that the table is *recent* and
that *coverage* across the active universe meets a floor. It is
structurally blind to a vendor TRUNCATION on the historical beat set:
a re-ingest that loses 30% of a ticker's older EARNINGS_BEAT rows
leaves the freshness check green (newest event still fresh, coverage
floor still met) while the engines silently lose historical earnings-
beat signal.

This check closes that hole with a *physical-truth invariant* mirroring
the prices_daily_completeness + corporate_actions_completeness +
sec_insider_monotone shape but keyed on per-ticker EARNINGS_BEAT
counts:

    For every ticker in ``platform.earnings_events``, the live
    ``COUNT(*) WHERE event_type='EARNINGS_BEAT'`` must be >= the
    snapshot recorded on the prior run. ANY per-ticker negative
    delta -> FAIL.

Why this exact shape:

* EARNINGS_BEAT rows are *historical events* — a Q2 2023 beat does not
  unhappen. Rows are never legitimately deleted.
* A re-ingestion that yields fewer beat rows for ANY ticker is a
  vendor truncation / API contract change — exactly the
  BAMLH0A0HYM2 / Sigma 22-site-drift failure mode the lifecycle is
  designed to surface.
* Zero-tolerance, no knob: even ONE beat row lost on ONE ticker is a
  fail. No percentage threshold (would hide small-cap truncations
  under mega-cap noise), no window (the invariant has nothing to do
  with recency — that belongs in ``earnings_events_freshness``).

KNOWN GAP — caveated explicitly (P1 follow-on, tracked in TODO.md):

    ``scripts/backfill_earnings_events.py::_classify_beat`` only emits
    a row when ``actual_eps > estimated_eps * 1.05``. MISS / IN-LINE
    earnings produce NO ROW. So this per-ticker monotone-non-decrease
    invariant catches **vendor truncation** (a re-ingest that drops
    historical beats), but it does NOT catch a **missed detection**
    from an FMP outage that would have written a beat row had the
    feed responded. The honest fix is to emit a ``NO_BEAT`` sentinel
    per quarter so per-quarter completeness becomes auditable — that
    is tracked as a P1 follow-on under the "Autonomous self-heal"
    section of TODO.md.

Architectural pair with sec_insider_monotone:

* sec_insider_transactions uses a separate per-ticker snapshot table
  in Postgres (``platform.sec_insider_row_counts_snapshot``) because
  the SEC ingest writes no CSV. The FMP earnings backfill is in the
  same shape — bulk-fetch per symbol, INSERT-on-conflict-do-nothing,
  no CSV archive. So the baseline must live in Postgres. The new
  ``platform.earnings_events_count_snapshot`` table is that durable
  baseline. PRIMARY KEY ``ticker``, UPSERT-on-success, one row per
  ticker (not a history).
* The read + compare + UPSERT runs in a single transaction so a crash
  mid-update can't poison the next cycle's baseline.

The healer ``compute_earnings_events_repair_targets`` calls the same
``_evaluate`` — detector and healer cannot disagree by construction.

The HealSpec re-pulls via the canonical ``earnings_refresh`` stage
with ``skip_guard_days=0`` so the bounded canonical re-pull actually
fires. Bounded by ``max_attempts=2``. Within those bounds the
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

CHECK_NAME = "earnings_events_monotone"

# Cap the per-failure surface in CheckResult.failures for log-size sanity.
# CheckResult.failed always carries the TRUE count so confidence reflects
# reality. Matches the corp_actions / fundamentals completeness /
# sec_insider_monotone cap.
MAX_REPORTED = 5

# Live per-ticker EARNINGS_BEAT counts on platform.earnings_events.
_LIVE_COUNTS_SQL = (
    "SELECT ticker, COUNT(*) AS beat_count "
    "FROM platform.earnings_events "
    "WHERE event_type = 'EARNINGS_BEAT' "
    "GROUP BY ticker"
)

# Prior per-ticker baseline. Locked FOR UPDATE inside the transaction so
# two concurrent runs cannot read-then-overwrite each other's view of
# the prior (UPSERT race protection).
_PRIOR_COUNTS_SQL = (
    "SELECT ticker, beat_count "
    "FROM platform.earnings_events_count_snapshot "
    "FOR UPDATE"
)

# UPSERT one row per ticker — PRIMARY KEY (ticker) drives the conflict
# target. ``snapshot_at`` is server-defaulted to now() on insert and
# explicitly bumped on conflict so a debugging operator can see when
# the baseline was last refreshed.
_UPSERT_SNAPSHOT_SQL = (
    "INSERT INTO platform.earnings_events_count_snapshot "
    "(ticker, beat_count, snapshot_at) "
    "VALUES ($1, $2, now()) "
    "ON CONFLICT (ticker) DO UPDATE SET "
    "beat_count = EXCLUDED.beat_count, snapshot_at = EXCLUDED.snapshot_at"
)


@dataclass(frozen=True)
class _Evaluation:
    """One monotone evaluation — shared by check + healer.

    The 4-tuples in ``decreased_tickers`` are
    ``(ticker, prior_beat_count, current_beat_count, delta)`` where
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
    ``compute_earnings_events_repair_targets`` (healing). Wraps the
    read + compare + write in a single transaction so a partial UPSERT
    can't poison the next cycle's baseline.

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
            r["ticker"]: int(r["beat_count"] or 0) for r in live_rows
        }
        prior_counts: dict[str, int] = {
            r["ticker"]: int(r["beat_count"] or 0) for r in prior_rows
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


async def check_earnings_events_monotone(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every ticker's live EARNINGS_BEAT row count >=
    its prior snapshot. First run seeds the baseline and passes.

    KNOWN GAP: BEAT-only ingestion means this catches truncation but
    not missed-detection. See module docstring + TODO.md P1 follow-on.
    """
    del source
    started = time.perf_counter()
    ev = await _evaluate(pool)

    if not ev.decreased_tickers:
        if ev.first_run:
            logger.info(
                "tpcore.validation.earnings_events_monotone.seeded",
                universe_size=ev.universe_size,
            )
        else:
            logger.info(
                "tpcore.validation.earnings_events_monotone.ok",
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
            reason="beat_count_decreased",
            expected=(
                f"earnings_events EARNINGS_BEAT COUNT(*) for {ticker} "
                f">= prior snapshot ({prior})"
            ),
            observed=(
                f"current beat_count={current} (delta={delta}, "
                f"snapshot={prior}). EARNINGS_BEAT is append-only — a "
                f"negative per-ticker delta is vendor truncation / "
                f"deletion event. Heal via canonical earnings_refresh "
                f"stage with skip_guard_days=0."
            ),
        ))
    logger.warning(
        "tpcore.validation.earnings_events_monotone.decreased",
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


async def compute_earnings_events_repair_targets(
    pool: asyncpg.Pool,
) -> list[str]:
    """Targets for the bounded auto-heal: the tickers whose
    EARNINGS_BEAT count decreased vs the prior snapshot.

    Returns ``[]`` when nothing to repair (clean OR first-run seed) —
    those are NOT a re-pull-fixable problem. Shares :func:`_evaluate`
    with the check; the healer can never target a different set than
    the detector reports.

    NOTE: the canonical ``earnings_refresh`` repair stage today
    re-pulls a default universe (see
    ``scripts/backfill_earnings_events.py``). The returned list is
    therefore advisory — for the orchestrator's telemetry and for the
    operator escalation surface, not for narrowing the stage's scope.
    If the earnings stage later gains a ``--tickers`` knob this list
    is already in the right shape to feed it.
    """
    ev = await _evaluate(pool)
    return [t for (t, _, _, _) in ev.decreased_tickers]


__all__ = [
    "CHECK_NAME",
    "MAX_REPORTED",
    "check_earnings_events_monotone",
    "compute_earnings_events_repair_targets",
]
