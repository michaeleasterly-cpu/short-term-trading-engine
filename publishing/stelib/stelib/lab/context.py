"""Lab-isolation contextvar primitives, **carved without the DB driver
pool plumbing**.

The full ``tpcore.lab.context`` module in the source repo also owns
``LabContext`` — an async context manager that builds two ``DB driver``
pools (one read-only, one allowlisted RW credibility-append pool) and
flips the ``_LAB_ACTIVE`` contextvar so the runtime-side guards know to
fail closed. That pool-management code requires ``DB driver`` at runtime
and depends on the (uncarved) ``tpcore.db.build_DB driver_pool``, so it
was deliberately omitted from this ``stelib`` carve.

What stays here is the pure-stdlib half: the contextvar plus the
``assert_not_in_lab()`` guard that every live-side-effect entrypoint in
``risk/``, ``order_management/``, ``aar/``, etc. calls before doing
anything observable. Callers who want the full pool-aware ``LabContext``
should either pull it from the source repo, or write a minimal
contextvar-flipping CM of their own (the contextvar is exported here as
``_LAB_ACTIVE`` precisely so the surrounding tooling can drive it)."""

from __future__ import annotations

import contextvars

_LAB_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_LAB_ACTIVE", default=False)

_ACTIVE_CRED_POOL: contextvars.ContextVar = contextvars.ContextVar(
    "_ACTIVE_CRED_POOL", default=None)


def active_credibility_pool():
    """The active LabContext's single allowlisted RW credibility pool,
    or ``None`` if no LabContext is active (legacy non-Lab path).
    Public accessor — never reach into a Lab-context object's internals
    (STYLE_GUIDE private-attribute rule)."""
    return _ACTIVE_CRED_POOL.get()


class LabIsolationViolation(RuntimeError):
    """A live side-effect class was constructed inside an active Lab
    run."""


def lab_is_active() -> bool:
    return _LAB_ACTIVE.get()


def assert_not_in_lab() -> None:
    """Guard installed at every live-side-effect boundary.

    Raises ``LabIsolationViolation`` if a Lab run is active — the
    fail-closed reentrancy layer."""
    if _LAB_ACTIVE.get():
        raise LabIsolationViolation(
            "live side-effect path reached inside an active Lab run "
            "(stelib isolation contract). If a Lab run legitimately "
            "needs risk/aar/order/broker/startup logic, wire it OUTSIDE "
            "the Lab CM (e.g. InMemoryRiskStateStore + a mock broker).")
