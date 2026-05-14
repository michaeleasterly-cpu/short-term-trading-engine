"""Tests for AllocatorService rebalance gating (audit items 44 + 45).

Covers the four-branch decision tree:

    drift <  SOFT_BAND                       → SKIP (drift_below_threshold)
    SOFT  ≤ drift < HARD AND transitional    → SKIP (regime_transitional)
    SOFT  ≤ drift < HARD AND not transitional → REBALANCE (soft_band)
    drift ≥ HARD_BAND                        → REBALANCE (hard_band_override)

Plus: frozen engines always persist regardless of drift gate; the
ALLOCATOR_REBALANCED / ALLOCATOR_SKIPPED events land in application_log.

All tests use FakePool stand-ins — no live DB required.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tpcore.allocator.service import (
    HARD_BAND_DRIFT_PCT,
    SOFT_BAND_DRIFT_PCT,
    AllocationDecision,
    AllocatorService,
)

# ── Fake pool that records writes + serves a configurable read state ──


class _FakeConn:
    def __init__(self, fake_pool: _FakePool) -> None:
        self._p = fake_pool
        self._in_tx = False

    def transaction(self):
        outer = self

        class _TxCM:
            async def __aenter__(self_inner):
                outer._in_tx = True
                return self_inner

            async def __aexit__(self_inner, *_):
                outer._in_tx = False
                return None

        return _TxCM()

    async def execute(self, sql: str, *args) -> None:
        self._p.executes.append((sql, args))

    async def fetchval(self, sql: str, *args) -> Any:
        # Tests configure prior_weights via FakePool.prior_weights;
        # the fetchval that looks up prior weights is the SELECT from
        # platform.allocations.
        sql_lower = sql.lower()
        if "platform.allocations" in sql_lower and "weight" in sql_lower:
            engine = args[0]
            return self._p.prior_weights.get(engine)
        return None

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        sql_lower = sql.lower()
        if "ticker = 'spy'" in sql_lower or "ticker='spy'" in sql_lower:
            return self._p.spy_bars
        return []


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(
        self,
        *,
        prior_weights: dict[str, Decimal] | None = None,
        spy_bars: list[dict[str, Any]] | None = None,
    ) -> None:
        self.prior_weights = prior_weights or {}
        self.spy_bars = spy_bars or []
        self.executes: list[tuple] = []
        self.application_log_writes: list[tuple] = []
        self.conn = _FakeConn(self)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _spy_bars_for_regime(regime: str) -> list[dict[str, Any]]:
    """Return SPY bars that produce CHOP in the target regime band.

    * trending     → tight intraday ranges on a rising series → CHOP < 38.2
    * transitional → moderate ranges                          → 38.2 ≤ CHOP ≤ 61.8
    * choppy       → wide oscillating ranges                  → CHOP > 61.8
    """
    import numpy as np
    n = 30
    if regime == "trending":
        base = np.linspace(100.0, 150.0, n)
        highs = base + 0.3
        lows = base - 0.3
    elif regime == "choppy":
        base = np.tile([100.0, 102.0], n // 2)
        highs = base + 1.5
        lows = base - 1.5
    else:  # transitional
        base = np.linspace(100.0, 110.0, n)
        highs = base + 1.0
        lows = base - 1.0
    return [
        {
            "date": date(2026, 1, 1),  # value doesn't matter for chop calc
            "high": float(h),
            "low": float(l),
            "close": float(b),
        }
        for h, l, b in zip(highs, lows, base, strict=True)
    ]


def _decision(engine: str, weight: Decimal, *, freeze_state: str = "active") -> AllocationDecision:
    return AllocationDecision(
        engine=engine,
        weight=weight,
        allocated_capital=(weight * Decimal("40000")).quantize(Decimal("0.01")),
        prior_equity=Decimal("10000"),
        realized_vol=None,
        freeze_state=freeze_state,
        freeze_reason=None,
        drawdown_pct=Decimal("0"),
    )


def _make_service(pool: _FakePool) -> AllocatorService:
    return AllocatorService(
        pool=pool,
        engines=("sigma", "reversion", "vector", "momentum"),
        platform_capital=Decimal("40000"),
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        as_of=date(2026, 5, 14),
    )


def _alloc_rows_persisted(pool: _FakePool) -> list[tuple]:
    """Return only the INSERT INTO platform.allocations writes."""
    return [c for c in pool.executes if "INSERT INTO platform.allocations" in c[0]]


def _app_log_writes(pool: _FakePool) -> list[tuple]:
    return [c for c in pool.executes if "INSERT INTO platform.application_log" in c[0]]


# ── Test 1: skip when drift below soft band ───────────────────────────


@pytest.mark.asyncio
async def test_skip_when_drift_below_soft_band():
    """All active engines within 25% of prior → no allocations persist."""
    pool = _FakePool(
        prior_weights={
            "sigma":     Decimal("0.25"),
            "reversion": Decimal("0.25"),
            "vector":    Decimal("0.25"),
            "momentum":  Decimal("0.25"),
        },
        spy_bars=_spy_bars_for_regime("trending"),  # regime irrelevant when drift small
    )
    svc = _make_service(pool)
    decisions = [
        _decision("sigma",     Decimal("0.26")),  # +4% drift
        _decision("reversion", Decimal("0.24")),  # -4% drift
        _decision("vector",    Decimal("0.27")),  # +8% drift
        _decision("momentum",  Decimal("0.23")),  # -8% drift  — all < 25%
    ]
    max_drift, _ = await svc._compute_drift(decisions)
    assert max_drift < SOFT_BAND_DRIFT_PCT
    # Persist with active_skip=True (matches what run_once would do)
    await svc._persist(decisions, active_skip=True)
    assert len(_alloc_rows_persisted(pool)) == 0


# ── Test 2: skip when transitional regime AND moderate drift ──────────


@pytest.mark.asyncio
async def test_skip_when_transitional_and_moderate_drift():
    """30% drift but CHOP transitional → skip (regime_transitional)."""
    pool = _FakePool(
        prior_weights={
            "sigma":     Decimal("0.25"),
            "reversion": Decimal("0.25"),
            "vector":    Decimal("0.25"),
            "momentum":  Decimal("0.25"),
        },
        spy_bars=_spy_bars_for_regime("transitional"),
    )
    svc = _make_service(pool)
    regime, _ = await svc._fetch_market_regime()
    assert regime == "transitional"
    skip, rebal = AllocatorService._classify_rebalance(Decimal("0.30"), regime)
    assert skip == "regime_transitional"
    assert rebal is None


# ── Test 3: rebalance when moderate drift AND favorable regime ────────


@pytest.mark.asyncio
async def test_rebalance_when_moderate_drift_and_favorable_regime():
    """30% drift + CHOP trending → rebalance (soft_band)."""
    pool = _FakePool(spy_bars=_spy_bars_for_regime("trending"))
    svc = _make_service(pool)
    regime, _ = await svc._fetch_market_regime()
    assert regime == "trending"
    skip, rebal = AllocatorService._classify_rebalance(Decimal("0.30"), regime)
    assert skip is None
    assert rebal == "soft_band"
    # Same outcome under choppy regime
    pool_choppy = _FakePool(spy_bars=_spy_bars_for_regime("choppy"))
    svc_choppy = _make_service(pool_choppy)
    regime2, _ = await svc_choppy._fetch_market_regime()
    assert regime2 == "choppy"
    skip2, rebal2 = AllocatorService._classify_rebalance(Decimal("0.30"), regime2)
    assert skip2 is None
    assert rebal2 == "soft_band"


# ── Test 4: force rebalance when drift ≥ hard band ────────────────────


@pytest.mark.asyncio
async def test_force_rebalance_when_drift_above_hard_band():
    """60% drift → rebalance regardless of regime (hard_band_override)."""
    for regime_name in ("trending", "transitional", "choppy"):
        pool = _FakePool(spy_bars=_spy_bars_for_regime(regime_name))
        svc = _make_service(pool)
        regime, _ = await svc._fetch_market_regime()
        skip, rebal = AllocatorService._classify_rebalance(Decimal("0.60"), regime)
        assert skip is None, f"skip should be None for hard-band, got {skip!r} in {regime_name}"
        assert rebal == "hard_band_override", f"got {rebal!r} in {regime_name}"
        assert Decimal("0.60") >= HARD_BAND_DRIFT_PCT


# ── Test 5: frozen engine update always persists ──────────────────────


@pytest.mark.asyncio
async def test_frozen_engine_always_persisted():
    """active_skip=True still writes frozen-engine rows."""
    pool = _FakePool(spy_bars=_spy_bars_for_regime("trending"))
    svc = _make_service(pool)
    decisions = [
        _decision("sigma",     Decimal("0.50")),  # active — would be skipped
        _decision("reversion", Decimal("0.50")),  # active — would be skipped
        _decision("vector",    Decimal("0"), freeze_state="soft_frozen"),
        _decision("momentum",  Decimal("0"), freeze_state="hard_frozen"),
    ]
    await svc._persist(decisions, active_skip=True)
    alloc_writes = _alloc_rows_persisted(pool)
    # Exactly 2 writes: vector + momentum (the frozen ones)
    assert len(alloc_writes) == 2
    # Confirm the right engines persisted
    engines_persisted = {call[1][0] for call in alloc_writes}  # first arg is engine
    assert engines_persisted == {"vector", "momentum"}


# ── Test 6: first run with no prior allocations → force rebalance ─────


@pytest.mark.asyncio
async def test_first_run_no_prior_allocations():
    """No prior row for any engine → drift = 1.0 → above HARD_BAND →
    forces rebalance regardless of regime."""
    pool = _FakePool(
        prior_weights={},  # empty — no prior rows
        spy_bars=_spy_bars_for_regime("transitional"),  # even transitional is overridden
    )
    svc = _make_service(pool)
    decisions = [
        _decision("sigma",     Decimal("0.25")),
        _decision("reversion", Decimal("0.25")),
        _decision("vector",    Decimal("0.25")),
        _decision("momentum",  Decimal("0.25")),
    ]
    max_drift, per_engine = await svc._compute_drift(decisions)
    assert max_drift == Decimal("1")
    assert all(v == Decimal("1") for v in per_engine.values())
    # Classification → hard_band_override
    regime, _ = await svc._fetch_market_regime()
    skip, rebal = AllocatorService._classify_rebalance(max_drift, regime)
    assert skip is None
    assert rebal == "hard_band_override"


# ── Test 7: log events written to application_log ─────────────────────


@pytest.mark.asyncio
async def test_log_events_written_skip():
    """run_once with low drift → ALLOCATOR_SKIPPED event written."""
    pool = _FakePool(
        prior_weights={
            "sigma":     Decimal("0.25"),
            "reversion": Decimal("0.25"),
            "vector":    Decimal("0.25"),
            "momentum":  Decimal("0.25"),
        },
        spy_bars=_spy_bars_for_regime("trending"),
    )
    svc = _make_service(pool)
    # Stub _load_histories so run_once doesn't need an AAR pool
    async def _empty_histories():
        from tpcore.allocator.service import _EngineHistory
        return [
            _EngineHistory(
                engine=e, aar_count=0, daily_pnls=[], equity_curve=[],
                peak_equity=10_000.0, current_equity=10_000.0,
                soft_frozen_sessions=0,
            )
            for e in ("sigma", "reversion", "vector", "momentum")
        ]
    svc._load_histories = _empty_histories  # type: ignore[method-assign]
    await svc.run_once()
    log_writes = _app_log_writes(pool)
    assert len(log_writes) == 1
    sql, args = log_writes[0]
    # Args from DBLogHandler INSERT: (engine, run_id, event_type, severity, message, data)
    assert args[0] == "allocator"
    assert args[2] == "ALLOCATOR_SKIPPED"
    assert args[3] == "INFO"
    payload = json.loads(args[5])
    assert payload["reason"] == "drift_below_threshold"
    assert payload["regime"] == "trending"


@pytest.mark.asyncio
async def test_log_events_written_rebalance():
    """run_once with no prior → ALLOCATOR_REBALANCED event written."""
    pool = _FakePool(
        prior_weights={},
        spy_bars=_spy_bars_for_regime("trending"),
    )
    svc = _make_service(pool)
    async def _empty_histories():
        from tpcore.allocator.service import _EngineHistory
        return [
            _EngineHistory(
                engine=e, aar_count=0, daily_pnls=[], equity_curve=[],
                peak_equity=10_000.0, current_equity=10_000.0,
                soft_frozen_sessions=0,
            )
            for e in ("sigma", "reversion", "vector", "momentum")
        ]
    svc._load_histories = _empty_histories  # type: ignore[method-assign]
    await svc.run_once()
    log_writes = _app_log_writes(pool)
    assert len(log_writes) == 1
    sql, args = log_writes[0]
    assert args[2] == "ALLOCATOR_REBALANCED"
    payload = json.loads(args[5])
    assert payload["reason"] == "hard_band_override"
    assert "new_weights" in payload
