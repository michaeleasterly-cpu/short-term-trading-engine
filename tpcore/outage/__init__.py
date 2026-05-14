"""3-tier outage policy + retry primitive for upstream service failures."""

from .policy import (
    OutagePolicy,
    OutageThresholds,
    OutageTier,
    classify_outage,
)
from .retry import with_retry


class DataProviderOutage(RuntimeError):
    """Raised by data adapters when an upstream is persistently unreachable.

    Engines treat this as a hard fail for the affected scan/candidate —
    "no data, no trade." Distinct from ``BrokerUnavailableError`` (which
    is the broker-side equivalent in ``tpcore.alpaca``) so callers can
    differentiate failure modes.
    """


__all__ = [
    "DataProviderOutage",
    "OutagePolicy",
    "OutageThresholds",
    "OutageTier",
    "classify_outage",
    "with_retry",
]
