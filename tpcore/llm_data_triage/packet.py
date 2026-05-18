"""Read-only context packet for LLM triage — deterministic, NO writes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tpcore.ladder import policy_for
from tpcore.llm_data_triage.select import NovelEscalation

_MAX_TEXT = 24_000
_TRUNCATE_MARKER = "\n...[truncated]..."

# Mirror the column shape from tpcore/selfheal/orchestrator._RED_SQL and
# tpcore/quality/data_quality.py: source, timestamp, confidence, stale, notes.
_DQL_SQL = """
SELECT source, timestamp, confidence, stale, notes
FROM platform.data_quality_log
WHERE source ILIKE $1
ORDER BY timestamp DESC
LIMIT 10
"""


@dataclass(frozen=True)
class TriagePacket:
    text: str
    packet_hash: str


def _ladder_info(cls: str) -> dict[str, str]:
    try:
        p = policy_for(cls)
        return {"disposition": p.disposition.value,
                "reason": p.reason or ""}
    except KeyError:
        return {"disposition": "unknown", "reason": "unknown"}


def _dql_source_pattern(cls: str) -> str:
    """Derive a ILIKE pattern from the Ladder class for data_quality_log.

    Examples:
      event:DATA_SOURCE_ESCALATED  -> '%'  (generic event — no feed hint)
      selfheal:prices_daily_freshness -> '%prices_daily_freshness%'
    """
    parts = cls.split(":", 1)
    if len(parts) == 2 and parts[0] not in ("event",):
        return f"%{parts[1]}%"
    return "%"


async def build_packet(pool: Any, esc: NovelEscalation) -> TriagePacket:
    """Assemble a read-only context dict, serialise to JSON, hash it.
    Pure read — NO writes, NO LLM calls."""
    pattern = _dql_source_pattern(esc.cls)
    async with pool.acquire() as conn:
        dql_rows = await conn.fetch(_DQL_SQL, pattern)

    ladder = _ladder_info(esc.cls)

    payload: dict[str, Any] = {
        "escalation": {
            "ref": esc.ref,
            "etype": esc.etype,
            "cls": esc.cls,
            "recorded_at": str(esc.recorded_at),
            "message": esc.message,
        },
        "ladder_policy": ladder,
        "data_quality_context": [dict(r) for r in dql_rows],
    }

    text = json.dumps(payload, sort_keys=True, default=str)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + _TRUNCATE_MARKER

    packet_hash = hashlib.sha256(text.encode()).hexdigest()
    return TriagePacket(text=text, packet_hash=packet_hash)


__all__ = ["TriagePacket", "build_packet"]
