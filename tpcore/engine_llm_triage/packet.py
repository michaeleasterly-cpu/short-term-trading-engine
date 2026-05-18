"""Read-only engine-lane context packet for LLM triage — deterministic,
NO writes, NO LLM calls (mirrors the #187 packet shape; engine inputs).

Assembles, for ONE novel escalation:
  - the ENGINE_ESCALATED escalation (carried on EngineNovelEscalation),
  - `tpcore.supervisor_state.current_hold(pool, engine)` (read-only),
  - open `platform.forensics_triggers` for the engine (resolved_at
    NULL; the same shape DA-2 reads),
  - the engine profile (`tpcore.engine_profile.profile_for`),
  - the ADVISORY `engine_ladder.policy_for(failure_class)` default +
    rationale (recommended disposition + why — context, never a gate).

Serialised deterministically (sorted keys), size-capped with the
#187 truncation marker, sha256-hashed (same input → same hash).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tpcore.engine_llm_triage.select import EngineNovelEscalation
from tpcore.engine_profile import profile_for
from tpcore.supervisor_state import current_hold


def _engine_ladder():
    """Lazy accessor for the read-only `ops.engine_ladder.policy_for`
    predicate. Imported at CALL time (never at module-load / pytest
    collection time) so importing this pure module never binds
    `sys.modules['ops']` — that would collide with `test_ops.py`'s
    `scripts/ops.py`-on-sys.path shim (the documented
    `scripts/ops.py`↔`ops/` shadowing). Read predicate only — NOT an
    actor path.
    """
    from ops import engine_ladder

    return engine_ladder

# Parity with tpcore/llm_data_triage/packet.py (#187): same cap + marker.
_MAX_TEXT = 24_000
_TRUNCATE_MARKER = "\n...[truncated]..."

# Open forensics triggers for the engine — mirrors
# ops.aar_autotune._open_triggers exactly (resolved_at NULL, by engine,
# newest first). Read-only; this module NEVER writes forensics_triggers.
_FORENSICS_SQL = """
SELECT id, trigger_kind, payload
FROM platform.forensics_triggers
WHERE resolved_at IS NULL
  AND payload->>'engine' = $1
ORDER BY fired_at DESC
LIMIT 25
"""


@dataclass(frozen=True)
class EngineTriagePacket:
    text: str
    packet_hash: str


def _advisory_policy(failure_class: str) -> dict[str, str | None]:
    """The Ladder's RECOMMENDED disposition for the class — advisory
    context for the LLM, NEVER a gate. `policy_for` is read-only."""
    pol = _engine_ladder().policy_for(failure_class)
    if pol is None:
        return {"default": None, "rationale": None}
    return {"default": pol.default.value, "rationale": pol.rationale}


def _profile_dict(engine: str) -> dict[str, Any] | None:
    p = profile_for(engine)
    if p is None:
        return None
    return {
        "engine": p.engine,
        "cadence": str(p.cadence),
        "dispatch_order": p.dispatch_order,
        "lifecycle_state": str(p.lifecycle_state),
        "allocator_eligible": p.allocator_eligible,
    }


async def build_packet(
    pool: Any, esc: EngineNovelEscalation) -> EngineTriagePacket:
    """Assemble a read-only context dict, serialise to JSON, hash it.
    Pure read — NO writes, NO LLM."""
    hold = await current_hold(pool, esc.engine)
    async with pool.acquire() as conn:
        forensics_rows = await conn.fetch(_FORENSICS_SQL, esc.engine)

    forensics: list[dict[str, Any]] = []
    for r in forensics_rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        forensics.append({
            "id": r["id"],
            "trigger_kind": r["trigger_kind"],
            "payload": payload,
        })

    payload: dict[str, Any] = {
        "escalation": {
            "hold_id": esc.hold_id,
            "engine": esc.engine,
            "failure_class": esc.failure_class,
            "reason": esc.reason,
            "recorded_at": str(esc.recorded_at),
            "shape": esc.shape,
        },
        "current_hold": (
            None if hold is None else {
                "hold_id": hold.hold_id,
                "failure_class": hold.failure_class,
                "reason": hold.reason,
                "held_at": str(hold.held_at),
            }
        ),
        "open_forensics_triggers": forensics,
        "engine_profile": _profile_dict(esc.engine),
        "advisory_ladder_policy": _advisory_policy(esc.failure_class),
    }

    text = json.dumps(payload, sort_keys=True, default=str)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + _TRUNCATE_MARKER

    packet_hash = hashlib.sha256(text.encode()).hexdigest()
    return EngineTriagePacket(text=text, packet_hash=packet_hash)


__all__ = ["EngineTriagePacket", "build_packet"]
