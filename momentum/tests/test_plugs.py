"""Unit tests for the Momentum plugs (no DB / no broker)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from momentum.models import MomentumCandidate, RebalanceAction
from momentum.plugs.capital_gate import MomentumCapitalGate, MomentumGraduationStats
from momentum.plugs.execution_risk import MomentumExecutionRisk
from momentum.plugs.setup_detection import MomentumSetupDetection
from tpcore.risk.governor import CheckResult, RiskDecision


# ─── SetupDetection — score math ────────────────────────────────────────────


def test_setup_score_basic():
    plug = MomentumSetupDetection(lookback_days=20, skip_days=5)
    # 30 trading days, linear ramp 100 → 130 (price up 30% over 30 days).
    bars = [
        {"date": date(2024, 1, 1) + __import__("datetime").timedelta(days=i),
         "close": 100.0 + i}
        for i in range(40)
    ]
    score = plug._score_one(bars, as_of=date(2024, 2, 9))
    # We use the LAST bar's date as the actual reference, not the calendar
    # as_of. Last bar's date = 2024-01-01 + 39d = 2024-02-09. Step back 5d
    # calendar → ~2024-02-04 (close ~ 134). Step back another 20d → 2024-01-15
    # (close ~ 114). Score = 134/114 - 1 ≈ 0.175. Allow ±5% tolerance for
    # exact bar selection.
    assert score is not None
    assert 0.10 < score < 0.30


def test_setup_score_missing_data_returns_none():
    plug = MomentumSetupDetection(lookback_days=20, skip_days=5)
    # Only 10 bars — not enough for a 25-bar window.
    bars = [
        {"date": date(2024, 1, 1) + __import__("datetime").timedelta(days=i),
         "close": 100.0}
        for i in range(10)
    ]
    score = plug._score_one(bars, as_of=date(2024, 1, 10))
    assert score is None


# ─── ExecutionRisk — sizing + diff ──────────────────────────────────────────


def _candidate(ticker: str, score: float, price: float, tier: int = 1) -> MomentumCandidate:
    return MomentumCandidate(
        ticker=ticker, as_of=date(2024, 1, 2),
        momentum_score=score, last_close=Decimal(str(price)), tier=tier,
    )


def _mock_governor_allow_all() -> MagicMock:
    g = MagicMock()
    g.check_cost = AsyncMock(return_value=CheckResult(RiskDecision.ALLOW))
    return g


@pytest.mark.asyncio
async def test_execution_decision_open_new_targets():
    plug = MomentumExecutionRisk(
        governor=_mock_governor_allow_all(), top_decile_pct=0.2,
    )
    candidates = [
        _candidate("A", 0.5, 100.0),
        _candidate("B", 0.4, 50.0),
        _candidate("C", 0.3, 200.0),
        _candidate("D", 0.2, 75.0),
        _candidate("E", 0.1, 150.0),
    ]
    # top_decile_pct=0.2 of 5 candidates = 1 target → 'A'.
    decision = await plug.build_decision(
        candidates=candidates,
        equity_usd=Decimal("10000"),
        current_holdings={},
        as_of=date(2024, 1, 2),
    )
    assert len(decision.targets) == 1
    assert decision.targets[0].ticker == "A"
    assert decision.n_open == 1
    assert decision.n_close == 0
    assert all(o.action is RebalanceAction.OPEN for o in decision.orders)


@pytest.mark.asyncio
async def test_execution_decision_closes_dropped_names():
    plug = MomentumExecutionRisk(
        governor=_mock_governor_allow_all(), top_decile_pct=0.5,
    )
    # New target set: A, B
    candidates = [_candidate("A", 0.5, 100.0), _candidate("B", 0.4, 50.0)]
    # Current holdings: A (kept), C (dropped → CLOSE)
    decision = await plug.build_decision(
        candidates=candidates,
        equity_usd=Decimal("10000"),
        current_holdings={"A": 0, "C": 10},
        as_of=date(2024, 1, 2),
    )
    actions = {o.ticker: o.action for o in decision.orders}
    assert actions.get("C") is RebalanceAction.CLOSE
    assert decision.n_close >= 1


@pytest.mark.asyncio
async def test_execution_decision_skips_high_tier():
    plug = MomentumExecutionRisk(
        governor=_mock_governor_allow_all(), top_decile_pct=1.0, max_tier=2,
    )
    candidates = [
        _candidate("LIQUID", 0.5, 100.0, tier=1),
        _candidate("ILLIQUID", 0.6, 50.0, tier=4),  # higher score but tier=4
    ]
    decision = await plug.build_decision(
        candidates=candidates,
        equity_usd=Decimal("10000"),
        current_holdings={},
        as_of=date(2024, 1, 2),
    )
    target_tickers = {t.ticker for t in decision.targets}
    assert "LIQUID" in target_tickers
    assert "ILLIQUID" not in target_tickers


# ─── CapitalGate ────────────────────────────────────────────────────────────


def test_capital_gate_rejects_oversize():
    gate = MomentumCapitalGate(engine_equity_usd=Decimal("1000"))
    assert gate.check_rebalance(Decimal("999")) is True
    assert gate.check_rebalance(Decimal("1500")) is False
    assert gate.check_rebalance(Decimal("0")) is False


def test_capital_gate_graduation_logic():
    stats = MomentumGraduationStats(
        n_rebalances=6, sharpe_annualized=1.5, profit_factor=2.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is True
    # Sharpe too low:
    stats = MomentumGraduationStats(
        n_rebalances=6, sharpe_annualized=0.8, profit_factor=2.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is False
    # Not enough rebalances:
    stats = MomentumGraduationStats(
        n_rebalances=3, sharpe_annualized=2.0, profit_factor=3.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is False


def test_capital_gate_healthcheck_includes_thresholds():
    gate = MomentumCapitalGate(engine_equity_usd=Decimal("5000"))
    hc = gate.healthcheck()
    assert hc["ok"] is True
    assert hc["plug"] == "capital_gate"
    assert hc["details"]["grad_min_rebalances"] == 6
