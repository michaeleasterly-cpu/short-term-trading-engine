"""Engine graduation gate — `assert_passed` is called by the per-trade
engines (Reversion + Vector; Sigma archived 2026-05-16).

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
# DERIVED read-model over the SINGLE SoT in tpcore.engine_profile._PROFILE
# (folded 2026-05-20 per TODO L540-544 + spec docs/superpowers/specs/
# 2026-05-20-declarative-engine-profile-data-dependencies.md). Each
# engine's EngineProfile.data_dependencies is the canonical authoritative
# declaration of which ``platform.<table>`` reads it has — evidence-
# derived from the engine's real reads (the per-engine commentary that
# lived here pre-migration was preserved into the original SoT-curation
# context and remains available in git history; the contemporary
# canonical declaration is the EngineProfile field).
#
# The back-compat `ENGINE_TABLES` module attribute was REMOVED in the §7.4
# follow-up to the declarative engine_profile data_dependencies migration
# (PRs #171/#191 + this PR). All external consumers now call
# `tpcore.engine_profile.engine_data_dependencies(engine)` directly, which
# reads EngineProfile.data_dependencies — the canonical SoT. A drift
# sentinel (`tpcore/tests/test_engine_profile.py
# ::test_engine_tables_shim_removed`) reds CI on any future re-introduction
# of the parallel read-model. See:
# `docs/superpowers/specs/2026-05-20-declarative-engine-profile-data-dependencies.md`
# §7.4.


class ValidationStaleError(RuntimeError):
    """No recent validation run within ``max_age_days``."""


class ValidationFailedError(RuntimeError):
    """The most recent validation run had at least one failing check."""


def _required_sources(engine: str) -> frozenset[str]:
    """The ``validation.<check>`` sources an engine actually depends on.

    Table→checks is taken from the selfheal registry's ``source``
    field — the existing single source of truth (the registry-coverage
    test guarantees it == KNOWN_CHECK_NAMES, so this can never drift).
    Data-dependencies come from the SINGLE SoT
    :func:`tpcore.engine_profile.engine_data_dependencies` (folded
    2026-05-20). An unknown / un-declared engine fails SAFE: gated on
    EVERY source (the old global behaviour), never on an empty set.
    """
    from tpcore.engine_profile import engine_data_dependencies  # lazy: avoid cycle

    tables = engine_data_dependencies(engine)
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


def _is_red(row: dict, max_age_days: int) -> bool:
    """The single per-row red predicate. A latest-run row counts as a
    failing check iff it is marked ``stale``. ``max_age_days`` is part
    of the signature because run-level staleness is a separate, global
    concern handled by the callers (a globally-stale run can't be
    trusted for ANY source); it is intentionally unused here so the
    per-row predicate stays the one shared definition of "red".
    """
    del max_age_days  # global staleness is handled by callers, not per-row
    return bool(row["stale"])


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
        if _is_red(r, max_age_days) and r["source"] in required
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
    actually reads (``EngineProfile.data_dependencies`` → registry) is
    missing/stale/failed at the latest run. A red check on data this
    engine never reads does NOT block it. This is a REFINEMENT of "100%
    data or don't trade" — the engine still needs 100% of ITS data —
    not a weakening. ``require_all_green=True`` restores the global
    all-green behaviour (operator safety override)."""
    rows = await _fetch_validation_rows(pool)
    required = EXPECTED_SOURCES if require_all_green else _required_sources(engine)
    _evaluate(rows, required, max_age_days)


async def failing_sources_for_engine(
    pool: asyncpg.Pool,
    engine: str,
    *,
    max_age_days: int = 7,
) -> list[str]:
    """NON-raising. Return ``engine``'s failing data sources in the
    HealSpec.source vocabulary (the selfheal-registry ``source``
    namespace ``EngineProfile.data_dependencies`` is built from) — the
    locked inter-lane contract vocabulary for Sub-project B.

    A source is failing if any required ``validation.<check>`` that
    maps to it (via the SAME ``HEAL_SPECS``→source iteration
    :func:`_required_sources` uses) is missing from the latest run or
    is :func:`_is_red`. A globally-stale or empty run is treated as
    "no trustworthy data" → every required source fails. An unknown /
    un-declared engine has no ``data_dependencies`` entry, so there is
    nothing to heal/report → ``[]``.
    """
    from tpcore.engine_profile import engine_data_dependencies  # lazy: avoid cycle

    tables = engine_data_dependencies(engine)
    if not tables:  # unknown / unmapped engine: nothing to report
        return []
    from tpcore.selfheal.registry import HEAL_SPECS  # lazy: avoid cycle

    # Same mechanism as _required_sources, but keep the validation-key →
    # HealSpec.source mapping so a failing key reports in source vocab.
    key_to_source = {
        f"validation.{check}": spec.source
        for check, spec in HEAL_SPECS.items()
        if spec.source in tables
    }
    if not key_to_source:  # fail safe mirrors _required_sources
        key_to_source = {
            f"validation.{check}": spec.source
            for check, spec in HEAL_SPECS.items()
        }

    rows = await _fetch_validation_rows(pool)
    if not rows:
        return sorted(set(key_to_source.values()))
    latest_ts = max(r["timestamp"] for r in rows)
    age = datetime.now(UTC) - latest_ts
    if age > timedelta(days=max_age_days):
        # Globally-stale run can't be trusted for ANY source.
        return sorted(set(key_to_source.values()))
    latest = {
        r["source"]: r for r in rows if r["timestamp"] == latest_ts
    }
    failing: set[str] = set()
    for key, source in key_to_source.items():
        row = latest.get(key)
        if row is None or _is_red(row, max_age_days):
            failing.add(source)
    return sorted(failing)


async def _fetch_validation_rows(pool: asyncpg.Pool) -> list[dict]:
    sql = """
        SELECT source, timestamp, stale
        FROM platform.data_quality_log
        WHERE kind = 'validation' AND source LIKE 'validation.%'
        ORDER BY timestamp DESC
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql)


__all__ = [
    "assert_passed",
    "assert_passed_for_engine",
    "failing_sources_for_engine",
    "ValidationFailedError",
    "ValidationStaleError",
    "EXPECTED_SOURCES",
]
