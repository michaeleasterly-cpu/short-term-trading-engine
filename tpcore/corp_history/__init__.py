"""Corporate-history graph helpers — first-deliverable scope.

Layered on top of platform.{issuers, issuer_securities, issuer_history,
corporate_events} (created in migration 20260524_1600) plus the existing
platform.ticker_history (SCD-2 per-security ticker tracking).

This module is intentionally small. The first deliverable per spec
docs/superpowers/specs/2026-05-24-corporate-history-enrichment.md v0.2
ships ONE query helper:

  resolve_issuer_at_date(conn, ticker, as_of) -> issuer_id | None

Future deliverables (P4 of the spec) extend with:
  walk_successors(issuer_id) -> list[(issuer_id, hop_count)]
  walk_predecessors(issuer_id) -> list[(issuer_id, hop_count)]
  events_affecting(ticker, start, end) -> list[CorporateEvent]

Pattern: recursive CTE on corporate_events (Postgres-native; <10 hops in
99.9% of real corp graphs per the expert review).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from datetime import date

    import asyncpg

logger = structlog.get_logger(__name__)


_RESOLVE_ISSUER_SQL = """
    -- Step 1: ticker → classification_id valid at as_of_date.
    -- Uses ticker_history's SCD-2 timeline (valid_from <= as_of <= valid_to).
    WITH security AS (
        SELECT classification_id
        FROM platform.ticker_history
        WHERE ticker = $1
          AND valid_from <= $2
          AND (valid_to IS NULL OR valid_to >= $2)
        ORDER BY valid_from DESC
        LIMIT 1
    )
    -- Step 2: classification_id → issuer_id valid at as_of_date.
    -- Uses issuer_securities' SCD-2 timeline.
    SELECT iss.issuer_id
    FROM security s
    JOIN platform.issuer_securities iss
      ON iss.classification_id = s.classification_id
     AND iss.valid_from <= $2
     AND (iss.valid_to IS NULL OR iss.valid_to >= $2)
    ORDER BY iss.valid_from DESC
    LIMIT 1
"""


async def resolve_issuer_at_date(
    conn: asyncpg.Connection,
    ticker: str,
    as_of: date,
) -> str | None:
    """Resolve a ticker observed at a given date to its issuer_id.

    Returns None when:
      - ticker has no entry in ticker_history at that date
      - the security's classification_id has no issuer_securities mapping
        at that date (the issuer-graph hasn't been populated yet for it)

    Returns the issuer_id otherwise. The lookup is two SCD-2 hops:
    ticker → classification_id (via ticker_history) → issuer_id (via
    issuer_securities). Both hops use valid_from <= as_of <= valid_to
    semantics so historical bars resolve to the historical issuer even
    after renames / mergers / share-class restructuring.

    Args:
        conn: asyncpg Connection. Caller controls transaction scope.
        ticker: the ticker symbol as observed on as_of date.
        as_of: the date at which to resolve the ticker (typically the
            row date being attributed).

    Returns:
        issuer_id (str) or None if the chain breaks.
    """
    return await conn.fetchval(_RESOLVE_ISSUER_SQL, ticker, as_of)


__all__ = ["resolve_issuer_at_date"]
