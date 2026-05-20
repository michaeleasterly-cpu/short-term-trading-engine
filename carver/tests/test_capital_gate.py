"""carver/plugs/capital_gate.py — engine-local + graduation rubric composition."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest


def test_subclasses_base_engine_plug_and_has_healthcheck() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate
    from tpcore.interfaces.engine_plug import BaseEnginePlug

    gate = CarverCapitalGate(engine_equity_usd=Decimal("10000"))
    assert isinstance(gate, BaseEnginePlug)
    assert gate.validate_dependencies() is True
    hc = gate.healthcheck()
    assert hc["engine"] == "carver"
    assert hc["plug"] == "capital_gate"


def test_check_rebalance_rejects_oversize() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate

    gate = CarverCapitalGate(engine_equity_usd=Decimal("10000"))
    # oversize -> False
    assert gate.check_rebalance(Decimal("20000")) is False
    # zero/negative -> False
    assert gate.check_rebalance(Decimal("0")) is False
    # within equity -> True
    assert gate.check_rebalance(Decimal("9999")) is True


def test_check_drawdown_trips_below_threshold() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate

    # current 80, peak 100 -> 20% drawdown >= 10% (default threshold).
    assert CarverCapitalGate.check_drawdown(Decimal("80"), Decimal("100")) is False


def test_check_drawdown_returns_true_with_no_peak() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate

    assert CarverCapitalGate.check_drawdown(Decimal("100"), None) is True
    assert CarverCapitalGate.check_drawdown(None, Decimal("100")) is True
    assert CarverCapitalGate.check_drawdown(Decimal("100"), Decimal("0")) is True


def test_is_graduated_thresholds() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate, CarverGraduationStats

    s = CarverGraduationStats(n_trades=0, win_rate=0.0, avg_return=0.0)
    assert CarverCapitalGate.is_graduated(s) is False
    s2 = CarverGraduationStats(n_trades=50, win_rate=0.60, avg_return=0.05)
    assert CarverCapitalGate.is_graduated(s2) is True


def test_assert_can_graduate_returns_false_when_stats_below_threshold() -> None:
    from carver.plugs.capital_gate import CarverCapitalGate, CarverGraduationStats

    # Stats below threshold => returns False without touching the pool.
    s = CarverGraduationStats(n_trades=1, win_rate=0.0, avg_return=0.0)
    result = asyncio.run(
        CarverCapitalGate.assert_can_graduate(stats=s, pool=None)
    )
    assert result is False


def test_assert_can_graduate_raises_without_credibility_row() -> None:
    """When stats clear but no credibility row, expect CredibilityScoreInsufficientError."""
    import pytest

    from carver.plugs.capital_gate import CarverCapitalGate, CarverGraduationStats
    from tpcore.backtest.credibility import CredibilityScoreInsufficientError

    class _StubConn:
        async def fetchrow(self, sql: str, *args: Any) -> dict | None:
            del sql, args
            return None

        async def fetchval(self, sql: str, *args: Any) -> Any:
            del sql, args
            return None

        async def fetch(self, sql: str, *args: Any) -> list:
            del sql, args
            return []

    class _StubAcquireCtx:
        def __init__(self) -> None:
            self._conn = _StubConn()

        async def __aenter__(self) -> _StubConn:
            return self._conn

        async def __aexit__(self, exc_t, exc_v, tb) -> None:
            return None

    class _StubPool:
        def acquire(self) -> _StubAcquireCtx:
            return _StubAcquireCtx()

    # Patch the names carver.plugs.capital_gate imported at module level.
    import carver.plugs.capital_gate as _cg

    async def _ok_assert(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def _no_grad(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return False

    orig_assert_passed = _cg.assert_passed
    orig_grad_ready = _cg.graduation_ready
    _cg.assert_passed = _ok_assert
    _cg.graduation_ready = _no_grad
    try:
        s = CarverGraduationStats(n_trades=50, win_rate=0.60, avg_return=0.05)
        with pytest.raises(CredibilityScoreInsufficientError):
            asyncio.run(
                CarverCapitalGate.assert_can_graduate(stats=s, pool=_StubPool())
            )
    finally:
        _cg.assert_passed = orig_assert_passed
        _cg.graduation_ready = orig_grad_ready


def test_check_trade_oversize_blocks() -> None:
    """Per-trade check_trade respects PRE_GRAD_POSITION_CAP_USD."""
    from carver.models import PRE_GRAD_POSITION_CAP_USD
    from carver.plugs.capital_gate import CarverCapitalGate

    gate = CarverCapitalGate(engine_equity_usd=Decimal("100000"))
    too_big = PRE_GRAD_POSITION_CAP_USD + Decimal("1")
    assert gate.check_trade(size=too_big, engine_pnl=Decimal("0")) is False
    assert gate.check_trade(size=Decimal("100"), engine_pnl=Decimal("0")) is True


def test_check_trade_daily_loss_freeze() -> None:
    """A 5%+ engine_pnl drawdown trips the daily-loss freeze."""
    from carver.plugs.capital_gate import CarverCapitalGate

    gate = CarverCapitalGate(engine_equity_usd=Decimal("10000"))
    # -6% on the day -> blocked.
    assert gate.check_trade(size=Decimal("100"), engine_pnl=Decimal("-600")) is False


def test_check_trade_position_count_cap() -> None:
    """At MAX_CONCURRENT_POSITIONS, no more opens."""
    from carver.models import MAX_CONCURRENT_POSITIONS
    from carver.plugs.capital_gate import CarverCapitalGate

    gate = CarverCapitalGate(engine_equity_usd=Decimal("100000"))
    assert gate.check_trade(
        size=Decimal("100"),
        engine_pnl=Decimal("0"),
        open_positions=MAX_CONCURRENT_POSITIONS,
    ) is False
    assert gate.check_trade(
        size=Decimal("100"),
        engine_pnl=Decimal("0"),
        open_positions=MAX_CONCURRENT_POSITIONS - 1,
    ) is True


def test_constants_exposed_for_scheduler() -> None:
    from carver.plugs.capital_gate import (
        DAILY_LOSS_FREEZE_PCT,
        DRAWDOWN_BREAKER_LOOKBACK_DAYS,
    )

    assert DAILY_LOSS_FREEZE_PCT == Decimal("0.05")
    assert DRAWDOWN_BREAKER_LOOKBACK_DAYS == 365


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
