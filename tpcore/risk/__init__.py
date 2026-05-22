"""Risk governor — enforces per-engine and platform-wide trading limits."""

from .governor import (
    CheckResult,
    InMemoryRiskStateStore,
    RiskDecision,
    RiskGovernor,
    RiskLimits,
    RiskState,
    RiskStateStore,
)
from .lifecycle_pause import (
    ENGINE_CREDIBILITY_DROP_EVENT,
    ENGINE_LIFECYCLE_DEGRADED_EVENT,
    ENGINE_LIFECYCLE_SOURCE_PREFIX,
    check_credibility_drop,
    check_lifecycle_degraded,
)
from .persistent_store import PostgresRiskStateStore

__all__ = [
    "ENGINE_CREDIBILITY_DROP_EVENT",
    "ENGINE_LIFECYCLE_DEGRADED_EVENT",
    "ENGINE_LIFECYCLE_SOURCE_PREFIX",
    "CheckResult",
    "InMemoryRiskStateStore",
    "PostgresRiskStateStore",
    "RiskDecision",
    "RiskGovernor",
    "RiskLimits",
    "RiskState",
    "RiskStateStore",
    "check_credibility_drop",
    "check_lifecycle_degraded",
]
