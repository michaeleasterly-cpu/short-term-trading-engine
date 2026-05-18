from __future__ import annotations

import contextvars
from typing import Any

_LAB_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_LAB_ACTIVE", default=False)


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
            "(SDLC SP2 isolation contract)")


class LabContext:
    """Async CM: marks the Lab active (so build_asyncpg_pool goes
    read-only + the reentrancy guards fire) and provides the single
    allowlisted RW credibility pool. build_pools=False is for unit
    tests that only need the contextvar semantics."""

    def __init__(self, *, db_url: str, build_pools: bool = True,
                 max_size: int = 2) -> None:
        self._db_url = db_url
        self._build_pools = build_pools
        self._max_size = max_size
        self._token: contextvars.Token | None = None
        self.read_pool: Any | None = None
        self.credibility_pool: Any | None = None

    async def __aenter__(self) -> LabContext:
        self._token = _LAB_ACTIVE.set(True)
        if self._build_pools:
            from tpcore.db import build_asyncpg_pool
            self.read_pool = await build_asyncpg_pool(
                self._db_url, read_only=True,
                min_size=1, max_size=self._max_size)
            # the ONE allowlisted RW handle — credibility append only.
            self.credibility_pool = await build_asyncpg_pool(
                self._db_url, read_only=False, min_size=1, max_size=1)
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.read_pool is not None:
                await self.read_pool.close()
            if self.credibility_pool is not None:
                await self.credibility_pool.close()
        finally:
            if self._token is not None:
                _LAB_ACTIVE.reset(self._token)
