"""Async database helpers shared by all engines.

Today this module is a single function: ``build_asyncpg_pool``. It accepts
the same ``DATABASE_URL`` that Alembic + SQLAlchemy use
(``postgresql+asyncpg://user:pass@host/db?ssl=require``) and returns an
``asyncpg.Pool`` ready for query work.

asyncpg doesn't accept SQLAlchemy's driver suffix or libpq-foreign query
keys, so we strip the ``+asyncpg`` and translate the SQLAlchemy-style
``ssl=require`` into the libpq ``sslmode=require`` that asyncpg understands.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


def normalize_database_url(url: str) -> str:
    """Convert a SQLAlchemy-style URL into one ``asyncpg`` accepts.

    Examples
    --------
    >>> normalize_database_url("postgresql+asyncpg://u:p@h/d?ssl=require")
    'postgresql://u:p@h/d?sslmode=require'
    >>> normalize_database_url("postgres://u:p@h/d")
    'postgres://u:p@h/d'
    """
    parts = urlsplit(url)
    scheme = parts.scheme.replace("+asyncpg", "")
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        # SQLAlchemy speaks ``ssl=require``; asyncpg / libpq expect ``sslmode``.
        if key == "ssl":
            query_pairs.append(("sslmode", value))
        else:
            query_pairs.append((key, value))
    new_query = urlencode(query_pairs)
    return urlunsplit((scheme, parts.netloc, parts.path, new_query, parts.fragment))


async def build_asyncpg_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 4,
    timeout: float = 10.0,
    read_only: bool = False,
) -> asyncpg.Pool:
    """Create an ``asyncpg.Pool`` from a SQLAlchemy-style ``DATABASE_URL``.

    The Sigma scheduler is a one-shot cron service: a small pool is plenty
    and avoids holding extra connections against Supabase's pooled limits.
    Always close the pool with ``await pool.close()`` before exit.

    ``read_only=True`` (or an active ``tpcore.lab`` ``_LAB_ACTIVE`` context)
    builds a pool whose every connection runs with
    ``default_transaction_read_only=on`` — any write raises asyncpg
    ``ReadOnlySQLTransactionError`` server-side (the Lab isolation floor,
    SDLC SP2 H-S2-2).
    """
    import asyncpg

    # Local, import-error-guarded import: ``tpcore.lab.context`` is created
    # in SDLC SP2 T3 and may not be merged when this builder lands. The guard
    # makes T2 independently landable and keeps ``tpcore/db.py`` import-light
    # / cycle-free — deliberate documented resilience, not a placeholder.
    try:
        from tpcore.lab.context import lab_is_active
    except ImportError:
        def lab_is_active() -> bool:
            return False

    # ``statement_cache_size=0`` + ``jit: off``: the canonical local DSN is the
    # Supabase Supavisor transaction-mode pooler (``:6543``), across which
    # asyncpg's auto-prepared statements do not survive pooled backends
    # ("prepared statement "__asyncpg_*__" does not exist"); server-side JIT
    # can likewise misbehave through poolers. Both are unconditionally safe:
    # on a direct connection a 0 cache only forgoes a micro-optimization. The
    # ``jit`` key seeds ``server_settings`` so the read-only / Lab branch below
    # MERGES into it (does not clobber it) — official asyncpg / Supabase guidance.
    server_settings: dict[str, str] = {"jit": "off"}
    if read_only or lab_is_active():
        server_settings["default_transaction_read_only"] = "on"
        # Lab on-demand experimental queries (e.g. ops/lab/run.py's per-
        # window + final-holdout panel-load over ~50 mega-caps × ~7 years
        # = ~87k rows in a single SELECT) routinely exceed Supabase's
        # default statement_timeout. vector_composite probe 5 (2026-05-20)
        # died with "canceling statement due to statement timeout" on the
        # final-holdout. Raising it here ONLY for read-only / Lab paths
        # preserves the safety stop on the live order-submit path (which
        # uses the default statement_timeout — a runaway query there is a
        # real hazard). 30min = generous-but-finite — a stuck Lab query
        # still fails loud after half an hour, not silently forever.
        server_settings["statement_timeout"] = "30min"
    kwargs: dict = dict(
        dsn=normalize_database_url(database_url),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        statement_cache_size=0,
        server_settings=server_settings,
    )
    return await asyncpg.create_pool(**kwargs)


__all__ = ["build_asyncpg_pool", "normalize_database_url"]
