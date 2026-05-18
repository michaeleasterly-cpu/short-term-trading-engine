"""Single source of truth for WHEN an engine may fire.

The event-driven model (operator directive 2026-05-17): an engine
fires the moment its preconditions hold — data ready + market closed +
its cadence boundary — never on a clock. Time is a GATE, never a
trigger. This module is the declarative SoT for those preconditions,
mirroring tpcore.feeds.profile / tpcore.risk.limits_profile. It
COMPOSES tpcore.quality.validation.capital_gate (the existing
per-engine data-readiness authority — called, never re-implemented).

Landed dark: nothing imports should_fire yet (Sub-project B wires the
engine_service to it). See
docs/superpowers/specs/2026-05-17-event-driven-engine-services-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

from tpcore import calendar as cal
from tpcore.quality.validation.capital_gate import assert_passed_for_engine
from tpcore.supervisor_state import current_hold

logger = structlog.get_logger(__name__)


class Cadence(StrEnum):
    DAILY = "daily"
    MONTHLY_FIRST_TRADING_DAY = "monthly_first_trading_day"
    WEEKLY_FIRST_TRADING_DAY = "weekly_first_trading_day"


class LifecycleState(StrEnum):
    LAB = "lab"          # SP2 territory; never dispatched/allocated
    PAPER = "paper"      # graduated, paper-trading (current reality for all live engines)
    LIVE = "live"        # reserved; no engine here yet (paper-only mandate)
    RETIRED = "retired"  # snap-out complete; archive/EULOGY exists; never dispatched


# Dispatchable states. Consumed by roster_for_dispatch() (T2) and the should_fire lifecycle guard (T3).
_DISPATCHABLE: frozenset[LifecycleState] = frozenset(
    {LifecycleState.PAPER, LifecycleState.LIVE})


class EngineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    cadence: Cadence
    dispatch_order: int
    lifecycle_state: LifecycleState
    market_closed_required: bool = True
    allocator_eligible: bool = False


_PROFILE: dict[str, EngineProfile] = {
    "reversion": EngineProfile(engine="reversion", cadence=Cadence.DAILY,
                               dispatch_order=1, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "vector":    EngineProfile(engine="vector", cadence=Cadence.DAILY,
                               dispatch_order=2, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "momentum":  EngineProfile(engine="momentum", cadence=Cadence.MONTHLY_FIRST_TRADING_DAY,
                               dispatch_order=3, lifecycle_state=LifecycleState.PAPER,
                               allocator_eligible=True),
    "sentinel":  EngineProfile(engine="sentinel", cadence=Cadence.DAILY,
                               dispatch_order=4, lifecycle_state=LifecycleState.PAPER),
    "canary":    EngineProfile(engine="canary", cadence=Cadence.DAILY,
                               dispatch_order=5, lifecycle_state=LifecycleState.PAPER),
    # allocator: separate _dispatch_allocator path (NOT in the ROSTER loop, D-SDLC1-4).
    "allocator": EngineProfile(engine="allocator", cadence=Cadence.WEEKLY_FIRST_TRADING_DAY,
                               dispatch_order=0, lifecycle_state=LifecycleState.PAPER),
    # sigma RETIRED (data-SDLC RETIRED symmetry, D-SDLC1-2). cadence/dispatch_order are arbitrary inert placeholders — RETIRED engines are filtered out of every dispatch/allocator accessor (T2) so these values are never consumed (D-SDLC1-6).
    "sigma":     EngineProfile(engine="sigma", cadence=Cadence.DAILY,
                               dispatch_order=99, lifecycle_state=LifecycleState.RETIRED),
}


def profile_for(engine: str) -> EngineProfile | None:
    """The EngineProfile for an engine, or None if unprofiled."""
    return _PROFILE.get(engine)


def _week_start_date(d: date) -> date:
    """Monday of d's ISO week (date)."""
    return d - timedelta(days=d.weekday())


def _cadence_boundary(profile: EngineProfile, now: datetime) -> bool:
    """True iff ``now``'s date is this profile's cadence boundary (XNYS)."""
    d = now.date()
    if profile.cadence is Cadence.DAILY:
        return cal.is_trading_day(now)
    if profile.cadence is Cadence.MONTHLY_FIRST_TRADING_DAY:
        return d == cal.first_session_of_month(d.year, d.month)
    if profile.cadence is Cadence.WEEKLY_FIRST_TRADING_DAY:
        wk_start = _week_start_date(d)
        sessions = cal.sessions_in_range(wk_start, d)
        return bool(sessions) and sessions[0] == d
    return False  # unknown cadence → fail-closed


def _midnight_utc(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _cadence_window_start(profile: EngineProfile, now: datetime) -> datetime:
    """Start (UTC) of the cadence cycle containing ``now``.

    A run record at/after this instant means the engine already ran
    this cycle. Daily = midnight UTC of now's date; monthly = midnight
    of the month's first session; weekly = midnight of the week's
    first session.
    """
    d = now.date()
    if profile.cadence is Cadence.DAILY:
        return _midnight_utc(d)
    if profile.cadence is Cadence.MONTHLY_FIRST_TRADING_DAY:
        return _midnight_utc(cal.first_session_of_month(d.year, d.month))
    if profile.cadence is Cadence.WEEKLY_FIRST_TRADING_DAY:
        sessions = cal.sessions_in_range(_week_start_date(d), d)
        return _midnight_utc(sessions[0] if sessions else d)
    return _midnight_utc(d)  # unknown → narrowest safe window (today)


def cadence_window_start(engine: str, now: datetime) -> datetime:
    """Public: start (UTC) of the cadence cycle containing ``now`` for
    ``engine`` (the single cadence-window authority — wraps
    :func:`_cadence_window_start`). Unprofiled engine → narrowest safe
    window (midnight UTC of now's date)."""
    profile = profile_for(engine)
    if profile is None:
        return _midnight_utc(now.date())
    return _cadence_window_start(profile, now)


@dataclass(frozen=True)
class FireDecision:
    fire: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)


_RUN_START_EVENT = "STARTUP"  # tpcore/logging/db_handler.py:115 (canonical run-start)


async def _already_ran(engine: str, pool, window_start: datetime) -> bool:
    async with pool.acquire() as conn:
        hit = await conn.fetchval(
            """
            SELECT 1 FROM platform.application_log
            WHERE engine = $1 AND event_type = $2 AND recorded_at >= $3
            LIMIT 1
            """,
            engine, _RUN_START_EVENT, window_start,
        )
    return hit is not None


async def should_fire(engine: str, now: datetime, pool) -> FireDecision:
    """Fail-CLOSED gate: True only if every precondition holds.

    Order (short-circuit): profiled → cadence boundary → market closed
    → data ready (capital_gate) → not already run this cycle. ANY
    error/ambiguity → fire=False (never trade on doubt).
    """
    checks: dict[str, bool] = {}
    try:
        profile = profile_for(engine)
        checks["profiled"] = profile is not None
        if profile is None:
            return FireDecision(False, "unprofiled engine", checks)

        checks["cadence"] = _cadence_boundary(profile, now)
        if not checks["cadence"]:
            return FireDecision(False, "not a cadence boundary", checks)

        if profile.market_closed_required:
            closed = not cal.session_contains(now)
            checks["market_closed"] = closed
            if not closed:
                return FireDecision(False, "market open", checks)
        else:
            checks["market_closed"] = True

        hold = await current_hold(pool, engine)
        checks["supervisor_held"] = hold is None
        if hold is not None:
            return FireDecision(False, "supervisor hold", checks)

        try:
            await assert_passed_for_engine(pool, engine)
            checks["data_ready"] = True
        except Exception as exc:  # noqa: BLE001 — any data-gate failure = not ready
            checks["data_ready"] = False
            return FireDecision(False, f"data not ready: {exc}", checks)

        ran = await _already_ran(engine, pool, _cadence_window_start(profile, now))
        checks["not_already_run"] = not ran
        if ran:
            return FireDecision(False, "already ran this cycle", checks)

        return FireDecision(True, "ready", checks)
    except Exception as exc:  # noqa: BLE001 — fail-closed on ANYTHING unexpected
        logger.warning("tpcore.engine_profile.should_fire_error",
                        engine=engine, error=str(exc))
        return FireDecision(False, f"error: {exc}", checks)
