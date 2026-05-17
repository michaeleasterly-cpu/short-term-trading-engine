"""Engine-Lane Escalation & Hardening Ladder (sub-project after canary).

Closes the silent-best-effort gap: DA-1 (engine_supervisor) and DA-2
(aar_autotune) emit ENGINE_ESCALATED with ZERO consumers. This module
makes every engine escalation CLASS carry a recorded disposition
(clockwork-enforced — a new class fails CI: R2) and (in later tasks)
every undispositioned INSTANCE past grace surface via
`python -m ops.engine_ladder list` with a `disposition` verb (R3).
Engine-native; symmetry-references the data-lane ladder
(tpcore/ladder + weekly_digest) but touches NO data-lane file.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from ops.aar_autotune import _BEHAVIORAL
from ops.engine_supervisor import INFRA_FAILURE_CLASSES

logger = structlog.get_logger(__name__)


class EscalationShape(StrEnum):
    """Whether an escalation paired an ENGINE_HELD (held) or not
    (escalate-only — DA-2 noise; engine kept trading)."""

    HELD = "held"
    ESCALATE_ONLY = "escalate-only"


class EngineEscalationDisposition(StrEnum):
    """Every engine escalation terminates in exactly one of these.
    No AUTO_CONVERTED: the engine lane has no auto-conversion actor."""

    CONVERTED = "converted"
    STRUCTURAL = "structural"
    REMOVED = "removed"


class DispositionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    class_name: str
    default: EngineEscalationDisposition
    rationale: str


_D = EngineEscalationDisposition

DISPOSITION_POLICIES: dict[str, DispositionPolicy] = {
    "crashed_startup": DispositionPolicy(
        class_name="crashed_startup", default=_D.STRUCTURAL,
        rationale="DA-1 bounded re-invoke exhausted; persistence ⇒ a "
                  "structural scheduler/runtime fix."),
    "scheduler_crash": DispositionPolicy(
        class_name="scheduler_crash", default=_D.STRUCTURAL,
        rationale="non-zero scheduler exit survived self-heal ⇒ a "
                  "code/runtime defect to fix structurally."),
    "data_request_timeout": DispositionPolicy(
        class_name="data_request_timeout", default=_D.STRUCTURAL,
        rationale="data lane never answered in-window ⇒ the structural "
                  "fix is typically in the DATA LANE's request "
                  "fulfillment/timeout, NOT this engine; disposition "
                  "records the operator confirmed cross-lane ownership."),
    "data_repair_escalated": DispositionPolicy(
        class_name="data_repair_escalated", default=_D.STRUCTURAL,
        rationale="the DATA-LANE escalation owns the fix; this engine "
                  "is HELD (not removed) and auto-clears on "
                  "DATA_REPAIR_COMPLETE green via DA-1 _auto_clear; "
                  "escalate to REMOVED only if the source is "
                  "permanently retired."),
    "missed_cycle": DispositionPolicy(
        class_name="missed_cycle", default=_D.STRUCTURAL,
        rationale="engine silently failed to start over N cycles ⇒ a "
                  "structural scheduling/dispatch fix."),
    _BEHAVIORAL: DispositionPolicy(
        class_name=_BEHAVIORAL, default=_D.STRUCTURAL,
        rationale="DA-2 loss_cluster≥5 / drawdown ⇒ edge-decay; a "
                  "structural strategy review, or REMOVED if the edge "
                  "is gone (snap-out via the Engine SDLC)."),
}

KNOWN_ESCALATION_CLASSES: frozenset[str] = (
    INFRA_FAILURE_CLASSES | {_BEHAVIORAL})


def _drift_for(*, known: set[str] | frozenset[str],
               policies: dict[str, DispositionPolicy]
               ) -> tuple[set[str], set[str]]:
    have = set(policies)
    known_s = set(known)
    return known_s - have, have - known_s


def escalation_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) of the DERIVED KNOWN set vs DISPOSITION_POLICIES.
    No args (mirrors tpcore.ladder.disposition.disposition_drift). Both
    empty == lockstep. A new DA-1/DA-2 class grows KNOWN (via the pinned
    constants) ⇒ missing non-empty ⇒ the clockwork test fails the build
    until a policy is recorded — the R2 tooth."""
    return _drift_for(known=KNOWN_ESCALATION_CLASSES,
                      policies=DISPOSITION_POLICIES)


def policy_for(class_name: str) -> DispositionPolicy | None:
    """The class's policy, or None if unknown (the data-lane analog
    tpcore.ladder.disposition.policy_for raises KeyError instead)."""
    return DISPOSITION_POLICIES.get(class_name)


_GRACE_DAYS = int(os.environ.get("ENGINE_LADDER_GRACE_DAYS", "7"))

# Candidate ENGINE_ESCALATED rows: not later-CLEARED, not DISPOSITIONED,
# with a has_held flag (paired ENGINE_HELD on the SAME hold_id) so the
# caller distinguishes held vs escalate-only. Escalate-only auto-close
# (trigger fingerprints resolved) is applied in Python against
# forensics_triggers (mirrors aar_autotune behavioral re-eval).
_CANDIDATE_SQL = """
    SELECT e.data->>'hold_id'        AS hold_id,
           e.engine                  AS engine,
           e.data->>'failure_class'  AS failure_class,
           e.data->>'reason'         AS reason,
           e.recorded_at             AS recorded_at,
           (e.data->'triggers')      AS triggers,
           EXISTS (SELECT 1 FROM platform.application_log h
                   WHERE h.event_type = 'ENGINE_HELD'
                     AND (h.data->>'hold_id') = (e.data->>'hold_id'))
                                     AS has_held
    FROM platform.application_log e
    WHERE e.event_type = 'ENGINE_ESCALATED'
      AND (e.data->>'hold_id') IS NOT NULL
      AND e.recorded_at < $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = 'ENGINE_ESCALATION_DISPOSITIONED'
          AND (d.data->>'hold_id') = (e.data->>'hold_id'))
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = 'ENGINE_CLEARED'
          AND (c.data->>'hold_id') = (e.data->>'hold_id')
          AND c.recorded_at > e.recorded_at)
    ORDER BY e.recorded_at
"""

_OPEN_FP_SQL = """
    SELECT payload->>'fingerprint' AS fp
    FROM platform.forensics_triggers
    WHERE resolved_at IS NULL AND payload->>'fingerprint' = ANY($1::text[])
"""


def _triggers_list(raw: Any) -> list[str]:
    # asyncpg returns JSONB as a native list; the str branch guards a double-encoded producer
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


async def list_undispositioned(pool, *, now: datetime | None = None,
                               grace_days: int | None = None) -> list[dict]:
    """Open-undispositioned engine escalations (held + escalate-only).
    Read-only, grace-windowed. Escalate-only rows auto-close once all
    their trigger fingerprints are resolved/absent from
    forensics_triggers (the no-hold terminal disjunct)."""
    now = now or datetime.now(UTC)
    grace = grace_days if grace_days is not None else _GRACE_DAYS
    cutoff = now - timedelta(days=grace)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CANDIDATE_SQL, cutoff)
    all_fps: set[str] = set()
    for r in rows:
        if not bool(r["has_held"]):
            all_fps.update(_triggers_list(r["triggers"]))
    open_fp_set: set[str] = set()
    if all_fps:
        async with pool.acquire() as conn:
            fp_rows = await conn.fetch(_OPEN_FP_SQL, list(all_fps))
        open_fp_set = {fr["fp"] for fr in fp_rows}
    out: list[dict] = []
    for r in rows:
        if bool(r["has_held"]):
            shape = EscalationShape.HELD
        else:
            shape = EscalationShape.ESCALATE_ONLY
            fps = _triggers_list(r["triggers"])
            if fps and not (set(fps) & open_fp_set):
                continue  # all fps resolved → auto-closed
            # no fps recorded → cannot auto-close; remains open
        pol = policy_for(r["failure_class"])
        out.append({
            "hold_id": r["hold_id"], "engine": r["engine"],
            "failure_class": r["failure_class"], "reason": r["reason"],
            "recorded_at": r["recorded_at"], "shape": str(shape),
            "policy_default": (pol.default.value if pol else None),
            "policy_rationale": (pol.rationale if pol else None),
        })
    return out


_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

_DISPOSITIONED_EVENT = "ENGINE_ESCALATION_DISPOSITIONED"

_IS_OPEN_SQL = """
    SELECT e.data->>'hold_id' AS hold_id, e.engine AS engine
    FROM platform.application_log e
    WHERE e.event_type = 'ENGINE_ESCALATED'
      AND (e.data->>'hold_id') = $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = 'ENGINE_ESCALATION_DISPOSITIONED'
          AND (d.data->>'hold_id') = $1)
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = 'ENGINE_CLEARED'
          AND (c.data->>'hold_id') = $1
          AND c.recorded_at > e.recorded_at)
    LIMIT 1
"""


async def disposition(pool, hold_id: str, verb: str, note: str) -> int:
    """Record an operator disposition for an open engine escalation.
    Accepts BOTH held and escalate-only hold_ids (validity is the
    open-escalation predicate, NOT current_hold). 0 ok; non-zero +
    NO write on a bad verb or an unknown/not-open hold_id."""
    try:
        disp = EngineEscalationDisposition(verb.strip().lower())
    except ValueError:
        logger.error("engine_ladder.bad_verb", verb=verb)
        return 2
    async with pool.acquire() as conn:
        rows = await conn.fetch(_IS_OPEN_SQL, hold_id)
    if not rows:
        logger.error("engine_ladder.unknown_or_not_open", hold_id=hold_id)
        return 2
    first = rows[0]
    engine = (first.get("engine") if isinstance(first, dict)
              else first["engine"]) or "engine"
    payload = {"schema": 1, "hold_id": hold_id,
               "disposition": disp.value, "note": note}
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, engine, uuid.uuid4(), _DISPOSITIONED_EVENT,
            "INFO", f"escalation {hold_id} dispositioned: {disp.value}",
            json.dumps(payload, default=str))
    logger.info("engine_ladder.dispositioned", hold_id=hold_id,
                disposition=disp.value)
    return 0
