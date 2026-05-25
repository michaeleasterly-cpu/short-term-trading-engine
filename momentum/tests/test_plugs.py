"""Unit tests for the Momentum plugs (no DB / no broker)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from momentum.models import (
    MomentumCandidate,
    RebalanceAction,
    is_tradeable_common_stock,
)
from momentum.plugs.capital_gate import MomentumCapitalGate, MomentumGraduationStats
from momentum.plugs.execution_risk import MomentumExecutionRisk
from momentum.plugs.setup_detection import MomentumSetupDetection
from tpcore.risk.governor import CheckResult, RiskDecision

# ─── SetupDetection — score math ────────────────────────────────────────────


def test_setup_score_basic():
    plug = MomentumSetupDetection(lookback_days=20, skip_days=5)
    # 30 trading days, linear ramp 100 → 130 (price up 30% over 30 days).
    bars = [
        {"date": date(2024, 1, 1) + __import__("datetime").timedelta(days=i), "close": 100.0 + i}
        for i in range(40)
    ]
    score = plug._score_one(bars, as_of=date(2024, 2, 9))  # noqa: SLF001
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
        {"date": date(2024, 1, 1) + __import__("datetime").timedelta(days=i), "close": 100.0}
        for i in range(10)
    ]
    score = plug._score_one(bars, as_of=date(2024, 1, 10))  # noqa: SLF001
    assert score is None


# ─── ExecutionRisk — sizing + diff ──────────────────────────────────────────


def _candidate(ticker: str, score: float, price: float, tier: int = 1) -> MomentumCandidate:
    return MomentumCandidate(
        ticker=ticker,
        as_of=date(2024, 1, 2),
        momentum_score=score,
        last_close=Decimal(str(price)),
        tier=tier,
    )


def _mock_governor_allow_all() -> MagicMock:
    g = MagicMock()
    g.check_cost = AsyncMock(return_value=CheckResult(RiskDecision.ALLOW))
    return g


@pytest.mark.asyncio
async def test_execution_decision_open_new_targets():
    plug = MomentumExecutionRisk(
        governor=_mock_governor_allow_all(),
        top_decile_pct=0.2,
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
        governor=_mock_governor_allow_all(),
        top_decile_pct=0.5,
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
        governor=_mock_governor_allow_all(),
        top_decile_pct=1.0,
        max_tier=2,
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
        n_rebalances=6,
        sharpe_annualized=1.5,
        profit_factor=2.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is True
    # Sharpe too low:
    stats = MomentumGraduationStats(
        n_rebalances=6,
        sharpe_annualized=0.8,
        profit_factor=2.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is False
    # Not enough rebalances:
    stats = MomentumGraduationStats(
        n_rebalances=3,
        sharpe_annualized=2.0,
        profit_factor=3.0,
    )
    assert MomentumCapitalGate.is_graduated(stats) is False


# ─── Tradability filter ──────────────────────────────────────────────────────


def test_filter_rejects_warrants_5_chars_or_more():
    # Real warrants observed in the 2026-05-13 smoke output.
    assert not is_tradeable_common_stock("XBPEW", Decimal("10.00"))
    assert not is_tradeable_common_stock("BBLGW", Decimal("16.70"))
    assert not is_tradeable_common_stock("NAMSW", Decimal("9.37"))


def test_filter_accepts_3_char_W_endings():
    # Common stocks ending in W where len < 5 → should pass.
    assert is_tradeable_common_stock("CDW", Decimal("250.00"))
    assert is_tradeable_common_stock("ZWS", Decimal("50.00"))


def test_filter_rejects_sub_5_dollar_stocks():
    assert not is_tradeable_common_stock("AAPL", Decimal("4.99"))
    assert not is_tradeable_common_stock("XYZ", Decimal("0.06"))
    assert is_tradeable_common_stock("AAPL", Decimal("5.00"))


def test_filter_rejects_separator_tickers():
    # Preferreds (BRK.B-style), units (XYZ.U), rights (ABC=R)
    assert not is_tradeable_common_stock("BRK.B", Decimal("400.00"))
    assert not is_tradeable_common_stock("BAC-A", Decimal("25.00"))
    assert not is_tradeable_common_stock("XYZ.U", Decimal("10.00"))


def test_filter_accepts_normal_common_stocks():
    assert is_tradeable_common_stock("AAPL", Decimal("180.00"))
    assert is_tradeable_common_stock("MSFT", Decimal("400.00"))
    assert is_tradeable_common_stock("SPY", Decimal("550.00"))
    assert is_tradeable_common_stock("AGMI", Decimal("75.22"))  # 4-char, no separator, doesn't end in W


def test_capital_gate_healthcheck_includes_thresholds():
    gate = MomentumCapitalGate(engine_equity_usd=Decimal("5000"))
    hc = gate.healthcheck()
    assert hc["ok"] is True
    assert hc["plug"] == "capital_gate"
    assert hc["details"]["grad_min_rebalances"] == 6


# ─── Drawdown circuit breaker (Phase 2.5 #3) ─────────────────────────────────


def test_drawdown_breaker_allows_at_peak():
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("10000"),
            peak_equity=Decimal("10000"),
        )
        is True
    )


def test_drawdown_breaker_allows_within_threshold():
    # 5% below peak → no breaker at default 10% threshold.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("9500"),
            peak_equity=Decimal("10000"),
        )
        is True
    )


def test_drawdown_breaker_trips_at_threshold():
    # Exactly 10% below peak → breaker trips.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("9000"),
            peak_equity=Decimal("10000"),
        )
        is False
    )


def test_drawdown_breaker_trips_below_threshold():
    # 15% below peak → breaker trips.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("8500"),
            peak_equity=Decimal("10000"),
        )
        is False
    )


def test_drawdown_breaker_allows_when_peak_unknown():
    # First run, no peak history yet → allow.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("10000"),
            peak_equity=None,
        )
        is True
    )
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=None,
            peak_equity=Decimal("10000"),
        )
        is True
    )


def test_drawdown_breaker_allows_with_zero_peak():
    # Degenerate input — don't crash, don't trip.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("100"),
            peak_equity=Decimal("0"),
        )
        is True
    )


def test_drawdown_breaker_respects_custom_threshold():
    # 6% below peak, threshold raised to 5% → trips.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("9400"),
            peak_equity=Decimal("10000"),
            threshold=Decimal("0.05"),
        )
        is False
    )
    # Same drawdown, threshold relaxed to 15% → allows.
    assert (
        MomentumCapitalGate.check_drawdown(
            current_equity=Decimal("9400"),
            peak_equity=Decimal("10000"),
            threshold=Decimal("0.15"),
        )
        is True
    )


# ─── SetupDetection — universe loader (candidates table vs fallback) ────────


class _RecordingConn:
    """Tiny pool stand-in. Hands out canned ``fetch`` results based on whether
    the SQL hits ``universe_candidates``, ``liquidity_tiers``, or ``v_universe``.

    PR-16: the live-path universe fallback now reads via UniverseRepo
    (platform.v_universe). ``liquidity_rows`` is reshaped on the fly
    into v_universe row shape for the fallback path so existing test
    fixtures don't need rewriting.
    """

    def __init__(self, candidates_rows: list[dict], liquidity_rows: list[dict]) -> None:
        self.candidates_rows = candidates_rows
        self.liquidity_rows = liquidity_rows
        self.sql_seen: list[str] = []

    async def fetch(self, sql: str, *args):
        self.sql_seen.append(sql)
        if "universe_candidates" in sql:
            return self.candidates_rows
        if "v_universe" in sql:
            # Reshape liquidity_rows into v_universe row shape for the
            # UniverseRepo fallback path. Each {ticker: T} becomes the
            # minimum UniverseRow dict the repo's model_validate accepts.
            from datetime import date as _date

            return [
                {
                    "classification_id": f"CID_{r['ticker']}",
                    "ticker_at_date": r["ticker"],
                    "current_ticker": r["ticker"],
                    "asset_class": "stock",
                    "country": "US",
                    "status": "active",
                    "liquidity_tier": 1,
                    "valid_from": _date(2020, 1, 1),
                    "valid_to": None,
                }
                for r in self.liquidity_rows
            ]
        if "liquidity_tiers" in sql:
            return self.liquidity_rows
        return []


class _AcquireCM:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _RecordingConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, candidates_rows: list[dict], liquidity_rows: list[dict]) -> None:
        self.conn = _RecordingConn(candidates_rows, liquidity_rows)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


@pytest.mark.asyncio
async def test_load_universe_prefers_candidates_table_when_present():
    pool = _FakePool(
        candidates_rows=[{"ticker": "AAPL"}, {"ticker": "MSFT"}],
        liquidity_rows=[{"ticker": "OLD"}],
    )
    plug = MomentumSetupDetection()
    universe = await plug._load_universe(pool, date(2026, 5, 13))  # noqa: SLF001
    assert universe == {"AAPL", "MSFT"}
    # Only the candidates query should have been issued — fallback never ran.
    assert any("universe_candidates" in s for s in pool.conn.sql_seen)
    assert not any("liquidity_tiers" in s for s in pool.conn.sql_seen)


@pytest.mark.asyncio
async def test_load_universe_falls_back_to_liquidity_tiers_when_empty():
    pool = _FakePool(
        candidates_rows=[],
        liquidity_rows=[{"ticker": "AAPL"}, {"ticker": "MSFT"}],
    )
    plug = MomentumSetupDetection()
    universe = await plug._load_universe(pool, date(2026, 5, 13))  # noqa: SLF001
    assert universe == {"AAPL", "MSFT"}
    assert any("universe_candidates" in s for s in pool.conn.sql_seen)
    # PR-16: fallback now reads v_universe via UniverseRepo (not direct
    # liquidity_tiers query).
    assert any("v_universe" in s for s in pool.conn.sql_seen)
