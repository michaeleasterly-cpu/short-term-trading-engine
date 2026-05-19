"""asyncpg statement-cache / JIT pooler-safety regression.

Against a Supabase Supavisor transaction-mode pooler (``:6543``), asyncpg's
auto-prepared statements do not survive across pooled backends, producing
intermittent ``prepared statement "__asyncpg_*__" does not exist`` errors.
The fix is to disable the statement cache (and server-side JIT, which can
also misbehave through poolers). These assertions bite if a future edit
drops ``statement_cache_size=0`` or stops merging ``jit: off``.
"""
import asyncpg
import pytest

from tpcore.db import build_asyncpg_pool


class _SpyPool:
    async def close(self) -> None:  # pragma: no cover - never called here
        pass


@pytest.fixture()
def spy_create_pool(monkeypatch):
    captured: dict = {}

    async def _fake_create_pool(**kwargs):
        captured.update(kwargs)
        return _SpyPool()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool)
    return captured


async def test_statement_cache_disabled(spy_create_pool):
    await build_asyncpg_pool("postgresql+asyncpg://u:p@h/d?ssl=require")
    assert spy_create_pool["statement_cache_size"] == 0


async def test_jit_off_in_server_settings(spy_create_pool):
    await build_asyncpg_pool("postgresql+asyncpg://u:p@h/d?ssl=require")
    assert spy_create_pool["server_settings"]["jit"] == "off"


async def test_read_only_server_settings_merged_not_clobbered(spy_create_pool):
    """``read_only=True`` adds its setting WITHOUT dropping the jit key."""
    await build_asyncpg_pool(
        "postgresql+asyncpg://u:p@h/d?ssl=require", read_only=True
    )
    ss = spy_create_pool["server_settings"]
    assert ss["jit"] == "off"
    assert ss["default_transaction_read_only"] == "on"


async def test_other_kwargs_preserved(spy_create_pool):
    await build_asyncpg_pool(
        "postgresql+asyncpg://u:p@h/d?ssl=require",
        min_size=2,
        max_size=7,
        timeout=15.0,
    )
    assert spy_create_pool["min_size"] == 2
    assert spy_create_pool["max_size"] == 7
    assert spy_create_pool["timeout"] == 15.0
    assert spy_create_pool["dsn"] == "postgresql://u:p@h/d?sslmode=require"
