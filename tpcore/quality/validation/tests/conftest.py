"""Shared test fixtures for the validation suite.

Provides:
- a fake asyncpg pool (rows-as-dict store) for check tests
- temp YAML writers used by source/check/end-to-end tests
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool
# ────────────────────────────────────────────────────────────────────────────


class FakePool:
    """Tiny in-memory stand-in for asyncpg.Pool.

    Stores rows as a list of dicts; supports `fetch(sql, *args)` and
    `fetchrow(sql, *args)` by routing on substrings of the SQL text. This
    is intentionally narrow: it understands only the queries the validation
    checks emit, and adding a new query shape requires extending the
    routing here.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.calls: list[tuple[str, tuple]] = []

    # ------------------------------ context-manager plumbing
    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self)

    # ------------------------------ query routing (used by checks + writer)
    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        sql_lower = sql.lower()
        if "platform.prices_daily" in sql_lower and "ticker = any($1)" in sql_lower:
            tickers = set(args[0])
            return [r for r in self.rows if r["ticker"] in tickers]
        if "platform.prices_daily" in sql_lower and "ticker = $1" in sql_lower:
            ticker = args[0]
            return [r for r in self.rows if r["ticker"] == ticker]
        return []

    async def fetchrow(self, sql: str, *args) -> dict[str, Any] | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args) -> Any:
        """Scalar query. Used by the row_integrity check's COUNT(*).
        Returns 0 by default so existing tests, which don't care about
        integrity, get the "clean" signal automatically."""
        self.calls.append((sql, args))
        if "count(*)" in sql.lower() and "platform.prices_daily" in sql.lower():
            return 0
        return None


class _FakeAcquireCM:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakePool:
        return self._pool

    async def __aexit__(self, *exc) -> None:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Builders for synthetic prices_daily rows
# ────────────────────────────────────────────────────────────────────────────


def make_bar(
    ticker: str,
    bar_date: date,
    close: Decimal,
    *,
    delisted: bool = False,
    delisting_date: date | None = None,
) -> dict[str, Any]:
    """Build one row matching `platform.prices_daily` columns the checks read."""
    return {
        "ticker": ticker,
        "date": bar_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1_000_000,
        "adjusted_close": close,
        "delisted": delisted,
        "delisting_date": delisting_date,
    }


# ────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def write_yaml(tmp_path: Path):
    """Helper that writes a YAML payload to ``tmp_path/<name>``."""

    def _write(name: str, body: str) -> Path:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        return p

    return _write
