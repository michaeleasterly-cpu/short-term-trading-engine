"""Pure trigger predicate — which open escalations are genuinely
novel (deterministic; no LLM). Reuses the Ladder SoT + the
weekly-digest open set; reimplements no predicate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tpcore.ladder import Disposition, policy_for

MAX_TRIAGE_PER_CYCLE = 5

# The real application_log payloads do NOT carry a 'cls' field.
# The weekly-digest derives the Ladder class as f"event:{etype}" — we
# reuse that derivation in _is_novel_class below.  The SQL mirrors the
# digest OPEN_ESCALATIONS query exactly (same open-set predicate), minus
# the $1 cutoff (triage wants the full open set, not last-7-days).
_OPEN_REFS_SQL = """-- OPEN_ESCALATIONS (triage view: ref+etype)
WITH esc AS (
  SELECT e.data->>'request_id' AS ref, 'DATA_REPAIR_ESCALATED' AS etype,
         e.recorded_at, e.message
  FROM platform.application_log e
  WHERE e.event_type = 'DATA_REPAIR_ESCALATED'
  UNION ALL
  SELECT e.data->>'hold_id' AS ref, 'DATA_SOURCE_ESCALATED' AS etype,
         e.recorded_at, e.message
  FROM platform.application_log e
  WHERE e.event_type = 'DATA_SOURCE_ESCALATED'
)
SELECT ref, etype, recorded_at, message FROM esc x
WHERE x.ref IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM platform.application_log t
    WHERE t.event_type IN ('DATA_REPAIR_COMPLETE','DATA_SOURCE_CLEARED')
      AND (t.data->>'request_id'=x.ref OR t.data->>'hold_id'=x.ref)
      AND t.recorded_at > x.recorded_at)
  AND NOT EXISTS (
    SELECT 1 FROM platform.application_log dp
    WHERE dp.event_type='DATA_ESCALATION_DISPOSITIONED'
      AND dp.data->>'ref'=x.ref)
ORDER BY x.recorded_at
"""

_PRIOR_SQL = """
SELECT data->>'ref' AS ref FROM platform.application_log
WHERE event_type='DATA_LLM_TRIAGE_PROPOSAL'
"""


@dataclass(frozen=True)
class NovelEscalation:
    ref: str
    etype: str
    cls: str
    recorded_at: datetime
    message: str


def _is_novel_class(cls: str | None) -> bool:
    """Genuinely novel = the Ladder has no deterministic auto-conversion
    (policy escalate-operator). Unknown/None => novel (an unknown
    escalation must never be silently skipped).

    The Ladder class for a real escalation event is derived as
    ``f"event:{etype}"`` — exactly the same derivation used by the
    weekly-digest ``_disposition_label``.  Test callers may monkeypatch
    this function; real callers supply the derived class."""
    if not cls:
        return True
    try:
        return policy_for(cls).disposition is Disposition.ESCALATE_OPERATOR
    except KeyError:
        return True


async def select_novel_escalations(pool: Any) -> list[NovelEscalation]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_OPEN_REFS_SQL)
        prior = {r["ref"] for r in await conn.fetch(_PRIOR_SQL)}
    out: list[NovelEscalation] = []
    for r in rows:
        if r["ref"] in prior:
            continue
        # Derive Ladder class: real payloads have no 'cls' column so we
        # use the same f"event:{etype}" derivation as the weekly-digest.
        # Fake test rows may supply 'cls' directly — honour that first so
        # monkeypatched _is_novel_class receives the expected value.
        derived_cls: str = r.get("cls") or f"event:{r['etype']}"
        if not _is_novel_class(derived_cls):
            continue
        out.append(NovelEscalation(
            ref=r["ref"],
            etype=r["etype"],
            cls=derived_cls,
            recorded_at=r["recorded_at"],
            message=r.get("message") or "",
        ))
        if len(out) >= MAX_TRIAGE_PER_CYCLE:
            break
    return out


__all__ = ["MAX_TRIAGE_PER_CYCLE", "NovelEscalation",
           "select_novel_escalations"]
