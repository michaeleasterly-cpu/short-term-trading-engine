import asyncpg
import pytest

from tpcore.db import build_asyncpg_pool


@pytest.mark.skipif(__import__("os").environ.get("DATABASE_URL") is None,
                    reason="needs a DB")
async def test_read_only_pool_rejects_writes():
    import os
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"],
                                    read_only=True, max_size=1)
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")  # reads OK
            with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
                await conn.execute(
                    "CREATE TEMP TABLE _lab_probe(x int); "
                    "INSERT INTO _lab_probe VALUES (1)")
    finally:
        await pool.close()


def test_build_asyncpg_pool_has_read_only_kwarg():
    import inspect
    sig = inspect.signature(build_asyncpg_pool)
    assert "read_only" in sig.parameters
    assert sig.parameters["read_only"].default is False
