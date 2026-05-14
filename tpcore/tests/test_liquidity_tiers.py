"""Tests for ``scripts.assign_liquidity_tiers``.

Per the 5-stage pipeline (L-1 follow-up, 2026-05-14), every adapter
ships with tests covering: success, empty data, edge cases, and
idempotency. The live ``assign_tiers`` orchestrator requires a real
DB; the deterministic core (``_tier_for``) is exhaustively covered
here, and the SQL paths are exercised against a fake pool.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.assign_liquidity_tiers import (
    DEFAULT_TIER,
    MIN_OBSERVATIONS_FOR_STABLE,
    TIER_BOUNDS,
    _tier_for,
)

# ── 1. Tier boundary math ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "spread,expected_tier",
    [
        (Decimal("0.00001"), 1),   # 1 bp — well inside T1
        (Decimal("0.0004"), 1),    # just under T1 ceiling
        (Decimal("0.0005"), 2),    # at T1 ceiling → T2
        (Decimal("0.0014"), 2),    # just under T2 ceiling
        (Decimal("0.0015"), 3),    # at T2 ceiling → T3
        (Decimal("0.0049"), 3),    # just under T3 ceiling
        (Decimal("0.0050"), 4),    # at T3 ceiling → T4
        (Decimal("0.0199"), 4),    # just under T4 ceiling
        (Decimal("0.0200"), 5),    # at T4 ceiling → T5
        (Decimal("0.50"), 5),      # very wide → T5
    ],
)
def test_tier_for_boundary_cases(spread, expected_tier):
    assert _tier_for(spread) == expected_tier


def test_tier_bounds_strictly_increasing():
    for i in range(len(TIER_BOUNDS) - 1):
        assert TIER_BOUNDS[i] < TIER_BOUNDS[i + 1]


def test_default_tier_is_t4():
    assert DEFAULT_TIER == 4


def test_min_observations_threshold_positive():
    assert MIN_OBSERVATIONS_FOR_STABLE > 0


# ── 2. assign_tiers — empty observations ───────────────────────────────


class _FakeConn:
    def __init__(self, fetch_rows: list[dict] | None = None) -> None:
        self._fetch_rows = fetch_rows or []
        self.executemany_calls: list[tuple] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        return list(self._fetch_rows)

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, rows))


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self._conn)

    async def close(self) -> None:
        return None


async def test_assign_tiers_empty_observations_returns_empty_dict(monkeypatch):
    """When spread_observations has nothing for the requested source,
    assign_tiers must return an empty bucket and skip the upsert. The
    operator's dashboard probe surfaces this as zero-coverage."""
    from scripts import assign_liquidity_tiers

    conn = _FakeConn(fetch_rows=[])
    pool = _FakePool(conn)

    async def _fake_build(*args, **kwargs):
        return pool

    monkeypatch.setattr(
        assign_liquidity_tiers, "build_asyncpg_pool", _fake_build
    )
    bucket = await assign_liquidity_tiers.assign_tiers(
        db_url="postgresql://noop", sources=["corwin_schultz"],
    )
    assert bucket == {}
    assert conn.executemany_calls == []  # nothing to upsert


# ── 3. assign_tiers — happy path & distribution ────────────────────────


async def test_assign_tiers_produces_correct_distribution(monkeypatch):
    """Three tickers across three tier bands → bucket reflects the mix
    and an upsert is issued."""
    from scripts import assign_liquidity_tiers

    rows = [
        {"ticker": "AAPL", "median_spread_pct": Decimal("0.0001"),
         "p95_spread_pct": Decimal("0.0010"), "observations": 100},  # T1
        {"ticker": "PLTR", "median_spread_pct": Decimal("0.0010"),
         "p95_spread_pct": Decimal("0.0030"), "observations": 50},   # T2
        {"ticker": "RIVN", "median_spread_pct": Decimal("0.0030"),
         "p95_spread_pct": Decimal("0.0100"), "observations": 20},   # T3
    ]
    conn = _FakeConn(fetch_rows=rows)
    pool = _FakePool(conn)

    async def _fake_build(*args, **kwargs):
        return pool

    monkeypatch.setattr(
        assign_liquidity_tiers, "build_asyncpg_pool", _fake_build
    )
    bucket = await assign_liquidity_tiers.assign_tiers(
        db_url="postgresql://noop", sources=["corwin_schultz"],
    )
    assert bucket == {1: 1, 2: 1, 3: 1}
    # Single executemany call with all three rows.
    assert len(conn.executemany_calls) == 1
    _, upsert_rows = conn.executemany_calls[0]
    assert len(upsert_rows) == 3
    # provisional flag: row 1 has 100 obs → not provisional;
    # row 3 has 20 obs (above 5) → also not provisional.
    aapl_row = next(r for r in upsert_rows if r[0] == "AAPL")
    assert aapl_row[5] is False  # provisional column


# ── 4. assign_tiers — provisional flag honored at observation threshold ─


async def test_assign_tiers_marks_provisional_when_under_min_obs(monkeypatch):
    """A ticker with fewer than MIN_OBSERVATIONS_FOR_STABLE pooled
    observations gets ``provisional=True`` — Corwin-Schultz needs
    enough samples to stabilize."""
    from scripts import assign_liquidity_tiers

    rows = [
        {"ticker": "FRESH", "median_spread_pct": Decimal("0.0003"),
         "p95_spread_pct": Decimal("0.0020"),
         "observations": MIN_OBSERVATIONS_FOR_STABLE - 1},
    ]
    conn = _FakeConn(fetch_rows=rows)
    pool = _FakePool(conn)

    async def _fake_build(*args, **kwargs):
        return pool

    monkeypatch.setattr(
        assign_liquidity_tiers, "build_asyncpg_pool", _fake_build
    )
    await assign_liquidity_tiers.assign_tiers(
        db_url="postgresql://noop", sources=["corwin_schultz"],
    )
    upsert_row = conn.executemany_calls[0][1][0]
    assert upsert_row[5] is True  # provisional


# ── 5. Idempotency — second call with the same rows produces the same
#       bucket and the same upsert payload ────────────────────────────


async def test_assign_tiers_idempotent_same_inputs_same_outputs(monkeypatch):
    """The SQL is ``ON CONFLICT (ticker) DO UPDATE`` and the function
    is deterministic given identical input observations — second run
    matches the first exactly."""
    from scripts import assign_liquidity_tiers

    rows = [
        {"ticker": "MSFT", "median_spread_pct": Decimal("0.0002"),
         "p95_spread_pct": Decimal("0.0015"), "observations": 80},
    ]
    conn = _FakeConn(fetch_rows=rows)
    pool = _FakePool(conn)

    async def _fake_build(*args, **kwargs):
        return pool

    monkeypatch.setattr(
        assign_liquidity_tiers, "build_asyncpg_pool", _fake_build
    )
    bucket1 = await assign_liquidity_tiers.assign_tiers(
        db_url="postgresql://noop", sources=["corwin_schultz"],
    )
    bucket2 = await assign_liquidity_tiers.assign_tiers(
        db_url="postgresql://noop", sources=["corwin_schultz"],
    )
    assert bucket1 == bucket2 == {1: 1}
    # Two upsert calls (one per run) with byte-identical payloads.
    assert len(conn.executemany_calls) == 2
    assert conn.executemany_calls[0][1] == conn.executemany_calls[1][1]
