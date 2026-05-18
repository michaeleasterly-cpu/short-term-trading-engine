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

    server_settings: dict[str, str] = {}
    if read_only or lab_is_active():
        server_settings["default_transaction_read_only"] = "on"
    kwargs: dict = dict(
        dsn=normalize_database_url(database_url),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
    )
    if server_settings:
        kwargs["server_settings"] = server_settings
    return await asyncpg.create_pool(**kwargs)


__all__ = ["build_asyncpg_pool", "normalize_database_url"]
