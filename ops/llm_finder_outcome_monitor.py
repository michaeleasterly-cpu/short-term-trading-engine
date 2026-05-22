"""Phase E + F live-paper outcome monitor — Task #25 §3.2.

Reads finder-emitted PAPER engines from ``application_log``
(``LAB_FINDER_ACTION(action='merge')`` rows not followed by
``action='ecr_retire'``); computes ``LiveOutcome`` per engine from
``aar_events`` + ``open_orders`` + ``risk_state``; surfaces to the §12
dashboard via ``LAB_FINDER_OUTCOME_CHECK`` events. Drives Phase F:

- F1 ``outcome_proven=True`` — only on operator-posted
  ``LAB_FINDER_OUTCOME_VERDICT(verdict='success')``.
- F2 auto-retire — fires on (a) mechanical bleed-cap breach
  (constraint 15 / 18: per-engine $5k OR global $15k), (b) operator
  ``verdict='failure'``, OR (c) inactivity timeout (constraint 19).

The monitor is the autonomous loop's "operator-eyes" surface: it
SURFACES `LiveOutcome` to the dashboard + READS the operator-posted
verdict event. It does NOT pre-register Sharpe / DD thresholds — "I
know it when I see it" is operator-discretion at the §12 surface.

This module is invoked from ``ops.llm_triage_service`` as the 5th
crash-isolated co-task (T10 wired the 4th; this is the next).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog

from tpcore.lab.llm_finder import (
    BLEED_CAP_PER_ENGINE_USD,
    GLOBAL_BLEED_PAUSE_THRESHOLD_USD,
    GLOBAL_FINDER_BLEED_CAP_USD,
    INACTIVITY_AUTO_RETIRE_SESSIONS,
    MIN_TRADE_COUNT_FOR_NO_VERDICT,
)
from tpcore.lab.llm_finder.models import LiveOutcome
from tpcore.lab.llm_finder.run_writer import record_finder_action

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)

# Trigger-event types the daemon co-task polls (Phase E session-close
# events; v1 keeps empty by design — the slash-skill + live-paper
# monitor cron fire this independently; the daemon-poll path is the
# Phase E.5 follow-up).
OUTCOME_MONITOR_TRIGGER_EVENT_TYPES: tuple[str, ...] = ()


# ───────────────────────── SQL ─────────────────────────


_FINDER_PAPER_ENGINES_SQL = """
    SELECT DISTINCT (payload->>'engine') AS engine,
           MIN(ts) AS first_promoted_ts
      FROM platform.application_log
     WHERE event_type = 'LAB_FINDER_ACTION'
       AND (payload->>'action') IN ('merge', 'ecr_modify')
       AND (payload->>'engine') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM platform.application_log a2
             WHERE a2.event_type = 'LAB_FINDER_ACTION'
               AND (a2.payload->>'engine') = (application_log.payload->>'engine')
               AND (a2.payload->>'action') = 'ecr_retire'
       )
     GROUP BY (payload->>'engine')
"""

_AAR_TRADES_SQL = """
    SELECT realised_pnl_usd, unrealised_pnl_usd, opened_at, closed_at
      FROM platform.aar_events
     WHERE engine = $1
       AND opened_at >= $2
     ORDER BY opened_at
"""

_OPERATOR_VERDICT_SQL = """
    SELECT payload->>'verdict' AS verdict,
           payload->>'operator_note' AS operator_note,
           ts
      FROM platform.application_log
     WHERE event_type = 'LAB_FINDER_OUTCOME_VERDICT'
       AND (payload->>'engine') = $1
     ORDER BY ts DESC
     LIMIT 1
"""

_OUTCOME_CHECK_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_edge_finder', $1, 'LAB_FINDER_OUTCOME_CHECK', 'INFO',
         $2, $3::jsonb)
"""


# ───────────────────────── Monitor entry point ─────────────────────────


async def monitor_finder_emitted_paper_engines(
    pool: asyncpg.Pool,
    *,
    as_of_session: date | None = None,
) -> tuple[LiveOutcome, ...]:
    """Compute LiveOutcome per finder-emitted PAPER engine + emit
    LAB_FINDER_OUTCOME_CHECK events + drive Phase F.

    Returns the tuple of LiveOutcomes computed this tick (for the
    test seam + the daemon log).
    """
    session_date = as_of_session or datetime.now(UTC).date()
    log.info("outcome_monitor.tick.start", session_date=str(session_date))

    paper_engines = await _read_finder_paper_engines(pool)
    if not paper_engines:
        log.info("outcome_monitor.tick.no_engines")
        return ()

    outcomes: list[LiveOutcome] = []
    aggregate_bleed = 0.0
    for engine, first_promoted_ts in paper_engines:
        lo = await _compute_live_outcome(
            pool, engine=engine, first_promoted_ts=first_promoted_ts, as_of_session=session_date
        )
        aggregate_bleed += lo.cumulative_bleed_usd
        outcomes.append(lo)

    # Phase E.3 — surface every outcome to the §12 dashboard.
    for lo in outcomes:
        await _emit_outcome_check(pool, lo)

    # Phase E.4 / F — evaluate the two autonomous-loop inputs per engine.
    for lo in outcomes:
        await _evaluate_phase_f(pool, lo, aggregate_bleed=aggregate_bleed)

    log.info(
        "outcome_monitor.tick.done",
        engines=len(outcomes),
        aggregate_bleed=aggregate_bleed,
        global_pause=(aggregate_bleed >= GLOBAL_BLEED_PAUSE_THRESHOLD_USD),
    )
    return tuple(outcomes)


# ───────────────────────── Sub-queries + classifier ─────────────────────────


async def _read_finder_paper_engines(
    pool: asyncpg.Pool,
) -> list[tuple[str, datetime]]:
    """Find engines promoted by the finder + not yet retired."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FINDER_PAPER_ENGINES_SQL)
    return [(r["engine"], r["first_promoted_ts"]) for r in rows]


async def _compute_live_outcome(
    pool: asyncpg.Pool,
    *,
    engine: str,
    first_promoted_ts: datetime,
    as_of_session: date,
) -> LiveOutcome:
    """Aggregate AAR rows + apply operator-verdict + classify auto-retire."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_AAR_TRADES_SQL, engine, first_promoted_ts)
        verdict_row = await conn.fetchrow(_OPERATOR_VERDICT_SQL, engine)

    realised = sum(float(r.get("realised_pnl_usd") or 0.0) for r in rows)
    unrealised = sum(float(r.get("unrealised_pnl_usd") or 0.0) for r in rows)
    cumulative_bleed = max(0.0, -(realised + unrealised))
    trade_count = len(rows)
    session_count = max(
        1,
        (as_of_session - first_promoted_ts.date()).days
        if first_promoted_ts and as_of_session >= first_promoted_ts.date()
        else 1,
    )

    operator_verdict: Literal["none", "success", "failure"] = "none"
    if verdict_row:
        v = verdict_row.get("verdict")
        if v in ("success", "failure"):
            operator_verdict = v  # type: ignore[assignment]

    # Phase F classifier — auto-retire fires on either mechanical bleed
    # OR operator failure OR inactivity timeout.
    auto_retire_triggered, auto_retire_reason = _classify_auto_retire(
        cumulative_bleed_usd=cumulative_bleed,
        session_count=session_count,
        trade_count_total=trade_count,
        operator_verdict=operator_verdict,
    )

    return LiveOutcome(
        engine=engine,
        as_of_session=as_of_session,
        session_count=session_count,
        pnl_realised_total_usd=realised,
        pnl_unrealised_total_usd=unrealised,
        sharpe_30d_net_costs_hac=None,  # descriptive surface; computed by dashboard
        max_single_session_drawdown_pct=None,
        cumulative_bleed_usd=cumulative_bleed,
        trade_count_total=trade_count,
        operator_verdict=operator_verdict,
        auto_retire_triggered=auto_retire_triggered,
        auto_retire_reason=auto_retire_reason,
    )


def _classify_auto_retire(
    *,
    cumulative_bleed_usd: float,
    session_count: int,
    trade_count_total: int,
    operator_verdict: Literal["none", "success", "failure"],
) -> tuple[bool, Literal["none", "bleed_cap", "operator_failure", "inactivity_timeout", "global_bleed_cap"]]:
    """Return (triggered, reason). Pure; testable without I/O."""
    if cumulative_bleed_usd >= BLEED_CAP_PER_ENGINE_USD:
        return True, "bleed_cap"
    if operator_verdict == "failure":
        return True, "operator_failure"
    if (
        session_count >= INACTIVITY_AUTO_RETIRE_SESSIONS
        and trade_count_total < MIN_TRADE_COUNT_FOR_NO_VERDICT
        and operator_verdict == "none"
    ):
        return True, "inactivity_timeout"
    return False, "none"


# ───────────────────────── Event writers ─────────────────────────


async def _emit_outcome_check(pool: asyncpg.Pool, lo: LiveOutcome) -> None:
    """Phase E.3 — write LAB_FINDER_OUTCOME_CHECK for the §12 dashboard."""
    import uuid as _uuid
    payload = lo.model_dump(mode="json")
    # Monitor-tick rows use the NIL UUID — the row family is the
    # LiveOutcome state (per engine) not the run-id chain.
    rid_nil = _uuid.UUID(int=0)
    message = f"{lo.engine} bleed={lo.cumulative_bleed_usd:.2f} verdict={lo.operator_verdict}"
    async with pool.acquire() as conn:
        await conn.execute(
            _OUTCOME_CHECK_INSERT_SQL, rid_nil, message, json.dumps(payload)
        )
    log.info(
        "outcome_monitor.check.emitted",
        engine=lo.engine,
        bleed=lo.cumulative_bleed_usd,
        verdict=lo.operator_verdict,
    )


async def _evaluate_phase_f(
    pool: asyncpg.Pool,
    lo: LiveOutcome,
    *,
    aggregate_bleed: float,
) -> None:
    """Drive Phase F1/F2 actions based on the LiveOutcome."""
    # F1: outcome_proven on operator success.
    if lo.operator_verdict == "success":
        await record_finder_action(
            pool,
            run_id="(monitor)",
            action="outcome_proven",
            triggered_by="operator_verdict",
            extra={"engine": lo.engine, "cumulative_bleed_usd": lo.cumulative_bleed_usd},
        )
        log.info("outcome_monitor.outcome_proven", engine=lo.engine)
        return

    # F2: auto-retire on any of the three triggers.
    if lo.auto_retire_triggered:
        await record_finder_action(
            pool,
            run_id="(monitor)",
            action="ecr_retire",
            triggered_by=lo.auto_retire_reason,
            extra={
                "engine": lo.engine,
                "cumulative_bleed_usd": lo.cumulative_bleed_usd,
                "session_count": lo.session_count,
                "trade_count_total": lo.trade_count_total,
            },
        )
        log.warning(
            "outcome_monitor.auto_retire",
            engine=lo.engine,
            reason=lo.auto_retire_reason,
        )
        # ECR-RETIRE machine path is invoked here — in v1.5 the actual
        # ECR file write + python -m ops.engine_sdlc --ecr invocation
        # ships; v1 logs the action + leaves the operator to verify.
        return

    # F-global: aggregate bleed check (constraint 18).
    if aggregate_bleed >= GLOBAL_FINDER_BLEED_CAP_USD:
        await record_finder_action(
            pool,
            run_id="(monitor)",
            action="ecr_retire",
            triggered_by="global_bleed_cap",
            extra={
                "engine": lo.engine,
                "aggregate_bleed_usd": aggregate_bleed,
            },
        )
        log.warning(
            "outcome_monitor.global_bleed_cap",
            aggregate_bleed=aggregate_bleed,
            engine=lo.engine,
        )


# ───────────────────────── Co-task entry (for daemon) ─────────────────────────


async def run_outcome_monitor_cotask(
    pool: asyncpg.Pool, trigger_event: Any
) -> None:
    """Daemon co-task entry — invoked by ops.llm_triage_service.

    Per spec §3.2 Phase E: runs once per session-close trigger event.
    v1's trigger set is empty (the cron / slash-skill fires this
    independently); the polling path is structurally present for the
    Phase E.5 follow-up.
    """
    _ = trigger_event  # accepted-but-unused for the v1 daemon poll path
    await monitor_finder_emitted_paper_engines(pool)


__all__ = [
    "OUTCOME_MONITOR_TRIGGER_EVENT_TYPES",
    "_classify_auto_retire",
    "monitor_finder_emitted_paper_engines",
    "run_outcome_monitor_cotask",
]
