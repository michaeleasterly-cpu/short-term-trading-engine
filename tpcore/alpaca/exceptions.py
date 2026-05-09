"""Adapter-local exception types for the Alpaca broker integration."""
from __future__ import annotations


class BrokerUnavailableError(RuntimeError):
    """Raised when the broker has been unreachable past the kill-switch threshold.

    The decision is made by ``tpcore.outage.classify_outage`` — the adapter
    tracks consecutive failures and raises this once the policy returns
    ``OutageTier.KILL_SWITCH``. Callers (the engine's order manager and the
    Risk Governor) should treat this as a hard stop on new submissions.
    """
