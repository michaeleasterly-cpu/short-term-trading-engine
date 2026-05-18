from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

_LAB_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_LAB_ACTIVE", default=False)

_ACTIVE_CRED_POOL: contextvars.ContextVar = contextvars.ContextVar(
    "_ACTIVE_CRED_POOL", default=None)


def active_credibility_pool():
    """The active LabContext's single allowlisted RW credibility pool,
    or None if no LabContext is active (legacy non-Lab path). Public
    accessor — never reach into LabContext internals (STYLE_GUIDE
    private-attribute rule)."""
    return _ACTIVE_CRED_POOL.get()


class LabIsolationViolation(RuntimeError):
    """A live side-effect class was constructed inside an active Lab run."""


def lab_is_active() -> bool:
    return _LAB_ACTIVE.get()


def assert_not_in_lab() -> None:
    """Guard installed at every live-side-effect boundary (T4).
    Raises if a Lab run is active — the fail-closed reentrancy layer."""
    if _LAB_ACTIVE.get():
        raise LabIsolationViolation(
            "live side-effect path reached inside an active Lab run "
            "(SDLC SP2 isolation contract). If a Lab run legitimately needs "
            "risk/aar/order/broker/startup logic, wire it OUTSIDE LabContext "
            "(e.g. InMemoryRiskStateStore + a mock broker) — engine "
            "run_*_with_context backtest paths construct none of these by "
            "design (SP2 C2).")


class LabContext:
    """Async CM: marks the Lab active (so build_asyncpg_pool goes
    read-only + the reentrancy guards fire) and provides the single
    allowlisted RW credibility pool. build_pools=False is for unit
    tests that only need the contextvar semantics.

    Not reentrant — nesting ``async with LabContext`` inside an active
    LabContext resets _LAB_ACTIVE to False on the inner exit before the
    outer CM exits; the Lab is a single sweep-level CM by contract."""

    def __init__(self, *, db_url: str, build_pools: bool = True,
                 max_size: int = 2) -> None:
        self._db_url = db_url
        self._build_pools = build_pools
        self._max_size = max_size
        self._token: contextvars.Token | None = None
        self._cred_token: contextvars.Token | None = None
        self.read_pool: asyncpg.Pool | None = None
        self.credibility_pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> LabContext:
        try:
            if self._build_pools:
                from tpcore.db import build_asyncpg_pool
                # the ONE allowlisted RW handle — credibility append
                # only. Built BEFORE _LAB_ACTIVE is set: build_asyncpg_pool
                # forces default_transaction_read_only=on whenever
                # lab_is_active() is True, so building this after the
                # contextvar flip would silently make the "allowlisted RW
                # handle" read-only and any write through it would raise
                # ReadOnlySQLTransactionError server-side (the T2 thread
                # target — H-S3-8: the credibility write must stay the
                # single intentional RW exception, not be newly blocked).
                self.credibility_pool = await build_asyncpg_pool(
                    self._db_url, read_only=False, min_size=1, max_size=1)
            self._token = _LAB_ACTIVE.set(True)
            self._cred_token = _ACTIVE_CRED_POOL.set(self.credibility_pool)
            if self._build_pools:
                from tpcore.db import build_asyncpg_pool

                # read_only=True ⇒ read-only regardless of lab_is_active().
                self.read_pool = await build_asyncpg_pool(
                    self._db_url, read_only=True,
                    min_size=1, max_size=self._max_size)
        except Exception:
            if self.read_pool is not None:
                await self.read_pool.close()
            if self.credibility_pool is not None:
                await self.credibility_pool.close()
            if self._cred_token is not None:
                _ACTIVE_CRED_POOL.reset(self._cred_token)
                self._cred_token = None
            if self._token is not None:
                _LAB_ACTIVE.reset(self._token)
                self._token = None
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.read_pool is not None:
                await self.read_pool.close()
            if self.credibility_pool is not None:
                await self.credibility_pool.close()
        finally:
            if self._cred_token is not None:
                _ACTIVE_CRED_POOL.reset(self._cred_token)
                self._cred_token = None
            if self._token is not None:
                _LAB_ACTIVE.reset(self._token)
                self._token = None
