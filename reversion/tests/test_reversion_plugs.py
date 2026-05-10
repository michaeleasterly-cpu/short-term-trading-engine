"""End-to-end tests for the five Reversion plugs (Phase 2 acceptance).

Pattern follows ``sigma/tests/test_sigma_plugs.py``: synthetic bar
fixtures driving each plug in isolation, plus an end-to-end pipeline
that runs scan → assess → decide → gate → AAR.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.interfaces.data import Bar, DataProviderInterface

from reversion import (
    Direction,
    ExecutionDecision,
    GraduationStats,
    Phase,
    PhaseAssessment,
    ReversionAARLogging,
    ReversionCapitalGate,
    ReversionExecutionRisk,
    ReversionLifecycleAnalysis,
    ReversionSetupDetection,
    SetupCandidate,
)


# ────────────────────────────────────────────────────────────────────────────
# Synthetic bar generators
# ────────────────────────────────────────────────────────────────────────────


def _bar(symbol: str, day: date, o: float, h: float, l: float, c: float, v: int) -> Bar:
    ts = datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC)
    return Bar(
        symbol=symbol, ts=ts,
        open=Decimal(str(o)), high=Decimal(str(h)),
        low=Decimal(str(l)), close=Decimal(str(c)),
        volume=v,
    )


def _stable_then_extreme_low(symbol: str, end: date, n: int = 70, base: float = 100.0) -> list[Bar]:
    """Tight oscillation, then a single hammer bar at the extreme.

    Triggers oversold (|Z| ≥ 3.0 — the production threshold) plus
    reversal-candle plus volume-spike scoring. The drop happens entirely
    inside one bar (gap down + long lower wick + small body near top) so
    the 20-day window stays mostly stable, keeping std small and z deeply
    negative.
    """
    bars: list[Bar] = []
    day = end - timedelta(days=n + 10)
    for i in range(n - 1):
        c = base + (0.4 if i % 2 else -0.4)
        o = c - 0.1
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        bars.append(_bar(symbol, day, o, h, l, c, 5_000_000))
        day += timedelta(days=1)
    # Hammer at the extreme — gap down ~25 from previous close, long lower
    # wick, small body near top of bar.
    o = base - 25.0
    l = base - 30.0
    h = base - 24.5
    close = base - 25.0  # body = 0
    bars.append(_bar(symbol, day, o, h, l, close, 18_000_000))
    return bars


def _stable_then_extreme_high(symbol: str, end: date, n: int = 70, base: float = 100.0) -> list[Bar]:
    """Tight oscillation, then a single shooting-star at the extreme.

    Symmetric to ``_stable_then_extreme_low`` — designed so the |Z| of
    the final close clears the production threshold (3.0)."""
    bars: list[Bar] = []
    day = end - timedelta(days=n + 10)
    for i in range(n - 1):
        c = base + (0.4 if i % 2 else -0.4)
        o = c - 0.1
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        bars.append(_bar(symbol, day, o, h, l, c, 5_000_000))
        day += timedelta(days=1)
    # Shooting star at the extreme — gap up ~25, long upper wick, small
    # body near bottom of bar.
    o = base + 25.0
    h = base + 30.0
    l = base + 24.5
    close = base + 25.0  # body = 0
    bars.append(_bar(symbol, day, o, h, l, close, 18_000_000))
    return bars


def _flat_series(symbol: str, end: date, n: int = 70, base: float = 100.0) -> list[Bar]:
    """No statistical extreme — should NOT qualify."""
    bars: list[Bar] = []
    day = end - timedelta(days=n + 10)
    for i in range(n):
        c = base + (0.2 if i % 2 else -0.2)
        bars.append(_bar(symbol, day, c - 0.05, c + 0.1, c - 0.1, c, 5_000_000))
        day += timedelta(days=1)
    return bars


class MockDataProvider(DataProviderInterface):
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_symbol

    async def get_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [b for b in self._bars.get(symbol, []) if start <= b.ts.date() <= end]

    async def get_quote(self, symbol: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_fundamentals(self, symbol: str, as_of: date | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_earnings_calendar(self, symbol: str, start: date, end: date) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def list_active_symbols(self) -> list[str]:  # pragma: no cover
        return list(self._bars.keys())

    async def list_delisted_symbols(self) -> list[tuple[str, date]]:  # pragma: no cover
        return []


@pytest.fixture
def as_of() -> date:
    return date(2026, 5, 1)


@pytest.fixture
def mock_provider(as_of: date) -> MockDataProvider:
    return MockDataProvider(
        {
            "AAPL": _stable_then_extreme_low("AAPL", as_of, base=100.0),
            "TSLA": _stable_then_extreme_high("TSLA", as_of, base=200.0),
            "WMT": _flat_series("WMT", as_of, base=80.0),
            # SPY is fetched by setup_detection for sector-z and VIX proxy.
            # Make it slightly stretched in both directions across the test
            # universe so the sector-match component fires somewhere.
            "SPY": _stable_then_extreme_low("SPY", as_of, base=500.0),
        }
    )


# ────────────────────────────────────────────────────────────────────────────
# Setup detection
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_detection_finds_oversold_long(
    mock_provider: MockDataProvider, as_of: date
) -> None:
    plug = ReversionSetupDetection(data=mock_provider, universe=("AAPL", "WMT", "TSLA"))
    assert plug.validate_dependencies()
    assert plug.healthcheck()["ok"]
    candidates = await plug.scan(as_of=as_of)

    # Oversold AAPL should qualify with direction LONG.
    longs = [c for c in candidates if c.direction is Direction.LONG]
    assert any(c.ticker == "AAPL" for c in longs), f"got: {[c.ticker for c in candidates]}"
    aapl = next(c for c in longs if c.ticker == "AAPL")
    assert aapl.z_score < -1.5
    assert aapl.statistical_extremity > 0
    assert aapl.exhaustion_confirmation > 0
    # Flat series shouldn't pass.
    assert not any(c.ticker == "WMT" for c in candidates)


@pytest.mark.asyncio
async def test_setup_detection_finds_overbought_short(
    mock_provider: MockDataProvider, as_of: date
) -> None:
    plug = ReversionSetupDetection(data=mock_provider, universe=("AAPL", "WMT", "TSLA"))
    candidates = await plug.scan(as_of=as_of)
    shorts = [c for c in candidates if c.direction is Direction.SHORT]
    assert any(c.ticker == "TSLA" for c in shorts)
    tsla = next(c for c in shorts if c.ticker == "TSLA")
    assert tsla.z_score > 1.5


# ────────────────────────────────────────────────────────────────────────────
# Lifecycle analysis
# ────────────────────────────────────────────────────────────────────────────


def _high_grade_fundamentals() -> dict:
    """Fixture for a HIGH-grade earnings-quality result (clean fundamentals)."""
    return {
        "symbol": "AAPL",
        "net_income": Decimal("100"),
        "fcf": Decimal("95"),  # fcf/ni = 0.95 ≥ 0.90 → HIGH track
        "total_assets": Decimal("1000"),  # accruals = 0.005 → HIGH track
        "revenue": Decimal("500"),
        "receivables": Decimal("50"),
        "capex": Decimal("-20"),
        # 4-entry history so the rev-rec YoY check has data to compare.
        # Receivables and revenue growing in lockstep → rev-rec ratio ~ 1.0 → no flag.
        "history": [
            {"revenue": Decimal("490"), "receivables": Decimal("49"),
             "fcf": Decimal("90"), "capex": Decimal("-19")},
            {"revenue": Decimal("480"), "receivables": Decimal("48"),
             "fcf": Decimal("88"), "capex": Decimal("-18")},
            {"revenue": Decimal("470"), "receivables": Decimal("47"),
             "fcf": Decimal("86"), "capex": Decimal("-17")},
            {"revenue": Decimal("450"), "receivables": Decimal("45"),
             "fcf": Decimal("82"), "capex": Decimal("-16")},
        ],
    }


def _low_grade_fundamentals() -> dict:
    """Fixture forcing LOW grade — fcf/ni well below 0.6."""
    return {
        "symbol": "AAPL",
        "net_income": Decimal("100"),
        "fcf": Decimal("40"),  # fcf/ni = 0.40 < 0.60 → LOW
        "total_assets": Decimal("1000"),
        "revenue": Decimal("500"),
        "receivables": Decimal("50"),
        "capex": Decimal("-20"),
        "history": [],
    }


def _candidate(direction: Direction = Direction.LONG, adx: float = 15.0) -> SetupCandidate:
    return SetupCandidate(
        ticker="AAPL",
        as_of=date(2026, 5, 1),
        direction=direction,
        reversion_score=80.0,
        statistical_extremity=40.0,
        exhaustion_confirmation=25.0,
        market_context=15.0,
        z_score=-2.5 if direction is Direction.LONG else 2.5,
        rsi_14=20.0 if direction is Direction.LONG else 80.0,
        bb_breach_consecutive_days=2,
        volume_ratio=2.5,
        adx_14=adx,
        has_reversal_candle=True,
        has_rsi_divergence=True,
        suggested_entry_price=Decimal("100.00"),
        target_20ma=Decimal("105.00") if direction is Direction.LONG else Decimal("95.00"),
        target_50ma=Decimal("108.00") if direction is Direction.LONG else Decimal("92.00"),
    )


def test_lifecycle_assess_long_sets_stop_below_entry() -> None:
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(Direction.LONG), fundamentals=_high_grade_fundamentals())
    assert assessment.phase is Phase.ACTIVE
    # Stop = entry × (1 − 0.08) = 92.00.
    assert assessment.stop_price == Decimal("92.00")
    assert assessment.target_20ma == Decimal("105.00")
    assert assessment.earnings_quality_blocked is False


def test_lifecycle_assess_short_sets_stop_above_entry() -> None:
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(Direction.SHORT), fundamentals=_high_grade_fundamentals())
    assert assessment.phase is Phase.ACTIVE
    # Stop = entry × (1 + 0.08) = 108.00.
    assert assessment.stop_price == Decimal("108.00")
    assert assessment.target_20ma == Decimal("95.00")


def test_lifecycle_disables_engine_above_adx_25() -> None:
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(adx=27.0), fundamentals=_high_grade_fundamentals())
    assert assessment.phase is Phase.EXHAUSTED


def test_lifecycle_blocks_when_fundamentals_missing() -> None:
    """No fundamentals → no trade — per the §4.2 gate behavior."""
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(Direction.LONG), fundamentals=None)
    assert assessment.phase is Phase.EXHAUSTED
    assert assessment.earnings_quality_blocked is True


def test_lifecycle_blocks_when_earnings_quality_low() -> None:
    """LOW-grade fundamentals → trade suppressed."""
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(
        _candidate(Direction.LONG), fundamentals=_low_grade_fundamentals()
    )
    assert assessment.phase is Phase.EXHAUSTED
    assert assessment.earnings_quality_blocked is True
    assert "eq=low" in (assessment.notes or "")


def _medium_grade_fundamentals() -> dict:
    """fcf/ni in [0.6, 0.9) and accruals in [0.05, 0.10) — MEDIUM grade."""
    return {
        "net_income": Decimal("100"),
        "fcf": Decimal("75"),  # fcf/ni = 0.75 → in MEDIUM band
        "total_assets": Decimal("1000"),  # accruals will land in MEDIUM band
        "history": [
            {"net_income": Decimal("90"), "fcf": Decimal("65"), "total_assets": Decimal("970")},
            {"net_income": Decimal("80"), "fcf": Decimal("60"), "total_assets": Decimal("950")},
        ],
    }


def test_lifecycle_blocks_when_earnings_quality_medium() -> None:
    """MEDIUM-grade fundamentals → blocked. After the 2018–2025 backtest
    showed only HIGH was profitable, the gate tightened from 'reject LOW'
    to 'require HIGH'."""
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(
        _candidate(Direction.LONG), fundamentals=_medium_grade_fundamentals()
    )
    assert assessment.phase is Phase.EXHAUSTED
    assert assessment.earnings_quality_blocked is True
    assert "eq=medium" in (assessment.notes or "")


def test_lifecycle_handle_tier1_fill_transitions_to_reverting() -> None:
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(Direction.LONG), fundamentals=_high_grade_fundamentals())
    after = plug.handle_tier1_fill(assessment, position_remaining=2)
    assert after.tier1_filled is True
    assert after.remaining_shares == 2
    assert after.phase is Phase.REVERTING
    # Full close → EXHAUSTED.
    closed = plug.handle_tier1_fill(assessment, position_remaining=0)
    assert closed.phase is Phase.EXHAUSTED


def test_lifecycle_advance_bar_fires_time_stop_after_5_days() -> None:
    plug = ReversionLifecycleAnalysis()
    assessment = plug.assess(_candidate(Direction.LONG), fundamentals=_high_grade_fundamentals())
    a = assessment
    for _ in range(5):
        a = plug.advance_bar(a, touched_20ma=False)
    assert a.bars_held == 5
    assert a.phase is Phase.EXHAUSTED


def test_lifecycle_advance_bar_resets_counter_on_touch() -> None:
    plug = ReversionLifecycleAnalysis()
    a = plug.assess(_candidate(Direction.LONG), fundamentals=_high_grade_fundamentals())
    a = plug.advance_bar(a, touched_20ma=False)
    a = plug.advance_bar(a, touched_20ma=False)
    a = plug.advance_bar(a, touched_20ma=True)  # reset
    assert a.bars_held == 0
    assert a.phase is Phase.ACTIVE


# ────────────────────────────────────────────────────────────────────────────
# Execution risk
# ────────────────────────────────────────────────────────────────────────────


def test_execution_risk_builds_two_tier_payloads_long() -> None:
    plug = ReversionExecutionRisk()
    assessment = PhaseAssessment(
        ticker="AAPL", as_of=date(2026, 5, 1),
        direction=Direction.LONG, phase=Phase.ACTIVE,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("92.00"),
        target_20ma=Decimal("105.00"),
        target_50ma=Decimal("108.00"),
    )
    decision = plug.decide(assessment, account_capital=Decimal("10000"))
    assert decision is not None
    assert decision.qty >= 4
    assert decision.tier1_qty + decision.tier2_qty == decision.qty
    # 75/25 split — tier 1 dominates.
    assert decision.tier1_qty >= decision.tier2_qty * 2

    tier1, tier2 = decision.order_payloads
    assert tier1["side"] == "buy" and tier2["side"] == "sell"
    assert tier1["order_class"] == "bracket"
    assert tier1["take_profit"] == {"limit_price": "105.00"}
    assert tier1["stop_loss"] == {"stop_price": "92.00"}
    assert tier2["type"] == "limit" and tier2["time_in_force"] == "gtc"
    assert tier2["limit_price"] == "108.00"


def test_execution_risk_short_blocked_by_default() -> None:
    """Scheduler defaults to LONG-only; SHORT should not generate a payload
    unless the caller opts in."""
    plug = ReversionExecutionRisk()
    assessment = PhaseAssessment(
        ticker="TSLA", as_of=date(2026, 5, 1),
        direction=Direction.SHORT, phase=Phase.ACTIVE,
        entry_price=Decimal("200.00"),
        stop_price=Decimal("216.00"),
        target_20ma=Decimal("190.00"),
        target_50ma=Decimal("184.00"),
    )
    assert plug.decide(assessment, account_capital=Decimal("10000")) is None
    decision = plug.decide(
        assessment, account_capital=Decimal("10000"), allow_shorts=True
    )
    assert decision is not None
    tier1, tier2 = decision.order_payloads
    assert tier1["side"] == "sell" and tier2["side"] == "buy"


def test_execution_risk_respects_earnings_quality_block() -> None:
    plug = ReversionExecutionRisk()
    assessment = PhaseAssessment(
        ticker="AAPL", as_of=date(2026, 5, 1),
        direction=Direction.LONG, phase=Phase.ACTIVE,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("92.00"),
        target_20ma=Decimal("105.00"),
        target_50ma=Decimal("108.00"),
        earnings_quality_blocked=True,
    )
    assert plug.decide(assessment, account_capital=Decimal("10000")) is None


def test_execution_risk_position_cap() -> None:
    plug = ReversionExecutionRisk()
    assessment = PhaseAssessment(
        ticker="AAPL", as_of=date(2026, 5, 1),
        direction=Direction.LONG, phase=Phase.ACTIVE,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("92.00"),
        target_20ma=Decimal("105.00"),
        target_50ma=Decimal("108.00"),
    )
    assert plug.decide(assessment, account_capital=Decimal("10000"), open_positions=5) is None


# ────────────────────────────────────────────────────────────────────────────
# Capital gate
# ────────────────────────────────────────────────────────────────────────────


def test_capital_gate_enforces_limits() -> None:
    gate = ReversionCapitalGate(engine_equity=Decimal("10000"))
    assert gate.check_trade(size=Decimal("2000"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("2001"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("0"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=5)
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("-600"))  # −6% drawdown

    # Graduation gating per §4.2 (10 trades / 55% / 2% / PF 1.5).
    # Insufficient trade count → not graduated even if other metrics are stellar.
    assert not ReversionCapitalGate.is_graduated(
        GraduationStats(n_trades=5, win_rate=0.7, avg_return=0.03, profit_factor=3.0)
    )
    # Insufficient profit factor → not graduated even with enough trades and acceptable returns.
    assert not ReversionCapitalGate.is_graduated(
        GraduationStats(n_trades=20, win_rate=0.6, avg_return=0.02, profit_factor=1.2)
    )
    # All four bars met → graduated. Numbers calibrated to the backtest result
    # (54.5% WR, +2.08% avg, PF 3.69) plus a sample-size buffer.
    assert ReversionCapitalGate.is_graduated(
        GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5)
    )


# ────────────────────────────────────────────────────────────────────────────
# AAR logging
# ────────────────────────────────────────────────────────────────────────────


def test_aar_logging_builds_tier1_and_tier2() -> None:
    plug = ReversionAARLogging()
    common = dict(
        trade_id="reversion-2026-05-01-AAPL-001",
        ticker="AAPL",
        entry_ts=datetime(2026, 5, 1, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 4, 19, 55, tzinfo=UTC),
        entry_price=Decimal("100.00"),
        confidence_at_entry=Decimal("0.75"),
        sizing_pct_of_engine_equity=Decimal("0.20"),
        rule_compliance=True,
    )
    tier1 = plug.build_tier1_aar(**common, exit_price=Decimal("105.00"), tier1_qty=Decimal("15"))
    assert tier1.exit_reason is ExitReason.TIER1_MID_BAND
    assert tier1.qty == Decimal("15")
    assert tier1.pnl_gross == Decimal("75.00")  # (105-100) × 15

    tier2 = plug.build_tier2_aar(
        **common,
        tier1_exit_price=Decimal("105.00"),
        tier2_exit_price=Decimal("108.00"),
        tier1_qty=Decimal("15"),
        tier2_qty=Decimal("5"),
    )
    assert tier2.exit_reason is ExitReason.TIER2_OPPOSITE_BAND
    assert tier2.qty == Decimal("20")
    # 15 × 5 + 5 × 8 = 75 + 40 = 115
    assert tier2.pnl_gross == Decimal("115.00")


# ────────────────────────────────────────────────────────────────────────────
# End-to-end pipeline
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_end_to_end(mock_provider: MockDataProvider, as_of: date) -> None:
    detect = ReversionSetupDetection(data=mock_provider, universe=("AAPL", "WMT", "TSLA"))
    lifecycle = ReversionLifecycleAnalysis()
    execute = ReversionExecutionRisk()
    gate = ReversionCapitalGate(engine_equity=Decimal("10000"))
    aar_plug = ReversionAARLogging()

    candidates = await detect.scan(as_of=as_of)
    assert candidates, "synthetic oversold/overbought fixtures should produce candidates"

    decisions: list[ExecutionDecision] = []
    open_positions = 0
    high_grade = _high_grade_fundamentals()
    for cand in candidates:
        assessment = lifecycle.assess(cand, fundamentals=high_grade)
        if assessment.phase is not Phase.ACTIVE:
            continue
        decision = execute.decide(
            assessment,
            account_capital=Decimal("10000"),
            open_positions=open_positions,
            allow_shorts=True,  # exercise both directions.
        )
        if decision is None:
            continue
        if not gate.check_trade(
            size=decision.notional_usd, engine_pnl=Decimal("0"), open_positions=open_positions
        ):
            continue
        decisions.append(decision)
        open_positions += 1

    assert decisions, "pipeline should produce at least one decision"
    for d in decisions:
        assert len(d.order_payloads) == 2
        tier1, tier2 = d.order_payloads
        assert tier1["order_class"] == "bracket"
        assert d.notional_usd <= Decimal("2000")

    # Build + log an AAR for the first trade as a sanity check.
    first = decisions[0]
    tier1 = first.order_payloads[0]
    aar: AfterActionReport = aar_plug.build_aar(
        trade_id=f"reversion-{as_of.isoformat()}-{first.ticker}-001",
        ticker=first.ticker,
        entry_ts=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        exit_ts=datetime.combine(as_of + timedelta(days=3), datetime.min.time(), tzinfo=UTC),
        entry_price=Decimal(tier1["take_profit"]["limit_price"]),
        exit_price=Decimal(tier1["take_profit"]["limit_price"]),
        qty=Decimal(first.qty),
        confidence_at_entry=Decimal("0.75"),
        sizing_pct_of_engine_equity=first.notional_usd / Decimal("10000"),
        exit_reason=ExitReason.TIER1_MID_BAND,
        rule_compliance=True,
    )
    assert aar.engine == "reversion"
    aar_plug.log_aar(aar)
