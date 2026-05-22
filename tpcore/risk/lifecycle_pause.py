"""Credibility-drop + lifecycle-degradation auto-pause — Wave-4 rows
E7 (credibility) and E11 (lifecycle) of the deterministic self-heal
expansion.

Reference: ``docs/superpowers/specs/2026-05-21-deterministic-self-heal-
coverage-expansion-design.md`` rows E7 + E11 + §4 answer #5.

Design summary:

Two AAR-adjacent trend signals — engine credibility (E7) and engine
lifecycle health (E11) — produce a per-cycle health datum each. Today
the data is captured (credibility via :func:`tpcore.backtest.
statistical_validation.write_credibility_score`; lifecycle via the
plug's own ``log_lifecycle_health`` writes) but no deterministic
auto-pause exists when the trend degrades.

Wave-4 inserts a post-cycle check (one helper per row, sharing the
rolling-window logic) that:

1. Reads the last N rows from ``platform.data_quality_log`` for the
   engine's source (``backtest_credibility.<engine>`` for E7,
   ``engine_lifecycle.<engine>`` for E11).
2. If we have AT LEAST N rows AND **every** row is below the degraded
   threshold, emit the row's distinct event + an ``ENGINE_HELD`` row
   with ``failure_class="behavioral_credibility"`` (E7) or
   ``"behavioral_lifecycle"`` (E11). The supervisor's
   :func:`tpcore.supervisor_state.current_hold` then returns a HoldState
   and the existing :func:`tpcore.engine_profile.should_fire` gate
   blocks dispatch until the operator clears it via the standard
   ``ENGINE_CLEARED`` event.
3. If an open hold already exists for the engine (any failure class),
   skip — the spec's "one-hold rule" matches DA-2's behavioral pattern.

Per spec §4 answer #5:

* **E7 credibility N=3** — credibility moves fast; 3 consecutive
  cycles below ``MIN_LIVE_SCORE=60`` is a clear-signal floor. N=1
  risks pausing on a single noisy Lab run; N=5 is too slow for a
  "the engine is bleeding" signal.
* **E11 lifecycle N=5** — lifecycle is a *trend* metric (engine
  slow-decay over multiple cycles, not a single-cycle shock). 5
  cycles ≈ a trading week. Operator-tunable later.

Programmatic pause + clear event: per the task brief, the pause must
NOT require operator action to fire but must emit a clear ENGINE_HELD
that the operator can see. The two helpers below do exactly that —
auto-emit on detection, operator clears via the canonical
``ENGINE_CLEARED`` path (same as DA-1 infra holds + DA-2 behavioral
holds).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import TYPE_CHECKING

import structlog

from tpcore.backtest.credibility import (
    CREDIBILITY_SOURCE_PREFIX,
    MIN_LIVE_SCORE,
)
from tpcore.supervisor_state import (
    HELD_EVENT,
    SCHEMA_VERSION,
    current_hold,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Public event names emitted to ``platform.application_log`` BEFORE the
# ENGINE_HELD row. Distinct from the supervisor's HELD vocabulary so
# the operator can attribute the pause to the underlying detection
# signal at a glance.
ENGINE_CREDIBILITY_DROP_EVENT: str = "ENGINE_CREDIBILITY_DROP"
ENGINE_LIFECYCLE_DEGRADED_EVENT: str = "ENGINE_LIFECYCLE_DEGRADED"


# Per spec §4 answer #5 — credibility (E7) N=3; lifecycle (E11) N=5.
# Env-overridable for operator tuning under load (matches the
# ENGINE_AUTOTUNE_LOSS_CLUSTER_HOLD_LEN pattern in ops.aar_autotune).
_CREDIBILITY_DROP_THRESHOLD: int = int(
    os.environ.get("ENGINE_CREDIBILITY_DROP_THRESHOLD", "3")
)
_LIFECYCLE_DEGRADED_THRESHOLD: int = int(
    os.environ.get("ENGINE_LIFECYCLE_DEGRADED_THRESHOLD", "5")
)


# Lifecycle health is encoded into ``data_quality_log.confidence`` as a
# 0..1 float (mirrors the credibility shape — score/100). The
# degraded floor is the same MIN_LIVE_SCORE/100 default (0.60) so a
# single source-of-truth threshold gates both rows. Env-tunable for
# the engine roster's first month of Wave-4 in case the operator
# wants a different floor.
_LIFECYCLE_DEGRADED_FLOOR_PCT: float = float(
    os.environ.get(
        "ENGINE_LIFECYCLE_DEGRADED_FLOOR_PCT", str(MIN_LIVE_SCORE / 100),
    )
)


# Source prefix for the engine-lifecycle health stream. The shape
# parallels ``CREDIBILITY_SOURCE_PREFIX`` so the rolling-window read
# query is a parameterised single-source SELECT.
ENGINE_LIFECYCLE_SOURCE_PREFIX: str = "engine_lifecycle"


_BEHAVIORAL_CREDIBILITY: str = "behavioral_credibility"
_BEHAVIORAL_LIFECYCLE: str = "behavioral_lifecycle"


_INSERT_APP_LOG_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


_SELECT_RECENT_DQ_SQL = """
    SELECT confidence, timestamp
    FROM platform.data_quality_log
    WHERE source = $1
    ORDER BY timestamp DESC
    LIMIT $2
"""


async def _emit_application_log(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    event_type: str,
    severity: str,
    message: str,
    payload: dict,
) -> None:
    """Crash-isolated application_log emit (mirrors engine_supervisor._emit)."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_APP_LOG_SQL,
                engine,
                uuid.uuid4(),
                event_type,
                severity,
                message,
                json.dumps(payload, default=str),
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning(
            "tpcore.risk.lifecycle_pause.emit_failed",
            engine=engine,
            event_type=event_type,
            error=str(exc),
        )


async def _read_recent_confidences(
    pool: asyncpg.Pool, *, source: str, limit: int,
) -> list[float]:
    """Return up to ``limit`` most-recent ``confidence`` values for
    ``source`` from ``platform.data_quality_log``, newest first.

    Confidence is a 0..1 Decimal in the schema; we cast to ``float`` at
    the boundary so the caller compares against the float threshold
    directly. An empty list ⇒ no data yet; the caller treats that as
    "skip — first-run seed".
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_RECENT_DQ_SQL, source, limit)
    return [float(r["confidence"]) for r in rows]


def _all_degraded(values: list[float], *, threshold_pct: float, required_n: int) -> bool:
    """True iff we have ``required_n`` values AND every one is below
    ``threshold_pct`` (a 0..1 float).

    The threshold is strict (``<`` not ``<=``) so a score that exactly
    touches the floor is NOT counted as degraded — matches the
    ``CredibilityScore.passes_gate`` ``>=`` semantics in
    ``tpcore.backtest.credibility``.
    """
    if len(values) < required_n:
        return False
    return all(v < threshold_pct for v in values[:required_n])


async def _emit_pause(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    detection_event: str,
    failure_class: str,
    reason: str,
    detection_payload: dict,
) -> str:
    """Emit the detection event + the canonical ENGINE_HELD row.

    Returns the ``hold_id`` so the caller can log it. Both events
    share the same ``hold_id`` so the operator's audit query can
    correlate them.
    """
    hold_id = str(uuid.uuid4())
    # 1. Detection event (auditable signal — what triggered the pause).
    await _emit_application_log(
        pool,
        engine=engine,
        event_type=detection_event,
        severity="ERROR",
        message=f"{engine} {detection_event}: {reason}",
        payload={
            "schema": SCHEMA_VERSION,
            "engine": engine,
            "hold_id": hold_id,
            "failure_class": failure_class,
            "reason": reason,
            **detection_payload,
        },
    )
    # 2. ENGINE_HELD row (the supervisor's vocabulary; should_fire reads it).
    await _emit_application_log(
        pool,
        engine=engine,
        event_type=HELD_EVENT,
        severity="ERROR",
        message=f"{engine} held: {failure_class} — {reason}",
        payload={
            "schema": SCHEMA_VERSION,
            "engine": engine,
            "hold_id": hold_id,
            "failure_class": failure_class,
            "reason": reason,
        },
    )
    logger.error(
        "tpcore.risk.lifecycle_pause.paused",
        engine=engine,
        hold_id=hold_id,
        failure_class=failure_class,
        reason=reason,
    )
    return hold_id


async def check_credibility_drop(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    threshold: int | None = None,
) -> bool:
    """E7 — auto-pause on N=3 consecutive sub-floor credibility scores.

    Reads the most-recent ``threshold`` rows of
    ``platform.data_quality_log`` for source
    ``f"{CREDIBILITY_SOURCE_PREFIX}.{engine}"``. If every row's
    ``confidence < MIN_LIVE_SCORE/100``, emits
    ``ENGINE_CREDIBILITY_DROP`` + ``ENGINE_HELD`` (failure_class
    ``behavioral_credibility``).

    The "one-hold rule" is observed: if any uncleared hold already
    exists for this engine (regardless of failure_class), the check
    is a no-op.

    Returns ``True`` iff a fresh pause was emitted this call. Pool of
    ``None`` returns ``False`` (test stub).
    """
    if pool is None:
        return False
    required_n = threshold if threshold is not None else _CREDIBILITY_DROP_THRESHOLD
    if required_n <= 0:
        return False

    # One-hold rule — don't re-emit on already-held engine.
    hold = await current_hold(pool, engine)
    if hold is not None:
        return False

    source = f"{CREDIBILITY_SOURCE_PREFIX}.{engine}"
    confidences = await _read_recent_confidences(
        pool, source=source, limit=required_n,
    )
    threshold_pct = MIN_LIVE_SCORE / 100
    if not _all_degraded(
        confidences, threshold_pct=threshold_pct, required_n=required_n,
    ):
        return False
    reason = (
        f"{required_n} consecutive credibility scores < "
        f"{MIN_LIVE_SCORE}/100 (latest={confidences[0]:.3f})"
    )
    await _emit_pause(
        pool,
        engine=engine,
        detection_event=ENGINE_CREDIBILITY_DROP_EVENT,
        failure_class=_BEHAVIORAL_CREDIBILITY,
        reason=reason,
        detection_payload={
            "threshold_consecutive_cycles": required_n,
            "floor_score": MIN_LIVE_SCORE,
            "recent_confidences": [round(c, 4) for c in confidences],
            "source": source,
        },
    )
    return True


async def check_lifecycle_degraded(
    pool: asyncpg.Pool | None,
    *,
    engine: str,
    threshold: int | None = None,
    floor_pct: float | None = None,
) -> bool:
    """E11 — auto-pause on N=5 consecutive degraded lifecycle scores.

    Reads the most-recent ``threshold`` rows of
    ``platform.data_quality_log`` for source
    ``f"{ENGINE_LIFECYCLE_SOURCE_PREFIX}.{engine}"``. If every row's
    ``confidence < floor_pct``, emits ``ENGINE_LIFECYCLE_DEGRADED`` +
    ``ENGINE_HELD`` (failure_class ``behavioral_lifecycle``).

    One-hold rule observed. Returns ``True`` iff a fresh pause was
    emitted.

    Lifecycle health writes are emitted by the per-engine
    ``lifecycle_analysis`` plug at end-of-cycle (the plug-side wire-in
    is OPT-IN per engine — Wave-4 ships the detection harness; the
    plug-side write is a future PR per the same staged-wire-in
    convention E2 used for the setup_detection transient_retry pilot).
    """
    if pool is None:
        return False
    required_n = threshold if threshold is not None else _LIFECYCLE_DEGRADED_THRESHOLD
    if required_n <= 0:
        return False
    threshold_pct = floor_pct if floor_pct is not None else _LIFECYCLE_DEGRADED_FLOOR_PCT

    hold = await current_hold(pool, engine)
    if hold is not None:
        return False

    source = f"{ENGINE_LIFECYCLE_SOURCE_PREFIX}.{engine}"
    confidences = await _read_recent_confidences(
        pool, source=source, limit=required_n,
    )
    if not _all_degraded(
        confidences, threshold_pct=threshold_pct, required_n=required_n,
    ):
        return False
    reason = (
        f"{required_n} consecutive lifecycle scores < "
        f"{threshold_pct:.3f} (latest={confidences[0]:.3f})"
    )
    await _emit_pause(
        pool,
        engine=engine,
        detection_event=ENGINE_LIFECYCLE_DEGRADED_EVENT,
        failure_class=_BEHAVIORAL_LIFECYCLE,
        reason=reason,
        detection_payload={
            "threshold_consecutive_cycles": required_n,
            "floor_pct": threshold_pct,
            "recent_confidences": [round(c, 4) for c in confidences],
            "source": source,
        },
    )
    return True


__all__ = [
    "ENGINE_CREDIBILITY_DROP_EVENT",
    "ENGINE_LIFECYCLE_DEGRADED_EVENT",
    "ENGINE_LIFECYCLE_SOURCE_PREFIX",
    "check_credibility_drop",
    "check_lifecycle_degraded",
]
