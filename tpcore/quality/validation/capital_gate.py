"""Engine graduation gate — `assert_passed` is called by Sigma + Reversion.

Per spec §5: the engines call this before "the graduation return path"
(i.e. before swapping from the pre-grad cap to live sizing). It raises
:class:`ValidationStaleError` if no recent suite run exists, or
:class:`ValidationFailedError` if the most recent run had any check
fail.

The lookup is intentionally narrow: query the most recent timestamp for
any ``validation.%`` source, then collect every row sharing that
timestamp (the suite writes every source with one shared ``started_at``).
If any expected source is missing at that timestamp, the run was partial
and is treated as a failure.

``EXPECTED_SOURCES`` derives from :data:`tpcore.quality.validation.suite.KNOWN_CHECK_NAMES`
so adding a check to the suite automatically makes it required here.
Audit-fix D3-1 (2026-05-14): replaces a hardcoded 3-name set that had
drifted as the suite grew from 3 → 10 checks.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from .suite import KNOWN_CHECK_NAMES

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

EXPECTED_SOURCES = frozenset(f"validation.{name}" for name in KNOWN_CHECK_NAMES)

# ── Per-engine data dependency map (#166) ─────────────────────────────
# EVIDENCE-DERIVED, not invented: each engine→table pair is a real
# ``platform.<table>`` read in that engine's own code (grep 2026-05-16,
# /tests/ excluded). Internal/meta tables (data_quality_log,
# application_log, open_orders, risk_state, aar_events,
# universe_candidates) are platform STATE, not validation-gated feeds,
# so they are intentionally absent.
#
#   sigma     prices_daily          backtest.py:31,86,239;
#                                    scheduler.py:129,234
#             liquidity_tiers        backtest.py:106,114,951
#   reversion prices_daily          backtest.py:18,223; scheduler.py:112
#             fundamentals_quarterly backtest.py:19,38,257,976,1167,1176
#   vector    prices_daily          backtest.py:216; scheduler.py:89,194
#             fundamentals_quarterly backtest.py:247,843,1024;
#                                    scheduler.py:132
#             earnings_events        backtest.py:269,847,1028
#   momentum  prices_daily          backtest.py:31,167;
#                                    plugs/setup_detection.py:186
#             liquidity_tiers        backtest.py:154,157;
#                                    plugs/setup_detection.py:5,144,165,172
#   sentinel  prices_daily          backtest.py:5,83,89; models.py:267;
#                                    plugs/setup_detection.py:297;
#                                    scheduler.py:320
#             macro_indicators       backtest.py:4; models.py:34;
#                                    plugs/setup_detection.py:63,260
ENGINE_TABLES: dict[str, frozenset[str]] = {
    "sigma": frozenset({"prices_daily", "liquidity_tiers"}),
    "reversion": frozenset({"prices_daily", "fundamentals_quarterly"}),
    "vector": frozenset({
        "prices_daily", "fundamentals_quarterly", "earnings_events",
    }),
    "momentum": frozenset({"prices_daily", "liquidity_tiers"}),
    "sentinel": frozenset({"prices_daily", "macro_indicators"}),
}


class ValidationStaleError(RuntimeError):
    """No recent validation run within ``max_age_days``."""


class ValidationFailedError(RuntimeError):
    """The most recent validation run had at least one failing check."""


def _required_sources(engine: str) -> frozenset[str]:
    """The ``validation.<check>`` sources an engine actually depends on.

    Table→checks is taken from the selfheal registry's ``source``
    field — the existing single source of truth (the registry-coverage
    test guarantees it == KNOWN_CHECK_NAMES, so this can never drift).
    An unknown engine fails SAFE: gated on EVERY source (the old
    global behaviour), never on an empty set.
    """
    tables = ENGINE_TABLES.get(engine)
    if not tables:
        return EXPECTED_SOURCES
    from tpcore.selfheal.registry import HEAL_SPECS  # lazy: avoid cycle

    req = {
        f"validation.{check}"
        for check, spec in HEAL_SPECS.items()
        if spec.source in tables
    }
    # Fail safe: an engine that mapped to zero checks would be silently
    # un-gated — never allow that.
    return frozenset(req) if req else EXPECTED_SOURCES


def _evaluate(
    rows: list[dict], required: frozenset[str], max_age_days: int,
) -> None:
    """Shared gate logic. Raises on stale run, or on missing/failed
    sources WITHIN ``required``. Run staleness is global — a stale run
    can't be trusted for ANY engine (safety)."""
    if not rows:
        raise ValidationStaleError("no validation runs found in data_quality_log")
    latest_ts = max(r["timestamp"] for r in rows)
    age = datetime.now(UTC) - latest_ts
    if age > timedelta(days=max_age_days):
        raise ValidationStaleError(
            f"most recent validation run is {age.days} days old (max {max_age_days})"
        )
    latest_rows = [r for r in rows if r["timestamp"] == latest_ts]
    present = {r["source"] for r in latest_rows}
    missing = required - present
    if missing:
        raise ValidationFailedError(
            f"most recent validation run is missing required sources: "
            f"{sorted(missing)}"
        )
    failed = sorted(
        r["source"] for r in latest_rows
        if r["stale"] and r["source"] in required
    )
    if failed:
        raise ValidationFailedError(
            f"most recent validation run had failing required checks: {failed}"
        )
    logger.debug(
        "tpcore.validation.gate.passed",
        latest_ts=latest_ts.isoformat(), age_days=age.days,
        required=len(required),
    )


async def assert_passed(pool: asyncpg.Pool, *, max_age_days: int = 7) -> None:
    """GLOBAL all-green gate (behaviour unchanged). Raise unless the
    most recent suite run is fresh and EVERY check passed. Retained as
    the operator's all-green safety override — see
    ``assert_passed_for_engine``."""
    rows = await _fetch_validation_rows(pool)
    _evaluate(rows, EXPECTED_SOURCES, max_age_days)


async def assert_passed_for_engine(
    pool: asyncpg.Pool,
    engine: str,
    *,
    require_all_green: bool = False,
    max_age_days: int = 7,
) -> None:
    """Per-engine gate (#166): block ``engine`` ONLY if a source IT
    actually reads (``ENGINE_TABLES`` → registry) is missing/stale/
    failed at the latest run. A red check on data this engine never
    reads does NOT block it. This is a REFINEMENT of "100% data or
    don't trade" — the engine still needs 100% of ITS data — not a
    weakening. ``require_all_green=True`` restores the global all-green
    behaviour (operator safety override)."""
    rows = await _fetch_validation_rows(pool)
    required = EXPECTED_SOURCES if require_all_green else _required_sources(engine)
    _evaluate(rows, required, max_age_days)


async def _fetch_validation_rows(pool: asyncpg.Pool) -> list[dict]:
    sql = """
        SELECT source, timestamp, stale
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
        ORDER BY timestamp DESC
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql)


__all__ = [
    "assert_passed",
    "assert_passed_for_engine",
    "ENGINE_TABLES",
    "ValidationFailedError",
    "ValidationStaleError",
    "EXPECTED_SOURCES",
]
