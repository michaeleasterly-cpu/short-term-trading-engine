"""Engine-lane novelty predicate — which open escalations are
genuinely novel + actionable (deterministic; no LLM).

Spec §7 (premise FIXED): the genuinely-novel engine input is a new
escalation *instance/pattern* the deterministic policy could not
auto-dispose and that has aged past grace — NOT an unknown
`failure_class` (structurally impossible: every emitted class is in
DISPOSITION_POLICIES or `escalation_drift()` fails the build).

`select_novel_escalations` therefore **calls
`ops.engine_ladder.list_undispositioned()` directly** (it already
encodes the open / grace / escalate-only-fingerprint semantics) and
filters its result. It does NOT reimplement the open-set (the bug
`feedback_symmetry_not_copy` forbids) and does NOT test
`policy_for() is None` (proven dead — spec §7/§11). The Ladder policy
default+rationale are already on each `list_undispositioned()` row and
are carried through as ADVISORY packet context — never a gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ops import engine_ladder
from tpcore.llm_data_triage.select import MAX_TRIAGE_PER_CYCLE

# One terminal, exactly one triage attempt per escalation, ever.
_PRIOR_SQL = """
SELECT data->>'hold_id' AS hold_id FROM platform.application_log
WHERE event_type='ENGINE_LLM_TRIAGE_PROPOSAL'
"""


@dataclass(frozen=True)
class EngineNovelEscalation:
    hold_id: str
    engine: str
    failure_class: str
    reason: str
    recorded_at: datetime
    shape: str
    # Advisory only (the Ladder's recommended disposition + why) —
    # carried for the packet, NEVER a selection gate.
    policy_default: str | None
    policy_rationale: str | None


async def select_novel_escalations(
    pool: Any) -> list[EngineNovelEscalation]:
    """= engine_ladder.list_undispositioned(pool) → drop any hold_id
    with a prior ENGINE_LLM_TRIAGE_PROPOSAL → oldest-first cap at
    MAX_TRIAGE_PER_CYCLE (reused from #187).

    list_undispositioned() already returns oldest-first (its
    `_CANDIDATE_SQL` ORDER BY e.recorded_at) and already applies the
    open / grace / escalate-only-fingerprint-resolution semantics — we
    re-derive none of it.
    """
    rows = await engine_ladder.list_undispositioned(pool)
    async with pool.acquire() as conn:
        prior = {r["hold_id"] for r in await conn.fetch(_PRIOR_SQL)}
    out: list[EngineNovelEscalation] = []
    for r in rows:
        if r["hold_id"] in prior:
            continue
        out.append(EngineNovelEscalation(
            hold_id=r["hold_id"],
            engine=r["engine"],
            failure_class=r["failure_class"],
            reason=r["reason"] or "",
            recorded_at=r["recorded_at"],
            shape=r["shape"],
            policy_default=r["policy_default"],
            policy_rationale=r["policy_rationale"],
        ))
        if len(out) >= MAX_TRIAGE_PER_CYCLE:
            break
    return out


__all__ = ["MAX_TRIAGE_PER_CYCLE", "EngineNovelEscalation",
           "select_novel_escalations"]
