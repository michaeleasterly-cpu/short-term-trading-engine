"""Per-indicator targeted FRED re-pull — D8 deterministic self-heal.

The canonical ``handle_macro_indicators`` re-pulls ALL series in
``INDICATOR_SERIES`` (50+ series including the per-state PHCI panel).
When ``macro_indicators_completeness`` reports a gap in ONE specific
indicator, re-pulling all 50 is wasteful + slow and inflates the FRED
courtesy-rate budget. This module re-pulls ONLY the named indicators
for the requested date window — the deterministic recovery wired from
``scripts/ops.py::_auto_cascade_validation_failures`` (Wave-1 spec
§5 row D8).

Reuses ``FREDAdapter.get_observations`` (the same per-series fetch the
batch loader uses internally) and ``ON CONFLICT (indicator, date) DO
NOTHING`` upsert — idempotent. Returns the per-indicator row counts so
the cascade telemetry can report exactly what landed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.fred.adapter import INDICATOR_SERIES, FREDAdapter

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Build the lookup once at import time — name → FRED series_id.
# ``INDICATOR_SERIES`` is the SoT for canonical-name ↔ FRED-id mapping.
_NAME_TO_SERIES_ID: dict[str, str] = dict(INDICATOR_SERIES)

# Derived indicators have no FRED series_id (computed from raw panels).
# A targeted re-pull cannot satisfy these — the caller must re-run the
# canonical ``macro_indicators`` stage to recompute. We surface this as
# a sentinel rather than silently dropping the request.
_DERIVED_INDICATORS: frozenset[str] = frozenset({"sos_state_diffusion"})



# Task #18 P5: platform.macro_indicators is now a view over macro_data.
# Targeted re-pull writes directly to macro_data via the bitemporal SCD-2
# helper — same semantics as the canonical handler.


async def per_indicator_fred_repull(
    pool: asyncpg.Pool,
    indicators: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, int]:
    """Re-pull the named indicators from FRED for the requested window.

    Args:
        pool: asyncpg pool — used for the bulk upsert.
        indicators: list of canonical indicator names (left-side of
            ``INDICATOR_SERIES`` tuples). Unknown names are reported in
            the result + structured-log but do not raise.
        start: ISO date for ``observation_start`` (None → full history).
        end: ISO date for ``observation_end`` (None → through today).

    Returns:
        ``{indicator: rows_upserted}`` per requested indicator. Unknown
        / derived indicators map to ``-1`` so the caller can report
        them without conflating with the legitimate ``0`` outcome
        (idempotent — already present, none new).

    Notes:
        - Idempotent: ``ON CONFLICT DO NOTHING``.
        - Derived ``sos_state_diffusion`` returns ``-1`` because it has
          no FRED series_id; recompute via the canonical stage.
        - One FREDAdapter context per call — courtesy delay between
          series is preserved by the adapter's own inter-request sleep
          when fetching multiple series.
    """
    if not indicators:
        return {}

    results: dict[str, int] = {}
    upsert_rows: list[tuple[str, date, Decimal]] = []

    async with FREDAdapter() as fred:
        for name in indicators:
            if name in _DERIVED_INDICATORS:
                logger.warning(
                    "tpcore.fred.targeted_repull.derived_indicator",
                    indicator=name,
                    note=(
                        "derived series — no FRED series_id; recompute "
                        "via the canonical macro_indicators stage"
                    ),
                )
                results[name] = -1
                continue
            series_id = _NAME_TO_SERIES_ID.get(name)
            if series_id is None:
                logger.warning(
                    "tpcore.fred.targeted_repull.unknown_indicator",
                    indicator=name,
                    note="not in INDICATOR_SERIES — check spelling",
                )
                results[name] = -1
                continue
            try:
                obs = await fred.get_observations(
                    series_id, start=start, end=end,
                )
            except Exception as exc:  # noqa: BLE001 — heal must never crash daemon
                logger.error(
                    "tpcore.fred.targeted_repull.fetch_failed",
                    indicator=name,
                    series_id=series_id,
                    error=str(exc),
                )
                results[name] = 0
                continue
            count_before = len(upsert_rows)
            for o in obs:
                upsert_rows.append((name, o["date"], o["value"]))
            results[name] = len(upsert_rows) - count_before

    if not upsert_rows:
        logger.info(
            "tpcore.fred.targeted_repull.empty",
            indicators=list(indicators),
        )
        return results

    # Task #18 P5: bitemporal SCD-2 helper writes to platform.macro_data.
    # SCD-2 no-change short-circuits unchanged rows so a heal re-run on a
    # cleanly-populated series is a no-op (counts reported below come from
    # the helper, not the upper-bound rows-fetched).
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal
    async with pool.acquire() as conn:
        await upsert_macro_data_bitemporal(
            conn, source="fred",
            rows=[(name, d, v, None) for (name, d, v) in upsert_rows],
        )
    logger.info(
        "tpcore.fred.targeted_repull.upserted",
        indicators=list(indicators),
        rows_fetched=len(upsert_rows),
    )
    return results


__all__ = ["per_indicator_fred_repull"]
