"""Task #18 P3 — bitemporal double-write helper for platform.macro_data.

The 3 macro handlers (handle_macro_indicators, handle_aaii_sentiment,
handle_fear_greed) call this helper after their existing legacy-table
INSERT so each emission lands in BOTH the legacy table AND macro_data
during the parallel-write window (spec phase A).

The helper enforces canonical SCD-2 semantics so macro_data does NOT
accumulate redundant rows across cron cycles:

  - If no current row exists for (source, series_id, observed_date):
      INSERT new row with realtime_end='infinity'.
  - If current row's value matches the new value:
      NO-OP (do not write — bitemporal "still true as of now").
  - If current row's value differs:
      REVISION: close the current row (realtime_end := clock_timestamp())
      and INSERT a new row with realtime_start := clock_timestamp().

All work happens in a single SQL round-trip per batch via CTE chain —
no row-by-row Python loop, so 60K-row weekly emissions stay sub-second.

This module exists for the P3-P4 parallel-write window. After P5 cutover
+ P6 consumer migration + P7 legacy-table drop, the producers write
DIRECTLY to macro_data and this helper module is deleted.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    import asyncpg

logger = structlog.get_logger(__name__)


# Tuple shape: (series_id, observed_date, value_num, value_text)
# Exactly one of value_num / value_text must be non-NULL per row
# (enforced by the table's macro_data_value_xor CHECK).
MacroRow = tuple[str, "date", "Decimal | float | None", str | None]


_UPSERT_SQL = """
WITH new_rows AS (
    SELECT
        $1::text AS source,
        unnest($2::text[]) AS series_id,
        unnest($3::date[]) AS observed_date,
        unnest($4::numeric[]) AS value_num,
        unnest($5::text[]) AS value_text
),
current_rows AS (
    SELECT n.series_id, n.observed_date, n.value_num, n.value_text,
           m.value_num AS cur_num, m.value_text AS cur_text,
           (m.source IS NOT NULL) AS has_current
    FROM new_rows n
    LEFT JOIN platform.macro_data m
      ON m.source = $1
     AND m.series_id = n.series_id
     AND m.observed_date = n.observed_date
     AND m.realtime_end = 'infinity'
),
classified AS (
    SELECT *,
           CASE
               WHEN NOT has_current THEN 'inserted'
               WHEN value_num  IS NOT DISTINCT FROM cur_num
                AND value_text IS NOT DISTINCT FROM cur_text THEN 'no_change'
               ELSE 'revised'
           END AS action
    FROM current_rows
),
closed AS (
    UPDATE platform.macro_data m
    SET realtime_end = clock_timestamp()
    FROM classified c
    WHERE m.source = $1
      AND m.series_id = c.series_id
      AND m.observed_date = c.observed_date
      AND m.realtime_end = 'infinity'
      AND c.action = 'revised'
    RETURNING 1
),
inserted AS (
    INSERT INTO platform.macro_data
        (source, series_id, observed_date,
         value_num, value_text,
         realtime_start, realtime_end, recorded_at)
    SELECT $1, series_id, observed_date,
           value_num, value_text,
           clock_timestamp(), 'infinity', clock_timestamp()
    FROM classified
    WHERE action IN ('inserted', 'revised')
    ON CONFLICT (source, series_id, observed_date, realtime_start) DO NOTHING
    RETURNING 1
)
SELECT
    (SELECT count(*) FROM classified WHERE action = 'inserted') AS n_inserted,
    (SELECT count(*) FROM classified WHERE action = 'revised')  AS n_revised,
    (SELECT count(*) FROM classified WHERE action = 'no_change') AS n_no_change
"""


async def upsert_macro_data_bitemporal(
    conn: asyncpg.Connection,
    *,
    source: str,
    rows: Sequence[MacroRow],
) -> dict[str, int]:
    """Bitemporal SCD-2 upsert for a batch of macro_data rows.

    Args:
        conn: asyncpg Connection. Caller controls transaction scope —
              this helper does NOT open/close a transaction itself so
              the legacy + bitemporal writes can be atomic together.
        source: 'fred' | 'aaii' | 'cnn_fear_greed' | ...
        rows: list of (series_id, observed_date, value_num, value_text).
              Exactly one of value_num / value_text per row.

    Returns:
        {'inserted': N, 'revised': N, 'no_change': N}

    The full batch is processed in a single CTE-chained round-trip so
    even 60K-row emissions are sub-second.
    """
    if not rows:
        return {"inserted": 0, "revised": 0, "no_change": 0}

    series_ids = [r[0] for r in rows]
    observed_dates = [r[1] for r in rows]
    value_nums = [r[2] for r in rows]
    value_texts = [r[3] for r in rows]

    result = await conn.fetchrow(
        _UPSERT_SQL, source, series_ids, observed_dates, value_nums, value_texts,
    )
    out = {
        "inserted":  int(result["n_inserted"]) if result else 0,
        "revised":   int(result["n_revised"]) if result else 0,
        "no_change": int(result["n_no_change"]) if result else 0,
    }
    if out["inserted"] or out["revised"]:
        logger.info(
            "ingestion.macro_data_emit.upserted",
            source=source, batch_size=len(rows), **out,
        )
    return out


__all__ = ["MacroRow", "upsert_macro_data_bitemporal"]
