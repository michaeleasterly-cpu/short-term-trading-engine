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

from datetime import date, datetime, timedelta
from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

from tpcore import calendar as cal

logger = structlog.get_logger(__name__)


class Cadence(StrEnum):
    DAILY = "daily"
    MONTHLY_FIRST_TRADING_DAY = "monthly_first_trading_day"
    WEEKLY_FIRST_TRADING_DAY = "weekly_first_trading_day"


class EngineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    cadence: Cadence
    market_closed_required: bool = True


_PROFILE: dict[str, EngineProfile] = {
    "reversion": EngineProfile(engine="reversion", cadence=Cadence.DAILY),
    "vector":    EngineProfile(engine="vector",    cadence=Cadence.DAILY),
    "sentinel":  EngineProfile(engine="sentinel",  cadence=Cadence.DAILY),
    "momentum":  EngineProfile(engine="momentum",  cadence=Cadence.MONTHLY_FIRST_TRADING_DAY),
    # allocator profile present (this is the SoT); consumed in Sub-project C.
    "allocator": EngineProfile(engine="allocator", cadence=Cadence.WEEKLY_FIRST_TRADING_DAY),
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
