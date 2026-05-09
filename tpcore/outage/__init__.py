"""3-tier outage policy for upstream service failures."""

from .policy import (
    OutagePolicy,
    OutageThresholds,
    OutageTier,
    classify_outage,
)


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
]
