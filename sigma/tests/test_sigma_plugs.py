"""End-to-end tests for the five Sigma plugs (Phase 1 acceptance)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sigma import (
    ExecutionDecision,
    GraduationStats,
    Phase,
    PhaseAssessment,
    SetupCandidate,
    SigmaAARLogging,
    SigmaCapitalGate,
    SigmaExecutionRisk,
    SigmaLifecycleAnalysis,
    SigmaSetupDetection,
)
from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.interfaces.data import Bar, DataProviderInterface

# ────────────────────────────────────────────────────────────────────────────
# Fixtures: synthetic bar generators + mock provider
# ────────────────────────────────────────────────────────────────────────────


def _bar(symbol: str, day: date, o: float, h: float, l: float, c: float, v: int) -> Bar:
    ts = datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC)
    return Bar(
        symbol=symbol,
        ts=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=v,
    )


def _range_bound_series(symbol: str, end: date, n: int = 60, base: float = 100.0) -> list[Bar]:
    """Noisy → tight transition.

    First half: wider oscillation (~$6 swings). Last half: tight $3 channel
    ending at the bottom of the cycle, so today's BB width is in the lower
    percentile and band_proximity ≈ 0.0–0.2 (near the lower band).
    """
    bars: list[Bar] = []
    day = end - timedelta(days=n + 10)
    noisy_offsets = [-3.0, +3.0, -2.0, +2.0]
    tight_offsets = [0.0, +1.5, +1.0, -1.5]
    half = n // 2
    for i in range(n):
        offsets = noisy_offsets if i < half else tight_offsets
        c = base + offsets[i % 4]
        o = c - 0.3
        h = max(o, c) + 0.4
        l = min(o, c) - 0.4
        bars.append(_bar(symbol, day, o, h, l, c, 5_000_000))
        day += timedelta(days=1)
    return bars


def _trending_series(symbol: str, end: date, n: int = 60, base: float = 100.0) -> list[Bar]:
    """Steady uptrend — high ADX, wide BB, should NOT qualify."""
    bars: list[Bar] = []
    day = end - timedelta(days=n + 10)
    for i in range(n):
        c = base + 0.8 * i
        o = c - 0.3
        h = c + 0.5
        l = o - 0.5
        bars.append(_bar(symbol, day, o, h, l, c, 5_000_000))
        day += timedelta(days=1)
    return bars


class MockDataProvider(DataProviderInterface):
    """Minimal mock — only ``get_daily_bars`` is exercised by the Sigma scan."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_symbol

    async def get_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [b for b in self._bars.get(symbol, []) if start <= b.ts.date() <= end]

    async def get_quote(self, symbol: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def get_fundamentals(self, symbol: str, as_of: date | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_earnings_calendar(self, symbol: str, start: date, end: date) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def list_active_symbols(self) -> list[str]:  # pragma: no cover - unused
        return list(self._bars.keys())

    async def list_delisted_symbols(self) -> list[tuple[str, date]]:  # pragma: no cover - unused
        return []


@pytest.fixture
def as_of() -> date:
    return date(2026, 5, 1)


@pytest.fixture
def mock_provider(as_of: date) -> MockDataProvider:
    return MockDataProvider(
        {
            "AAPL": _range_bound_series("AAPL", as_of, base=180.0),
            "MSFT": _range_bound_series("MSFT", as_of, base=420.0),
            "AMZN": _trending_series("AMZN", as_of, base=200.0),
            "GOOGL": _trending_series("GOOGL", as_of, base=160.0),
            "META": _range_bound_series("META", as_of, base=500.0),
            "TSLA": _trending_series("TSLA", as_of, base=180.0),
            "NVDA": _trending_series("NVDA", as_of, base=900.0),
            "JPM": _range_bound_series("JPM", as_of, base=200.0),
            "V": _range_bound_series("V", as_of, base=290.0),
            "WMT": _trending_series("WMT", as_of, base=85.0),
        }
    )


# ────────────────────────────────────────────────────────────────────────────
# Plug tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_detection_returns_candidates(
    mock_provider: MockDataProvider, as_of: date
) -> None:
    plug = SigmaSetupDetection(data=mock_provider)
    assert plug.validate_dependencies()
    assert plug.healthcheck()["ok"]

    candidates = await plug.scan(as_of=as_of)
    assert isinstance(candidates, list)
    assert len(candidates) >= 1, "range-bound series should produce ≥1 candidate"
    for c in candidates:
        assert isinstance(c, SetupCandidate)
        assert 0 <= c.sigma_score <= 100
        assert c.adx < 20.0  # universe filter
        assert c.bb_width_percentile < 0.30
        assert c.bb_lower < c.bb_mid < c.bb_upper
    # Trending names should be filtered out.
    tickers = {c.ticker for c in candidates}
    assert tickers.isdisjoint({"AMZN", "GOOGL", "TSLA", "NVDA", "WMT"})


def test_compute_chop_low_for_trending_high_for_oscillating() -> None:
    """CHOP must drop on a clean trend and rise on noisy oscillation."""
    import pandas as pd

    from sigma.plugs.setup_detection import CHOP_PERIOD, compute_chop

    n = 60
    # Pure trend: every day +1.
    trend_close = pd.Series([100.0 + i for i in range(n)])
    trend_high = trend_close + 0.5
    trend_low = trend_close - 0.5
    trend_chop = float(compute_chop(trend_high, trend_low, trend_close).iloc[-1])

    # Tight oscillation around 100 with ±0.5 swings.
    osc_close = pd.Series([100.0 + (0.5 if i % 2 else -0.5) for i in range(n)])
    osc_high = osc_close + 0.05
    osc_low = osc_close - 0.05
    osc_chop = float(compute_chop(osc_high, osc_low, osc_close).iloc[-1])

    assert trend_chop < 38.2, f"trending series should give low CHOP, got {trend_chop}"
    assert osc_chop > 61.8, f"oscillating series should give high CHOP, got {osc_chop}"
    # Window obeyed.
    assert pd.isna(compute_chop(trend_high, trend_low, trend_close).iloc[CHOP_PERIOD - 2])


def test_score_market_context_buckets() -> None:
    """Per-stock CHOP combinations land in the right Market Context bucket.

    The score uses the candidate's own CHOP — index-level (SPY) gating was
    removed after the backtest showed it underperformed (Sharpe −28% vs
    baseline). See sigma/backtest.py.
    """
    from sigma.plugs.setup_detection import _score_market_context

    # CHOP > 61.8 → 15 regime-confirmation; VWAP within 1% → 10 → total 25.
    assert _score_market_context(chop=70.0, last_close=180.00, last_vwap_20=180.50) == 25.0
    # 38.2 < CHOP ≤ 61.8 → 10 regime-confirmation; VWAP miss → 10 total.
    assert _score_market_context(chop=50.0, last_close=180.0, last_vwap_20=200.0) == 10.0
    # CHOP < 38.2 → 0 regime-confirmation; VWAP within 1% → 10. (In live code
    # such a candidate is hard-filtered out upstream — this guards the score
    # function in isolation.)
    assert _score_market_context(chop=30.0, last_close=180.0, last_vwap_20=180.5) == 10.0
    # NaN CHOP → safe zero on the regime leg; VWAP miss → 0 total.
    assert _score_market_context(
        chop=float("nan"), last_close=180.0, last_vwap_20=float("nan")
    ) == 0.0


def test_lifecycle_analysis_classifies_phase() -> None:
    plug = SigmaLifecycleAnalysis()
    assert plug.healthcheck()["ok"]

    base = SetupCandidate(
        ticker="AAPL",
        as_of=date(2026, 5, 1),
        sigma_score=82.0,
        channel_quality=35.0,
        entry_precision=30.0,
        market_context=17.0,
        band_proximity=0.10,
        bb_width_percentile=0.15,
        adx=15.0,
        chop=70.0,
        suggested_entry_price=Decimal("180.00"),
        bb_upper=Decimal("184.00"),
        bb_lower=Decimal("176.00"),
        bb_mid=Decimal("180.00"),
    )
    assessment = plug.assess(base)
    assert isinstance(assessment, PhaseAssessment)
    assert assessment.phase is Phase.ACTIVE
    # Stop is exactly entry × (1 − 0.03).
    assert assessment.stop_price == Decimal("174.60")
    assert assessment.take_profit_mid == Decimal("180.00")
    assert assessment.take_profit_far == Decimal("184.00")

    far_from_band = base.model_copy(update={"band_proximity": 0.45})
    assert plug.assess(far_from_band).phase is Phase.SETUP

    exhausted = base.model_copy(update={"band_proximity": 0.97})
    assert plug.assess(exhausted).phase is Phase.EXHAUSTION


def test_execution_risk_builds_two_tier_payloads() -> None:
    plug = SigmaExecutionRisk()
    assert plug.healthcheck()["ok"]

    assessment = PhaseAssessment(
        ticker="AAPL",
        as_of=date(2026, 5, 1),
        phase=Phase.ACTIVE,
        entry_price=Decimal("180.00"),
        stop_price=Decimal("174.60"),
        take_profit_mid=Decimal("184.00"),
        take_profit_far=Decimal("188.00"),
    )
    decision = plug.decide(assessment, account_capital=Decimal("10000"))
    assert isinstance(decision, ExecutionDecision)
    assert decision.qty >= 2
    assert decision.notional_usd <= Decimal("1500")
    # 50/50 with odd-share bias to tier 1.
    assert decision.tier1_qty + decision.tier2_qty == decision.qty
    assert decision.tier1_qty - decision.tier2_qty in (0, 1)

    assert isinstance(decision.order_payloads, list)
    assert len(decision.order_payloads) == 2
    tier1, tier2 = decision.order_payloads

    # Tier 1: bracket at mid-band with hard stop.
    assert tier1["symbol"] == "AAPL"
    assert tier1["side"] == "buy"
    assert tier1["type"] == "market"
    assert tier1["order_class"] == "bracket"
    assert tier1["qty"] == str(decision.tier1_qty)
    assert tier1["take_profit"] == {"limit_price": "184.00"}
    assert tier1["stop_loss"] == {"stop_price": "174.60"}
    assert tier1["client_order_id"].startswith("AAPL_")
    assert tier1["client_order_id"].endswith("_tier1")

    # Tier 2: GTC sell-limit at the upper band, no bracket legs.
    assert tier2["symbol"] == "AAPL"
    assert tier2["side"] == "sell"
    assert tier2["type"] == "limit"
    assert tier2["limit_price"] == "188.00"
    assert tier2["time_in_force"] == "gtc"
    assert tier2["qty"] == str(decision.tier2_qty)
    assert tier2["client_order_id"].endswith("_tier2")
    # Tier 1 and Tier 2 share the same prefix (timestamp-anchored).
    assert tier1["client_order_id"].rsplit("_", 1)[0] == tier2["client_order_id"].rsplit("_", 1)[0]

    # Non-active phase → no order.
    inactive = assessment.model_copy(update={"phase": Phase.SETUP})
    assert plug.decide(inactive, account_capital=Decimal("10000")) is None

    # Position-cap → no order.
    assert plug.decide(assessment, account_capital=Decimal("10000"), open_positions=4) is None


def test_execution_risk_odd_quantity_biases_tier1() -> None:
    """When total qty is odd, the extra share goes to Tier 1."""
    plug = SigmaExecutionRisk()
    assessment = PhaseAssessment(
        ticker="XYZ",
        as_of=date(2026, 5, 1),
        phase=Phase.ACTIVE,
        # $200 entry × 7 shares = $1400, fits under the $1500 cap; 7 is odd.
        entry_price=Decimal("200.00"),
        stop_price=Decimal("194.00"),
        take_profit_mid=Decimal("204.00"),
        take_profit_far=Decimal("208.00"),
    )
    decision = plug.decide(assessment, account_capital=Decimal("10000"))
    assert decision is not None
    assert decision.qty == 7
    assert decision.tier1_qty == 4
    assert decision.tier2_qty == 3


def test_execution_risk_rejects_when_qty_below_two() -> None:
    """Need at least one share per tier; under-two sizing is rejected."""
    plug = SigmaExecutionRisk()
    # $1500 cap / $900 entry → 1 share, not enough to split.
    assessment = PhaseAssessment(
        ticker="HIGH",
        as_of=date(2026, 5, 1),
        phase=Phase.ACTIVE,
        entry_price=Decimal("900.00"),
        stop_price=Decimal("873.00"),
        take_profit_mid=Decimal("920.00"),
        take_profit_far=Decimal("940.00"),
    )
    assert plug.decide(assessment, account_capital=Decimal("10000")) is None


def test_capital_gate_enforces_limits() -> None:
    gate = SigmaCapitalGate(engine_equity=Decimal("10000"))
    assert gate.healthcheck()["ok"]

    assert gate.check_trade(size=Decimal("1500"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("1501"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("0"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=4)
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("-600"))  # −6% drawdown

    # Graduation gating.
    assert not gate.is_graduated(GraduationStats(n_trades=10, win_rate=0.7, avg_return=0.02))
    assert gate.is_graduated(GraduationStats(n_trades=50, win_rate=0.65, avg_return=0.015))


def test_aar_logging_builds_and_logs_aar(caplog: pytest.LogCaptureFixture) -> None:
    plug = SigmaAARLogging()
    assert plug.healthcheck()["ok"]

    aar = plug.build_aar(
        trade_id="sigma-2026-05-01-AAPL-001",
        ticker="AAPL",
        entry_ts=datetime(2026, 5, 1, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 2, 19, 55, tzinfo=UTC),
        entry_price=Decimal("180.00"),
        exit_price=Decimal("182.00"),
        qty=Decimal("8"),
        confidence_at_entry=Decimal("0.82"),
        sizing_pct_of_engine_equity=Decimal("0.144"),
        exit_reason=ExitReason.TAKE_PROFIT,
        rule_compliance=True,
        regime_tags=["range_bound"],
    )
    assert isinstance(aar, AfterActionReport)
    assert aar.pnl_gross == Decimal("16.00")
    assert aar.pnl_net == Decimal("16.00")

    payload = plug.log_aar(aar)
    assert payload["ticker"] == "AAPL"
    assert payload["exit_reason"] == "take_profit"


# ────────────────────────────────────────────────────────────────────────────
# End-to-end: scan → assess → decide → gate → log
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_end_to_end(mock_provider: MockDataProvider, as_of: date) -> None:
    detect = SigmaSetupDetection(data=mock_provider)
    lifecycle = SigmaLifecycleAnalysis()
    execute = SigmaExecutionRisk()
    gate = SigmaCapitalGate(engine_equity=Decimal("10000"))
    aar_plug = SigmaAARLogging()

    candidates = await detect.scan(as_of=as_of)
    assert candidates, "expected at least one candidate from the range-bound fixtures"

    decisions: list[ExecutionDecision] = []
    open_positions = 0
    for cand in candidates:
        assessment = lifecycle.assess(cand)
        if assessment.phase is not Phase.ACTIVE:
            continue
        decision = execute.decide(
            assessment, account_capital=Decimal("10000"), open_positions=open_positions
        )
        if decision is None:
            continue
        if not gate.check_trade(
            size=decision.notional_usd, engine_pnl=Decimal("0"), open_positions=open_positions
        ):
            continue
        decisions.append(decision)
        open_positions += 1

    assert decisions, "pipeline should produce at least one ACTIVE-phase order"
    for d in decisions:
        assert len(d.order_payloads) == 2
        tier1, tier2 = d.order_payloads
        assert tier1["order_class"] == "bracket"
        assert tier2["type"] == "limit" and tier2["time_in_force"] == "gtc"
        assert int(tier1["qty"]) + int(tier2["qty"]) == d.qty
        assert d.notional_usd <= Decimal("1500")

    # Build + log an AAR for the first trade as a sanity check.
    first = decisions[0]
    tier1_payload = first.order_payloads[0]
    aar = aar_plug.build_aar(
        trade_id=f"sigma-{as_of.isoformat()}-{first.ticker}-001",
        ticker=first.ticker,
        entry_ts=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
        exit_ts=datetime.combine(as_of + timedelta(days=2), datetime.min.time(), tzinfo=UTC),
        entry_price=Decimal(tier1_payload["take_profit"]["limit_price"]),  # stand-in
        exit_price=Decimal(tier1_payload["take_profit"]["limit_price"]),
        qty=Decimal(first.qty),
        confidence_at_entry=Decimal("0.80"),
        sizing_pct_of_engine_equity=first.notional_usd / Decimal("10000"),
        exit_reason=ExitReason.TAKE_PROFIT,
        rule_compliance=True,
    )
    assert aar.engine == "sigma"
    aar_plug.log_aar(aar)


def test_lifecycle_handle_tier1_fill_updates_state() -> None:
    plug = SigmaLifecycleAnalysis()
    assessment = PhaseAssessment(
        ticker="AAPL",
        as_of=date(2026, 5, 1),
        phase=Phase.ACTIVE,
        entry_price=Decimal("180.00"),
        stop_price=Decimal("174.60"),
        take_profit_mid=Decimal("184.00"),
        take_profit_far=Decimal("188.00"),
    )
    assert assessment.tier1_filled is False
    assert assessment.remaining_shares == 0

    after_tier1 = plug.handle_tier1_fill(assessment, position_remaining=4)
    assert after_tier1.tier1_filled is True
    assert after_tier1.remaining_shares == 4
    # Trade is still open at the Tier 2 leg → phase stays ACTIVE.
    assert after_tier1.phase is Phase.ACTIVE
    # Original assessment is untouched.
    assert assessment.tier1_filled is False

    fully_closed = plug.handle_tier1_fill(assessment, position_remaining=0)
    assert fully_closed.phase is Phase.EXHAUSTION
    assert fully_closed.remaining_shares == 0

    with pytest.raises(ValueError):
        plug.handle_tier1_fill(assessment, position_remaining=-1)


def test_aar_logging_tier1_and_tier2_helpers() -> None:
    plug = SigmaAARLogging()
    common = dict(
        trade_id="sigma-2026-05-01-AAPL-001",
        ticker="AAPL",
        entry_ts=datetime(2026, 5, 1, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 1, 19, 55, tzinfo=UTC),
        entry_price=Decimal("180.00"),
        confidence_at_entry=Decimal("0.80"),
        sizing_pct_of_engine_equity=Decimal("0.144"),
        rule_compliance=True,
    )

    tier1_aar = plug.build_tier1_aar(
        **common,
        exit_price=Decimal("184.00"),
        tier1_qty=Decimal("4"),
    )
    assert tier1_aar.exit_reason is ExitReason.TIER1_MID_BAND
    assert tier1_aar.qty == Decimal("4")
    assert tier1_aar.pnl_gross == Decimal("16.00")  # (184 − 180) × 4
    assert tier1_aar.trade_id.endswith("-tier1")

    tier2_aar = plug.build_tier2_aar(
        **common,
        tier1_exit_price=Decimal("184.00"),
        tier2_exit_price=Decimal("188.00"),
        tier1_qty=Decimal("4"),
        tier2_qty=Decimal("4"),
    )
    assert tier2_aar.exit_reason is ExitReason.TIER2_OPPOSITE_BAND
    assert tier2_aar.qty == Decimal("8")
    # Combined P&L: 4×(184−180) + 4×(188−180) = 16 + 32 = 48.
    assert tier2_aar.pnl_gross == Decimal("48.00")
    assert tier2_aar.trade_id.endswith("-tier2")
