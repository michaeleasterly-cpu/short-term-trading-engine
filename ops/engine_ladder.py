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

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from ops.aar_autotune import _BEHAVIORAL
from ops.engine_supervisor import (
    INFRA_FAILURE_CLASSES,
    PLATFORM_SERVICE_FAILURE_CLASSES,
)
from tpcore.db import build_asyncpg_pool
from tpcore.supervisor_state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
)

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
    "engine_service_task_crashloop": DispositionPolicy(
        class_name="engine_service_task_crashloop", default=_D.STRUCTURAL,
        rationale="a co-hosted engine-daemon task (sweep/monitor) "
                  "crash-looped past the 3-in-600s budget; the 5s-backoff "
                  "restart is advisory — persistence ⇒ a structural fix "
                  "to that co-task's runtime, NOT a per-engine infra heal."),
    "engine_service_digest_failed": DispositionPolicy(
        class_name="engine_service_digest_failed", default=_D.STRUCTURAL,
        rationale="the day-rollover weekly_digest subprocess failed "
                  "(spawn error or non-zero rc) and was swallowed; "
                  "the digest is the state-comprehension floor — a "
                  "structural fix to the digest path, not an engine heal."),
}

KNOWN_ESCALATION_CLASSES: frozenset[str] = (
    INFRA_FAILURE_CLASSES | PLATFORM_SERVICE_FAILURE_CLASSES
    | {_BEHAVIORAL})


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


# Genuinely new, engine-ladder-local (DA-1/DA-2 never emit it).
_DISPOSITIONED_EVENT = "ENGINE_ESCALATION_DISPOSITIONED"

_GRACE_DAYS = int(os.environ.get("ENGINE_LADDER_GRACE_DAYS", "7"))

# Candidate ENGINE_ESCALATED rows: not later-CLEARED, not DISPOSITIONED,
# with a has_held flag (paired ENGINE_HELD on the SAME hold_id) so the
# caller distinguishes held vs escalate-only. Escalate-only auto-close
# (trigger fingerprints resolved) is applied in Python against
# forensics_triggers (mirrors aar_autotune behavioral re-eval).
# Event-type literals interpolated from the tpcore.supervisor_state
# constants DA-1/DA-2 emit (trusted compile-time module constants, NOT
# user input — safe to f-string into the SQL; same shape DA-1 uses).
_CANDIDATE_SQL = f"""
    SELECT e.data->>'hold_id'        AS hold_id,
           e.engine                  AS engine,
           e.data->>'failure_class'  AS failure_class,
           e.data->>'reason'         AS reason,
           e.recorded_at             AS recorded_at,
           (e.data->'triggers')      AS triggers,
           EXISTS (SELECT 1 FROM platform.application_log h
                   WHERE h.event_type = '{HELD_EVENT}'
                     AND (h.data->>'hold_id') = (e.data->>'hold_id'))
                                     AS has_held
    FROM platform.application_log e
    WHERE e.event_type = '{ESCALATED_EVENT}'
      AND (e.data->>'hold_id') IS NOT NULL
      AND e.recorded_at < $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = '{_DISPOSITIONED_EVENT}'
          AND (d.data->>'hold_id') = (e.data->>'hold_id'))
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = '{CLEARED_EVENT}'
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


async def _escalate_only_still_open(pool, fps: list[str]) -> bool:
    """Single source of truth for the escalate-only auto-close rule
    (shared by list_undispositioned AND disposition so they can never
    diverge again). An escalate-only escalation is OPEN iff:
      - it recorded NO trigger fingerprints (cannot auto-close → open), OR
      - at least one of its fingerprints is still unresolved in
        platform.forensics_triggers.
    If it recorded fingerprints and ALL are resolved → auto-closed
    (NOT open)."""
    if not fps:
        return True  # no fps recorded → cannot auto-close; remains open
    async with pool.acquire() as conn:
        fp_rows = await conn.fetch(_OPEN_FP_SQL, list(set(fps)))
    open_fp_set = {fr["fp"] for fr in fp_rows}
    return bool(set(fps) & open_fp_set)


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
    out: list[dict] = []
    for r in rows:
        if bool(r["has_held"]):
            shape = EscalationShape.HELD
        else:
            shape = EscalationShape.ESCALATE_ONLY
            fps = _triggers_list(r["triggers"])
            if not await _escalate_only_still_open(pool, fps):
                continue  # all fps resolved → auto-closed
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

# Mirrors _CANDIDATE_SQL's anti-joins + has_held/triggers projection
# (single hold_id; no grace filter — disposition has NO grace by
# design). The escalate-only fingerprint-resolution gate is applied in
# Python via the SHARED _escalate_only_still_open helper so list and
# disposition can never diverge on what "open" means.
_IS_OPEN_SQL = f"""
    SELECT e.engine            AS engine,
           (e.data->'triggers') AS triggers,
           EXISTS (SELECT 1 FROM platform.application_log h
                   WHERE h.event_type = '{HELD_EVENT}'
                     AND (h.data->>'hold_id') = $1)
                               AS has_held
    FROM platform.application_log e
    WHERE e.event_type = '{ESCALATED_EVENT}'
      AND (e.data->>'hold_id') = $1
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log d
        WHERE d.event_type = '{_DISPOSITIONED_EVENT}'
          AND (d.data->>'hold_id') = $1)
      AND NOT EXISTS (
        SELECT 1 FROM platform.application_log c
        WHERE c.event_type = '{CLEARED_EVENT}'
          AND (c.data->>'hold_id') = $1
          AND c.recorded_at > e.recorded_at)
    LIMIT 1
"""


async def _emit(pool, engine: str, event_type: str, severity: str,
                message: str, payload: dict) -> None:
    """One application_log row via the locked INSERT (column-order
    parity with engine_supervisor._emit / weekly_digest._emit)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, engine, uuid.uuid4(), event_type, severity,
            message, json.dumps(payload, default=str))


async def disposition(pool, hold_id: str, verb: str, note: str) -> int:
    """Record an operator disposition for an open engine escalation.
    Accepts BOTH held and escalate-only hold_ids (validity is the
    open-escalation predicate, NOT current_hold). Returns 0 on success,
    1 on a bad verb, 2 on an unknown/not-open hold_id."""
    try:
        disp = EngineEscalationDisposition(verb.strip().lower())
    except ValueError:
        logger.error("engine_ladder.bad_verb", verb=verb)
        return 1
    async with pool.acquire() as conn:
        rows = await conn.fetch(_IS_OPEN_SQL, hold_id)
    if not rows:
        logger.error("engine_ladder.unknown_or_not_open", hold_id=hold_id)
        return 2
    first = rows[0]
    engine = first["engine"]
    if not engine:
        logger.error("engine_ladder.escalation_missing_engine", hold_id=hold_id)
        return 2
    # Escalate-only parity: list_undispositioned auto-closes an
    # escalate-only escalation once all its trigger fingerprints are
    # resolved. disposition MUST apply the SAME gate (via the shared
    # helper) so a hold_id `list` hides can never be dispositioned.
    # Held escalations are NOT subject to this (held shape proceeds).
    # The grace asymmetry is deliberate: disposition has NO grace.
    if not bool(first["has_held"]):
        fps = _triggers_list(first["triggers"])
        if not await _escalate_only_still_open(pool, fps):
            logger.error("engine_ladder.escalate_only_already_resolved",
                          hold_id=hold_id)
            return 2
    payload = {"schema": 1, "hold_id": hold_id,
               "disposition": disp.value, "note": note}
    await _emit(pool, engine, _DISPOSITIONED_EVENT, "INFO",
                f"escalation {hold_id} dispositioned: {disp.value}", payload)
    logger.info("engine_ladder.dispositioned", hold_id=hold_id,
                disposition=disp.value)
    return 0


# Epic E Phase 3.2: surface the engine LLM-triage advisory proposal
# (ENGINE_LLM_TRIAGE_PROPOSAL — emitted by ops.engine_llm_triage) on
# the EXISTING undispositioned digest line. Engine-native mirror of
# the data-lane weekly_digest._llm_suffix (symmetry-of-approach, not a
# clone — the data-lane file is untouched). Advisory ONLY: the human
# still dispositions via the deterministic Ladder path.
_LLM_PROPOSAL_SQL = """
    SELECT data->>'hold_id'              AS hold_id,
           data->>'proposed_disposition' AS proposed_disposition,
           data->>'confidence'           AS confidence,
           data->>'pr_link'              AS pr_link
    FROM platform.application_log
    WHERE event_type = 'ENGINE_LLM_TRIAGE_PROPOSAL'
      AND (data->>'hold_id') = ANY($1::text[])
"""


async def _attach_llm_proposals(pool, rows: list[dict]) -> None:
    """Annotate the GIVEN open-set rows IN PLACE with their advisory
    ENGINE_LLM_TRIAGE_PROPOSAL (if any). DRY: it consumes the open set
    `list_undispositioned` already computed — it does NOT re-query /
    re-derive the engine overdue set; it only fetches proposals for the
    exact hold_ids already in ``rows``. Only the latest proposal per
    hold_id is kept (a re-triage supersedes)."""
    if not rows:
        return
    hold_ids = [r["hold_id"] for r in rows]
    async with pool.acquire() as conn:
        prop_rows = await conn.fetch(_LLM_PROPOSAL_SQL, hold_ids)
    by_hold: dict[str, dict] = {}
    for p in prop_rows:
        hid = p["hold_id"]
        if hid is not None:
            by_hold[hid] = {
                "proposed_disposition": p["proposed_disposition"],
                "confidence": p["confidence"],
                "pr_link": p["pr_link"],
            }
    for r in rows:
        r["llm_proposal"] = by_hold.get(r["hold_id"])


def _llm_suffix(proposal: dict | None) -> str:
    """The advisory LLM annotation appended to an undispositioned line
    IFF a proposal exists for that hold_id (else ``""`` — exact parity
    with weekly_digest._llm_suffix)."""
    if not proposal:
        return ""
    link = proposal.get("pr_link") or "(no PR)"
    return (
        f" | LLM: {proposal.get('proposed_disposition')} "
        f"(conf {proposal.get('confidence')}) — PR {link}"
    )


def _fmt(rows: list[dict]) -> str:
    head = (f"UNDISPOSITIONED ENGINE-LANE ESCALATIONS ({len(rows)}) — "
            "rung-3: each MUST be converted | structural | removed")
    if not rows:
        return head
    lines = [head]
    for r in rows:
        pol_default = r["policy_default"]
        if pol_default is None:
            pol_str = "UNKNOWN (no policy registered — drift; add to DISPOSITION_POLICIES)"
        else:
            pol_str = f"{pol_default} ({r['policy_rationale']})"
        lines.append(
            f"  [{r['shape']}] {r['engine']}/{r['failure_class']} "
            f"hold_id={r['hold_id']} since={r['recorded_at']} "
            f"reason={r['reason']} -> policy={pol_str}"
            f"{_llm_suffix(r.get('llm_proposal'))}")
    return "\n".join(lines)


async def _amain(argv: list[str]) -> int:
    dsn = (os.environ.get("DATABASE_URL")
           or os.environ.get("DATABASE_URL_IPV4"))
    if not dsn:
        logger.error("engine_ladder.no_dsn")
        return 1
    p = argparse.ArgumentParser(prog="python -m ops.engine_ladder")
    sub = p.add_subparsers(dest="cmd")
    pl = sub.add_parser("list")
    pl.add_argument("--grace-days", type=int, default=None)
    pd = sub.add_parser("disposition")
    pd.add_argument("hold_id")
    pd.add_argument("verb")
    pd.add_argument("note", nargs="*", default=[])
    args = p.parse_args(argv or ["list"])
    pool = await build_asyncpg_pool(dsn)
    try:
        if args.cmd == "list":
            rows = await list_undispositioned(
                pool, grace_days=args.grace_days)
            # DRY: annotate the SAME open set with the advisory LLM
            # proposal (Epic E Phase 3.2) — no re-derivation.
            await _attach_llm_proposals(pool, rows)
            print(_fmt(rows))
            return 0
        if args.cmd == "disposition":
            return await disposition(pool, args.hold_id, args.verb,
                                     " ".join(args.note))
        p.print_usage(sys.stderr)
        return 2
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
