"""Vector engine — unit tests for the five plugs.

Covers the plug *contracts* on synthetic data: setup gate logic, lifecycle
phase transitions (incl. the early-cut window and the trailing-stop arm),
execution sizing across the VIX scaling table, the capital gate's pre-grad
caps and graduation thresholds, and the AAR builder shape. The end-to-end
scheduler test is deferred to the integration layer.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from tpcore.aar.models import ExitReason

from vector.models import (
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    PROFIT_TARGET_PCT,
    Phase,
    SetupCandidate,
)
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import (
    GRAD_MIN_AVG_RETURN,
    GRAD_MIN_TRADES,
    GRAD_MIN_WIN_RATE,
    GraduationStats,
    VectorCapitalGate,
)
from vector.plugs.execution_risk import VectorExecutionRisk
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis
from vector.plugs.setup_detection import VectorSetupDetection


# ────────────────────────────────────────────────────────────────────────────
# Fixtures — synthetic bar panels
# ────────────────────────────────────────────────────────────────────────────


def _stable_then_breakout(end: date, base: float = 100.0, n: int = 220) -> pd.DataFrame:
    """Tight oscillation, then a clean breakout above 50-MA on heavy volume."""
    rows = []
    day = end - timedelta(days=n + 10)
    for i in range(n - 1):
        c = base + (0.4 if i % 2 else -0.4)
        rows.append({"date": day, "open": c - 0.1, "high": c + 0.2, "low": c - 0.2, "close": c, "volume": 5_000_000})
        day += timedelta(days=1)
    # Breakout day — close above the 50-MA on 2x volume.
    rows.append({"date": day, "open": base, "high": base + 6, "low": base - 0.2, "close": base + 5.5, "volume": 12_000_000})
    return pd.DataFrame(rows).set_index("date").sort_index()


def _fundamentals_high_quality() -> dict:
    """Revenue > floor, P/B ~ 0.96, D/E ~ 0.36, +66% YoY net income."""
    return {
        "revenue": Decimal("800000000"),  # $800M > floor $500M
        "net_income": Decimal("100000000"),
        "fcf": Decimal("120000000"),
        # bvps = (15B − 4B) / 100M = $110/share → P/B at price ~$105 = 0.95.
        "total_assets": Decimal("15000000000"),
        "total_liabilities": Decimal("4000000000"),
        "shares_outstanding": Decimal("100000000"),
        "history": [
            {"net_income": Decimal("90000000")},
            {"net_income": Decimal("80000000")},
            {"net_income": Decimal("70000000")},
            {"net_income": Decimal("60000000")},
        ],
    }


def _fundamentals_below_floor() -> dict:
    f = _fundamentals_high_quality()
    f["revenue"] = Decimal("100000000")  # below $500M floor
    return f


# ────────────────────────────────────────────────────────────────────────────
# Setup detection
# ────────────────────────────────────────────────────────────────────────────


def test_setup_detection_finds_candidate_when_all_gates_pass() -> None:
    plug = VectorSetupDetection(universe=("AAA",))
    bars = {"AAA": _stable_then_breakout(date(2026, 5, 1))}
    fundamentals = {"AAA": _fundamentals_high_quality()}
    candidates = plug.scan(
        as_of=date(2026, 5, 1),
        bars_by_ticker=bars,
        fundamentals_by_ticker=fundamentals,
        spy_panel=None,
        vix_value=20.0,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.ticker == "AAA"
    assert c.pullback_or_breakout == "breakout_above_50ma"
    assert c.vector_score >= 30  # technical + catalyst > 30 (sentiment 0)


def test_setup_detection_rejects_when_revenue_below_floor() -> None:
    plug = VectorSetupDetection(universe=("AAA",))
    bars = {"AAA": _stable_then_breakout(date(2026, 5, 1))}
    fundamentals = {"AAA": _fundamentals_below_floor()}
    candidates = plug.scan(
        as_of=date(2026, 5, 1),
        bars_by_ticker=bars,
        fundamentals_by_ticker=fundamentals,
        spy_panel=None,
        vix_value=20.0,
    )
    assert candidates == []


def test_setup_detection_rejects_when_vix_above_block_threshold() -> None:
    plug = VectorSetupDetection(universe=("AAA",))
    bars = {"AAA": _stable_then_breakout(date(2026, 5, 1))}
    fundamentals = {"AAA": _fundamentals_high_quality()}
    candidates = plug.scan(
        as_of=date(2026, 5, 1),
        bars_by_ticker=bars,
        fundamentals_by_ticker=fundamentals,
        spy_panel=None,
        vix_value=30.0,  # above 28 → no entries
    )
    assert candidates == []


def test_setup_detection_healthcheck_ok() -> None:
    plug = VectorSetupDetection()
    h = plug.healthcheck()
    assert h["ok"] is True
    assert h["plug"] == "setup_detection"


# ────────────────────────────────────────────────────────────────────────────
# Lifecycle analysis
# ────────────────────────────────────────────────────────────────────────────


def _candidate(*, last_close: Decimal = Decimal("100"), vix: float | None = 18.0) -> SetupCandidate:
    return SetupCandidate(
        ticker="AAA",
        as_of=date(2026, 5, 1),
        vector_score=70.0,
        technical=35.0,
        catalyst=25.0,
        sentiment=10.0,
        last_close=last_close,
        sma_50=Decimal("95"),
        sma_200=Decimal("90"),
        avg_volume=5_000_000,
        vix_at_entry=vix,
        spy_in_uptrend=True,
        earnings_growth_yoy=0.20,
        pullback_or_breakout="breakout_above_50ma",
    )


def test_lifecycle_initial_assess_phase_entry_with_correct_levels() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    assert a.phase is Phase.ENTRY
    assert a.entry_price == Decimal("100")
    assert a.stop_price == Decimal("93.00")  # 100 × (1 − 0.07)
    assert a.profit_target_price == Decimal("115.00")  # 100 × (1 + 0.15)
    assert a.days_held == 0


def test_lifecycle_step_promotes_to_holding_after_window() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    # Three uneventful sessions → still ENTRY at day 1, 2, then HOLDING at 3+
    a = plug.step(a, today_close=Decimal("101"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.ENTRY
    a = plug.step(a, today_close=Decimal("102"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.ENTRY
    a = plug.step(a, today_close=Decimal("103"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.HOLDING
    assert a.days_held == 3


def test_lifecycle_early_cut_when_close_below_10ma_in_window() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    # Day 1: close drops below 10-MA → EARLY_CUT, flag set.
    a = plug.step(a, today_close=Decimal("96"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.EARLY_CUT
    assert a.early_cut_applied is True


def test_lifecycle_target_exit() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    a = plug.step(a, today_close=Decimal("116"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.EXIT


def test_lifecycle_hard_stop_exit() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    a = plug.step(a, today_close=Decimal("92"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.EXIT


def test_lifecycle_trailing_stop_arms_then_fires() -> None:
    plug = VectorLifecycleAnalysis()
    a = plug.assess(_candidate())
    # Drive close to +12% — trail arms.
    a = plug.step(a, today_close=Decimal("112"), today_sma_10=Decimal("99"))
    assert a.trailing_armed is True
    # Pull back 6% from peak (112 × 0.94 = 105.28) — trail fires.
    a = plug.step(a, today_close=Decimal("105"), today_sma_10=Decimal("99"))
    assert a.phase is Phase.EXIT


# ────────────────────────────────────────────────────────────────────────────
# Execution risk
# ────────────────────────────────────────────────────────────────────────────


def test_execution_returns_none_below_score_floor() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate()
    cand = cand.model_copy(update={"vector_score": 30.0})  # below SCORE_WEAK
    a = VectorLifecycleAnalysis().assess(cand)
    assert plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=0) is None


def test_execution_returns_none_when_at_position_cap() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate()
    a = VectorLifecycleAnalysis().assess(cand)
    assert plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=MAX_CONCURRENT_POSITIONS) is None


def test_execution_full_size_when_vix_low() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate(vix=18.0)
    a = VectorLifecycleAnalysis().assess(cand)
    d = plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=0)
    assert d is not None
    assert d.vix_size_factor == Decimal("1.0")
    # 100 share price, $2000 cap → 20 shares
    assert d.qty == 20


def test_execution_half_size_when_vix_above_25() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate(vix=27.0)
    a = VectorLifecycleAnalysis().assess(cand)
    d = plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=0)
    assert d is not None
    assert d.vix_size_factor == Decimal("0.5")
    # $2000 × 0.5 = $1000 → 10 shares
    assert d.qty == 10


def test_execution_quarter_size_when_vix_above_30() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate(vix=32.0)
    a = VectorLifecycleAnalysis().assess(cand)
    d = plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=0)
    assert d is not None
    assert d.vix_size_factor == Decimal("0.25")
    assert d.qty == 5  # $2000 × 0.25 = $500 → 5 shares


def test_execution_payload_is_bracket() -> None:
    plug = VectorExecutionRisk()
    cand = _candidate()
    a = VectorLifecycleAnalysis().assess(cand)
    d = plug.decide(cand, a, account_equity=Decimal("10000"), open_positions=0)
    assert d is not None
    assert len(d.order_payloads) == 1
    payload = d.order_payloads[0]
    assert payload["order_class"] == "bracket"
    assert payload["side"] == "buy"
    assert "take_profit" in payload
    assert "stop_loss" in payload
    assert payload["client_order_id"].startswith("vector_AAA_")


# ────────────────────────────────────────────────────────────────────────────
# Capital gate
# ────────────────────────────────────────────────────────────────────────────


def test_capital_gate_check_trade_pre_grad_cap() -> None:
    gate = VectorCapitalGate()
    assert gate.check_trade(size=Decimal("2000"), engine_pnl=Decimal("0"))
    assert not gate.check_trade(size=Decimal("2001"), engine_pnl=Decimal("0"))


def test_capital_gate_position_count_cap() -> None:
    gate = VectorCapitalGate()
    assert gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=4)
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=5)


def test_capital_gate_daily_loss_freeze() -> None:
    gate = VectorCapitalGate()
    assert not gate.check_trade(size=Decimal("1000"), engine_pnl=Decimal("-600"))  # −6% drawdown


def test_capital_gate_is_graduated_thresholds() -> None:
    assert not VectorCapitalGate.is_graduated(GraduationStats(n_trades=10, win_rate=0.7, avg_return=0.05))
    assert VectorCapitalGate.is_graduated(
        GraduationStats(n_trades=GRAD_MIN_TRADES, win_rate=GRAD_MIN_WIN_RATE, avg_return=GRAD_MIN_AVG_RETURN)
    )


# ────────────────────────────────────────────────────────────────────────────
# AAR logging
# ────────────────────────────────────────────────────────────────────────────


def test_aar_logging_builds_well_formed_aar() -> None:
    plug = VectorAARLogging()
    aar = plug.build_aar(
        trade_id="vector-2026-05-01-AAA-001",
        ticker="AAA",
        entry_ts=datetime(2026, 5, 1, 13, 30, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 5, 19, 55, tzinfo=UTC),
        entry_price=Decimal("100"),
        exit_price=Decimal("115"),
        qty=Decimal("20"),
        exit_reason=ExitReason.TAKE_PROFIT,
        confidence_at_entry=Decimal("0.70"),
        sizing_pct_of_engine_equity=Decimal("0.20"),
    )
    assert aar.engine == "vector"
    assert aar.pnl_gross == Decimal("300")
    assert aar.exit_reason is ExitReason.TAKE_PROFIT
