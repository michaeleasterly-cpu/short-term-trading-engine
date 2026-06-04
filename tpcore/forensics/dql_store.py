"""Forensics-trigger persistence on the consolidated ``data_quality_log``.

Plan 2 (migration ``20260604_0300`` drops ``platform.forensics_triggers``;
``20260604_0500`` folds it into ``platform.data_quality_log`` via the
``kind='forensics_trigger'`` discriminator). This module is the single
read/write surface every forensics producer + reader now shares, so the
``kind`` filter, the jsonb ``notes`` field names, and the "open" predicate can
never drift across the (formerly raw-SQL) call sites.

Row mapping (old ``forensics_triggers`` column → new ``data_quality_log``):

* ``id`` (bigint)         → ``id`` (uuid) — the resolution key.
* ``trigger_kind`` (text) → ``notes->>'trigger_kind'`` (+ duplicated into the
  ``source`` = ``forensics_trigger.<engine>`` key for cheap grouping).
* ``payload`` (jsonb)     → merged into ``notes`` (all payload fields stay at
  the top level: ``engine``, ``fingerprint``, ``dossier_path``, etc.).
* ``fired_at`` (tstz)     → ``timestamp``.
* ``resolved_at`` (tstz)  → ``notes->>'resolved_at'`` (ISO string; NULL = open).

The typed metric columns (``latency_ms``/``missing_bars``/``stale``/
``confidence``) stay NULL — the ``dql_typed_cols_validation_only`` CHECK forbids
them on a non-``validation`` kind.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tpcore.quality.data_quality import KIND_FORENSICS_TRIGGER

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

# Source-key prefix; the engine is appended (``forensics_trigger.<engine>``) so
# the per-engine open-trigger reads can filter on ``source`` cheaply while the
# canonical discriminator stays ``kind='forensics_trigger'``.
SOURCE_PREFIX = "forensics_trigger"


def _source_for(engine: str) -> str:
    return f"{SOURCE_PREFIX}.{engine}"


_EXISTS_SQL = """
    SELECT 1
    FROM platform.data_quality_log
    WHERE kind = $1
      AND notes->>'trigger_kind' = $2
      AND notes->>'fingerprint' = $3
    LIMIT 1
"""

_INSERT_SQL = """
    INSERT INTO platform.data_quality_log (kind, source, timestamp, notes)
    VALUES ($1, $2, $3, $4::jsonb)
    RETURNING id
"""

_SET_DOSSIER_SQL = """
    UPDATE platform.data_quality_log
       SET notes = notes || jsonb_build_object('dossier_path', $1::text)
     WHERE id = $2
"""

_RESOLVE_SQL = """
    UPDATE platform.data_quality_log
       SET notes = jsonb_set(
           notes, '{resolved_at}', to_jsonb(now()::text), true)
     WHERE id = $1
       AND kind = $2
"""


async def fingerprint_exists(
    conn: asyncpg.Connection, *, trigger_kind: str, fingerprint: str
) -> bool:
    """True iff a forensics-trigger row with this ``(trigger_kind, fingerprint)``
    already exists (idempotency check; mirrors the old EXISTS-by-fingerprint)."""
    row = await conn.fetchval(
        _EXISTS_SQL, KIND_FORENSICS_TRIGGER, trigger_kind, fingerprint
    )
    return row is not None


async def insert_trigger(
    conn: asyncpg.Connection,
    *,
    trigger_kind: str,
    engine: str,
    payload: dict[str, Any],
    fired_at: datetime | None = None,
) -> Any:
    """INSERT one forensics-trigger row, returning its uuid id.

    ``payload`` is stored verbatim under ``notes`` with ``trigger_kind`` +
    ``resolved_at`` (NULL = open) added. ``engine`` is also stamped into the
    ``source`` key for per-engine reads. Caller is responsible for the
    fingerprint idempotency check (use :func:`fingerprint_exists`)."""
    fired = fired_at or datetime.now(UTC)
    notes = {**payload, "trigger_kind": trigger_kind, "resolved_at": None}
    notes.setdefault("engine", engine)
    return await conn.fetchval(
        _INSERT_SQL,
        KIND_FORENSICS_TRIGGER,
        _source_for(engine),
        fired,
        json.dumps(notes, default=str),
    )


async def set_dossier_path(
    conn: asyncpg.Connection, *, trigger_id: Any, dossier_path: str
) -> None:
    """Merge ``{'dossier_path': ...}`` into the trigger's notes jsonb."""
    await conn.execute(_SET_DOSSIER_SQL, dossier_path, trigger_id)


async def mark_resolved(conn: asyncpg.Connection, *, trigger_id: Any) -> None:
    """Set ``notes.resolved_at = now()`` (the open→resolved transition)."""
    await conn.execute(_RESOLVE_SQL, trigger_id, KIND_FORENSICS_TRIGGER)


# ── Read SQL fragments (shared by the dashboard / ops / engine-ladder /
#    aar_autotune readers so the kind filter + open predicate stay in lockstep).

# All forensics-trigger rows for one engine that are still OPEN, newest first.
OPEN_FOR_ENGINE_SQL = f"""
    SELECT id,
           notes->>'trigger_kind' AS trigger_kind,
           notes                  AS payload
    FROM platform.data_quality_log
    WHERE kind = '{KIND_FORENSICS_TRIGGER}'
      AND notes->>'resolved_at' IS NULL
      AND notes->>'engine' = $1
    ORDER BY timestamp DESC
"""

# Open trigger fingerprints intersected with a candidate list (engine-ladder).
OPEN_FINGERPRINTS_SQL = f"""
    SELECT notes->>'fingerprint' AS fp
    FROM platform.data_quality_log
    WHERE kind = '{KIND_FORENSICS_TRIGGER}'
      AND notes->>'resolved_at' IS NULL
      AND notes->>'fingerprint' = ANY($1::text[])
"""

# Open-trigger counts grouped by kind, with oldest-open timestamp (dashboard).
OPEN_COUNTS_BY_KIND_SQL = f"""
    SELECT notes->>'trigger_kind' AS trigger_kind,
           COUNT(*)               AS open_count,
           MIN(timestamp)         AS oldest_open_at
    FROM platform.data_quality_log
    WHERE kind = '{KIND_FORENSICS_TRIGGER}'
      AND notes->>'resolved_at' IS NULL
    GROUP BY notes->>'trigger_kind'
    ORDER BY notes->>'trigger_kind'
"""

# Recent open triggers (dashboard recent list).
OPEN_RECENT_SQL = f"""
    SELECT id,
           notes->>'trigger_kind' AS trigger_kind,
           notes                  AS payload,
           timestamp              AS fired_at
    FROM platform.data_quality_log
    WHERE kind = '{KIND_FORENSICS_TRIGGER}'
      AND notes->>'resolved_at' IS NULL
    ORDER BY timestamp DESC
    LIMIT 20
"""

# Open dossier surface (ops.py operator-action panel).
OPEN_DOSSIERS_SQL = f"""
    SELECT notes->>'trigger_kind' AS trigger_kind,
           notes->>'engine'       AS engine_under_review,
           timestamp              AS fired_at
    FROM platform.data_quality_log
    WHERE kind = '{KIND_FORENSICS_TRIGGER}'
      AND notes->>'resolved_at' IS NULL
    ORDER BY timestamp DESC
"""


__all__ = [
    "KIND_FORENSICS_TRIGGER",
    "OPEN_COUNTS_BY_KIND_SQL",
    "OPEN_DOSSIERS_SQL",
    "OPEN_FINGERPRINTS_SQL",
    "OPEN_FOR_ENGINE_SQL",
    "OPEN_RECENT_SQL",
    "SOURCE_PREFIX",
    "fingerprint_exists",
    "insert_trigger",
    "mark_resolved",
    "set_dossier_path",
]
