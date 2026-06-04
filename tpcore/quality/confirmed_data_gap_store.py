"""Confirmed-data-gap evidence on the consolidated ``data_quality_log``.

Plan 2 (migration ``20260604_0300`` drops ``platform.fundamentals_period_source_evidence``;
``20260604_0500`` folds it into ``platform.data_quality_log`` via the
``kind='confirmed_data_gap_evidence'`` discriminator). This module is the single
read/write surface every confirmed-data-gap producer + the completeness-check
reader now share, so the ``kind`` filter, the jsonb ``notes`` field names, and the
dual-source EXCLUSION predicate can never drift across the (formerly raw-SQL)
call sites. It mirrors ``tpcore/forensics/dql_store.py`` (the forensics-trigger fold).

Row mapping (old ``fundamentals_period_source_evidence`` column → new ``data_quality_log``):

* ``ticker`` (text)          → ``notes->>'ticker'`` (+ duplicated into the
  ``source`` = ``confirmed_data_gap_evidence.<source_label>`` key for cheap grouping).
* ``period_end_date`` (date) → ``notes->>'period_end_date'`` (ISO ``YYYY-MM-DD``).
* ``source`` (text)          → ``notes->>'evidence_source'`` (``sec_companyfacts``,
  ``fmp_historical``, ``fmp_refresh`` — the per-period source LEG label) + the
  ``data_quality_log.source`` key (``confirmed_data_gap_evidence.<evidence_source>``).
* ``outcome`` (text)         → ``notes->>'outcome'`` (``yielded`` / ``empty`` /
  ``extract_none`` / ``fetch_failure``).
* ``attempted_at`` (tstz)    → ``timestamp`` (the freshness-window anchor).
* ``notes`` (text)           → ``notes->>'detail'`` (free-text annotation; may be NULL).

The typed metric columns (``latency_ms``/``missing_bars``/``stale``/
``confidence``) stay NULL — the ``dql_typed_cols_validation_only`` CHECK forbids
them on a non-``validation`` kind (``write_row`` raises ``ValueError`` locally if
they are passed for this kind).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog

from tpcore.quality.data_quality import KIND_CONFIRMED_DATA_GAP_EVIDENCE, write_row

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Source-key prefix; the per-period evidence-source leg is appended
# (``confirmed_data_gap_evidence.<evidence_source>``) so reads can group cheaply
# while the canonical discriminator stays ``kind='confirmed_data_gap_evidence'``.
SOURCE_PREFIX = "confirmed_data_gap_evidence"

# One evidence row, in the historical sidecar column order:
#   (ticker, period_end_date, evidence_source, outcome, detail)
EvidenceRow = tuple[str, date, str, str, str | None]


def _source_for(evidence_source: str) -> str:
    return f"{SOURCE_PREFIX}.{evidence_source}"


async def write_evidence_rows(
    pool: asyncpg.Pool,
    rows: list[EvidenceRow],
    attempted_at: datetime,
) -> int:
    """Write confirmed-data-gap evidence rows into ``data_quality_log``.

    Replaces the old idempotent UPSERT into the (now-dropped)
    ``platform.fundamentals_period_source_evidence`` table. Each row lands as a
    ``kind='confirmed_data_gap_evidence'`` row whose ``notes`` jsonb carries the
    ticker / period_end_date / evidence_source / outcome / detail; ``attempted_at``
    is the ``timestamp`` (the freshness-window anchor read by the completeness
    check). The redesigned ``data_quality_log`` has no UNIQUE(ticker, period,
    source) constraint (the uuid PK makes every row unique), so this is a plain
    INSERT per row — the completeness reader windows on the latest row by
    ``timestamp``, so a re-attempt simply appends a newer row.

    Returns the number of rows written; a no-op (returns 0) when ``rows`` is
    empty.
    """
    if not rows:
        return 0
    for ticker, period_end_date, evidence_source, outcome, detail in rows:
        notes = {
            "ticker": ticker,
            "period_end_date": period_end_date.isoformat(),
            "evidence_source": evidence_source,
            "outcome": outcome,
            "detail": detail,
        }
        await write_row(
            pool,
            kind=KIND_CONFIRMED_DATA_GAP_EVIDENCE,
            source=_source_for(evidence_source),
            timestamp=attempted_at,
            notes=notes,
        )
    logger.debug(
        "tpcore.quality.confirmed_data_gap_store.write_evidence_rows",
        rows=len(rows),
    )
    return len(rows)


# ── Read SQL fragment (shared by the completeness check reader + the ops.py
#    manifest read so the kind filter + notes field names stay in lockstep).
#
# Dual-source EXCLUSION semantics (preserved EXACTLY from the old
# ``fundamentals_period_source_evidence`` join, plan §8):
#   * freshness-gated: ``timestamp >= NOW() - ($3 days)``.
#   * at least one ``fmp_*`` leg with ``outcome IN ('empty', 'extract_none')``.
#   * at least one ``sec_companyfacts`` leg with ``outcome IN ('empty', 'extract_none')``.
#   * HARD reject the period if ANY leg in the window is ``outcome='fetch_failure'``.
EVIDENCE_JOIN_SQL = f"""
    SELECT (notes->>'period_end_date')::date AS period_end_date
    FROM platform.data_quality_log
    WHERE kind = '{KIND_CONFIRMED_DATA_GAP_EVIDENCE}'
      AND notes->>'ticker' = $1
      AND (notes->>'period_end_date')::date = ANY($2::date[])
      AND timestamp >= NOW() - ($3::int * INTERVAL '1 day')
    GROUP BY (notes->>'period_end_date')::date
    HAVING bool_or(notes->>'evidence_source' IN ('fmp_historical', 'fmp_refresh')
                   AND notes->>'outcome' IN ('empty', 'extract_none'))
       AND bool_or(notes->>'evidence_source' = 'sec_companyfacts'
                   AND notes->>'outcome' IN ('empty', 'extract_none'))
       AND NOT bool_or(notes->>'outcome' = 'fetch_failure')
"""

# Per-(ticker, period) SEC outcome read (ops.py manifest ``sec_outcome`` column).
SEC_OUTCOMES_SQL = f"""
    SELECT DISTINCT ON (notes->>'ticker', (notes->>'period_end_date')::date)
           notes->>'ticker'                  AS ticker,
           (notes->>'period_end_date')::date AS period_end_date,
           notes->>'outcome'                 AS outcome
    FROM platform.data_quality_log
    WHERE kind = '{KIND_CONFIRMED_DATA_GAP_EVIDENCE}'
      AND notes->>'evidence_source' = 'sec_companyfacts'
      AND notes->>'ticker' = ANY($1::text[])
      AND (notes->>'period_end_date')::date = ANY($2::date[])
    ORDER BY notes->>'ticker', (notes->>'period_end_date')::date, timestamp DESC
"""


__all__ = [
    "EVIDENCE_JOIN_SQL",
    "EvidenceRow",
    "KIND_CONFIRMED_DATA_GAP_EVIDENCE",
    "SEC_OUTCOMES_SQL",
    "SOURCE_PREFIX",
    "write_evidence_rows",
]
