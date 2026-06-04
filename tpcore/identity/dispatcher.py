"""Ticker ↔ classification_id translation at system edges.

Engines and durable storage carry ``classification_id`` (the TKR-14 surrogate
on ``platform.ticker_classifications.id``). Tickers are mutable display
attributes — they get reassigned on renames, reused after delistings, and
changed by corporate actions. The dispatcher is the single place where
ticker→classification_id (wire-in) and classification_id→ticker (wire-out)
translation happens.

Inbound boundaries (caller has a ticker, needs a stable id):
    - CSV archive replay
    - Alpaca fill confirmation
    - Operator-typed ticker in a manual command

Outbound boundaries (caller has a stable id, needs the human-facing ticker):
    - Alpaca order submission (broker speaks ticker)
    - Dashboard / log emission (operator reads ticker)
    - AAR ``ticker_snapshot`` denormalization at write time

Backed by ``platform.ticker_history`` (SCD-2 per-security ticker timeline).
The hot scoring loop NEVER calls this module — once engines carry the
classification_id, no ticker translation runs per-bar.

Pattern mirrors ``tpcore.fundamentals.cache.FundamentalsCache``: the
dispatcher holds an ``asyncpg.Pool`` reference but does not own it;
caller manages pool lifecycle.

Cache discipline
----------------
TTL+LRU in-process cache fronts both lookups. Defaults: 300s TTL,
10_000 entries (≈2x typical active universe). Cache key includes the
``as_of`` parameter; latest-as-of and historical-as-of are cached
separately. The ``invalidate()`` hook clears either the whole cache or
all entries for a specific ticker / classification_id — call it after
``parent_resolver`` mints a new TKR-14 row or after a corp-action that
shifts a ticker to a new entity.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING, ClassVar

import structlog

if TYPE_CHECKING:
    from datetime import date

    import asyncpg

logger = structlog.get_logger(__name__)


_TICKER_TO_CID_LATEST = """
    SELECT classification_id
    FROM platform.ticker_history
    WHERE ticker = $1
      AND valid_to IS NULL
    ORDER BY valid_from DESC
    LIMIT 1
"""

_TICKER_TO_CID_AS_OF = """
    SELECT classification_id
    FROM platform.ticker_history
    WHERE ticker = $1
      AND valid_from <= $2
      AND (valid_to IS NULL OR $2 < valid_to)
    ORDER BY valid_from DESC
    LIMIT 1
"""

_CID_TO_TICKER_LATEST = """
    SELECT ticker
    FROM platform.ticker_history
    WHERE classification_id = $1
      AND valid_to IS NULL
    ORDER BY valid_from DESC
    LIMIT 1
"""

_CID_TO_TICKER_AS_OF = """
    SELECT ticker
    FROM platform.ticker_history
    WHERE classification_id = $1
      AND valid_from <= $2
      AND (valid_to IS NULL OR valid_to >= $2)
    ORDER BY valid_from DESC
    LIMIT 1
"""


class _TTLCache:
    """Tiny TTL + LRU cache. Kept in-module to avoid a new dependency.

    Insertion-orders keys for LRU eviction; per-entry expiry timestamp
    handles TTL. ``get`` returns ``None`` on miss or expiry, never raises.
    """

    __slots__ = ("_data", "_max_size", "_ttl_seconds")

    def __init__(self, max_size: int, ttl_seconds: float) -> None:
        self._data: OrderedDict[object, tuple[float, object]] = OrderedDict()
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds

    def get(self, key: object) -> object | _MissT:
        entry = self._data.get(key)
        if entry is None:
            return _MISS
        expires_at, value = entry
        if expires_at < time.monotonic():
            del self._data[key]
            return _MISS
        self._data.move_to_end(key)
        return value

    def put(self, key: object, value: object) -> None:
        expires_at = time.monotonic() + self._ttl_seconds
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (expires_at, value)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def invalidate(self, predicate=None) -> int:
        """Drop all entries (predicate=None) or those matching predicate.

        ``predicate`` receives the key and returns True if the entry
        should be evicted. Returns the number of entries dropped.
        """
        if predicate is None:
            n = len(self._data)
            self._data.clear()
            return n
        drop = [k for k in self._data if predicate(k)]
        for k in drop:
            del self._data[k]
        return len(drop)

    def __len__(self) -> int:
        return len(self._data)


class _MissT:
    """Sentinel distinct from None (cached None is a real value: 'no match')."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<MISS>"


_MISS = _MissT()


class IdentityDispatcher:
    """Ticker ↔ classification_id translator.

    Args:
        pool: ``asyncpg.Pool`` for ``platform.ticker_history`` lookups.
            Caller manages the pool's lifecycle.
        cache_max_size: LRU eviction trigger. Default 10_000 covers ~2x
            the typical active universe across both directions.
        cache_ttl_seconds: Entry expiry. Default 300s (5 min) matches
            the rate at which ``ticker_history`` realistically changes
            (the producer side runs on event ingestion, not per-second).

    Cache lifetime
    --------------
    The TTL+LRU cache is **shared across all IdentityDispatcher
    instances that hold the same pool**. Engine callsites that
    construct a fresh ``IdentityDispatcher(pool)`` per function call
    (the common pattern in this codebase) therefore benefit from cross-
    call caching — the second invocation in the same process sees a
    warm cache instead of starting empty. The cache keys on
    ``id(pool)``; multi-pool processes (rare) get isolated caches per
    pool, so a staging-vs-prod split can't cross-pollute.

    For tests that want a fresh cache (asserting on fetchval counts),
    pass a unique pool object per test — ``MagicMock()`` gives each
    test its own id and therefore its own cache slot. Or call
    ``IdentityDispatcher.reset_shared_caches()`` between tests.
    """

    # Pool id → (ticker_cache, cid_cache). Module-level lifetime; keyed
    # on id(pool) so each distinct pool gets its own caches.
    _shared_caches: ClassVar[dict[int, tuple[_TTLCache, _TTLCache]]] = {}

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        cache_max_size: int = 10_000,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._pool = pool
        cache_key = id(pool)
        caches = self._shared_caches.get(cache_key)
        if caches is None:
            caches = (
                _TTLCache(cache_max_size, cache_ttl_seconds),
                _TTLCache(cache_max_size, cache_ttl_seconds),
            )
            self._shared_caches[cache_key] = caches
        self._ticker_cache, self._cid_cache = caches

    @classmethod
    def reset_shared_caches(cls) -> None:
        """Drop all shared caches. Use between tests that assert on DB
        call counts; otherwise unnecessary."""
        cls._shared_caches.clear()

    async def ticker_to_classification_id(
        self,
        ticker: str,
        as_of: date | None = None,
    ) -> str | None:
        """Resolve a ticker to its TKR-14 classification_id.

        Args:
            ticker: the ticker symbol (case-preserved as stored; callers
                should pass it exactly as ``ticker_history`` records it,
                typically uppercase).
            as_of: the date at which to resolve. ``None`` returns the
                currently-active row (``valid_to IS NULL``); a date
                applies SCD-2 semantics (``valid_from <= as_of <=
                valid_to``). Backtests crossing renames pass the
                row-date here.

        Returns:
            The classification_id (str) or ``None`` if the ticker has
            no row matching the requested ``as_of``. ``None`` is cached
            — repeated lookups for an unknown ticker do not re-query.
        """
        cache_key = (ticker, as_of)
        hit = self._ticker_cache.get(cache_key)
        if hit is not _MISS:
            return hit  # type: ignore[return-value]
        if as_of is None:
            value = await self._fetchval(_TICKER_TO_CID_LATEST, ticker)
        else:
            value = await self._fetchval(_TICKER_TO_CID_AS_OF, ticker, as_of)
        self._ticker_cache.put(cache_key, value)
        if value is None:
            # Observability for "unknown ticker" — typos in universe
            # specs, recently-added tickers not yet in ticker_history,
            # delisted+pruned tickers. Logged once per (ticker, as_of)
            # because the None is now cached.
            logger.debug(
                "identity.dispatcher.ticker_unknown",
                ticker=ticker,
                as_of=str(as_of) if as_of is not None else None,
            )
        return value

    async def classification_id_to_ticker(
        self,
        classification_id: str,
        as_of: date | None = None,
    ) -> str | None:
        """Resolve a classification_id to its ticker at ``as_of``.

        Args:
            classification_id: the TKR-14 surrogate.
            as_of: ``None`` returns the currently-active ticker (the
                ``valid_to IS NULL`` row); a date applies SCD-2
                semantics. Pass the fill date when reconstructing AAR
                display ticker.

        Returns:
            The ticker (str) or ``None`` if no row matches. ``None``
            is cached.
        """
        cache_key = (classification_id, as_of)
        hit = self._cid_cache.get(cache_key)
        if hit is not _MISS:
            return hit  # type: ignore[return-value]
        if as_of is None:
            value = await self._fetchval(_CID_TO_TICKER_LATEST, classification_id)
        else:
            value = await self._fetchval(_CID_TO_TICKER_AS_OF, classification_id, as_of)
        self._cid_cache.put(cache_key, value)
        if value is None:
            logger.debug(
                "identity.dispatcher.cid_unknown",
                classification_id=classification_id,
                as_of=str(as_of) if as_of is not None else None,
            )
        return value

    def invalidate(
        self,
        *,
        ticker: str | None = None,
        classification_id: str | None = None,
    ) -> int:
        """Drop cache entries.

        Modes:
            - No args: drop the entire cache (both directions).
            - ``ticker=X``: drop every cached lookup keyed on ticker ``X``
              (in either direction).
            - ``classification_id=Y``: same, for cid ``Y``.

        Returns the total number of entries evicted. Call after
        ``parent_resolver`` mints a new TKR-14 row, after a corp-action
        that shifts a ticker, or on receipt of a ``ticker_history``
        change event.
        """
        if ticker is None and classification_id is None:
            n = self._ticker_cache.invalidate()
            n += self._cid_cache.invalidate()
            logger.info("identity.dispatcher.cache_cleared", entries=n)
            return n
        n = 0
        if ticker is not None:
            n += self._ticker_cache.invalidate(lambda k: k[0] == ticker)
            n += self._cid_cache.invalidate(lambda k: k[0] == ticker)
        if classification_id is not None:
            n += self._ticker_cache.invalidate(lambda k: k[0] == classification_id)
            n += self._cid_cache.invalidate(lambda k: k[0] == classification_id)
        return n

    async def _fetchval(self, sql: str, *args: object) -> str | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)


__all__ = ["IdentityDispatcher"]
