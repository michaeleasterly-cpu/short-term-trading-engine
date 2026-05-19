"""Focused regression test for ``ops/cron_fundamentals_refresh.py`` (#250).

The legacy Railway cron unpacked ``cache.backfill_all(tickers=None)`` as
a 3-tuple, but ``FundamentalsCache.backfill_all`` returns the 4-tuple
``(rows, no_data, failures, skipped)`` since the 2026-05-13
resumable-refresh change — a dead-on-arrival ``ValueError``.

``_amain`` builds a pool + adapter from env, so the seams
(``build_asyncpg_pool``, ``FMPFundamentalsAdapter``,
``FundamentalsCache``) are monkeypatched at their import sites in the
cron module. A faked cache returns the real 4-tuple. The assertion is
the minimal one the task calls for: ``_amain`` now unpacks 4 and exits
0 with NO ``ValueError`` (no DB, no network).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# NOTE: ``scripts/ops.py`` is a single-file module that some tests import
# as the top-level name ``ops`` after putting ``scripts/`` on sys.path,
# which permanently shadows the ``ops/`` package for the pytest session
# (``ops`` is not a package — no __init__.py). So
# ``import ops.cron_fundamentals_refresh`` is collection-order fragile.
# Load the module by file path under a private, collision-free name —
# robust regardless of which test ran first (mirrors
# tests/test_data_repair_service.py).
_CRON_PATH = (
    Path(__file__).resolve().parent.parent
    / "ops" / "cron_fundamentals_refresh.py"
)
_spec = importlib.util.spec_from_file_location("_cron_fund_under_test", _CRON_PATH)
assert _spec is not None and _spec.loader is not None
cron = importlib.util.module_from_spec(_spec)
sys.modules["_cron_fund_under_test"] = cron
_spec.loader.exec_module(cron)


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakePool:
    async def close(self) -> None:
        return None


class _FakeAdapter:
    async def aclose(self) -> None:
        return None


class _FakeCache:
    """``backfill_all`` returns the real 4-tuple contract."""

    def __init__(self, pool, adapter=None) -> None:
        self.pool = pool
        self.adapter = adapter

    async def backfill_all(self, *a, **k):
        # rows, no_data list, failures list, skipped count.
        return (3, [], [], 1)


@pytest.fixture()
def _seams(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    async def _fake_pool(_url):
        return _FakePool()

    monkeypatch.setattr(cron, "build_asyncpg_pool", _fake_pool)
    monkeypatch.setattr(cron, "FMPFundamentalsAdapter", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(cron, "FundamentalsCache", _FakeCache)


async def test_amain_unpacks_4tuple_and_exits_zero(_seams):
    # Pre-fix this raised ValueError ("too many values to unpack")
    # before returning. Now it must unpack 4 and exit 0 (no failures).
    rc = await cron._amain()  # noqa: SLF001
    assert rc == 0
