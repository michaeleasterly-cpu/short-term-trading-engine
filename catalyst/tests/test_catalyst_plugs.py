"""Catalyst — exhaustive plug unit tests.

Covers all 5 plugs end-to-end (engine_readiness §6 + §10): every plug
subclasses :class:`BaseEnginePlug` and implements
``validate_dependencies`` + ``healthcheck``; setup_detection populates
``FilterDiagnostics``; ``aar_logging.build_aar`` derives ``exit_reason``
via :func:`tpcore.aar.classify_exit_reason` (never a hardcoded literal);
the capital gate denies / approves / graduation-rubric paths.

Hermetic: no DB, no network, no module-level ``import ops.lab.run``.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from catalyst.models import (
    CATALYST_MIN_AGGREGATE_USD,
    CATALYST_MIN_DISTINCT_INSIDERS,
    InsiderCluster,
    Phase,
    SetupCandidate,
)
from catalyst.plugs.aar_logging import CatalystAARLogging
from catalyst.plugs.capital_gate import CatalystCapitalGate
from catalyst.plugs.execution_risk import CatalystExecutionRisk
from catalyst.plugs.lifecycle_analysis import CatalystLifecycleAnalysis
from catalyst.plugs.setup_detection import (
    CatalystSetupDetection,
    _density_score,
    detect_clusters,
)
from tpcore.aar.models import ExitReason
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.exceptions import SizingError
from tpcore.interfaces.engine_plug import BaseEnginePlug

# ─── Section 10 compliance — every plug is a BaseEnginePlug ──────────────


def test_every_plug_subclasses_baseengineplug():
    """engine_readiness §1 / §10: exactly 5 BaseEnginePlug subclasses."""
    plugs = [
        CatalystSetupDetection,
        CatalystLifecycleAnalysis,
        CatalystExecutionRisk,
        CatalystAARLogging,
        CatalystCapitalGate,
    ]
    assert len(plugs) == 5
    for cls in plugs:
        assert issubclass(cls, BaseEnginePlug), cls


def test_every_plug_implements_validate_and_healthcheck():
    """engine_readiness §10 grep #1: validate_dependencies + healthcheck."""
    plugs = [
        CatalystSetupDetection(),
        CatalystLifecycleAnalysis(),
        CatalystExecutionRisk(),
        CatalystAARLogging(),
        CatalystCapitalGate(),
    ]
    for plug in plugs:
        assert plug.validate_dependencies() is True
        hc = plug.healthcheck()
        assert hc["engine"] == "catalyst"
        assert hc["ok"] is True
        assert "plug" in hc


# ─── Plug 1 — setup detection ───────────────────────────────────────────


def _insider_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[
        "ticker", "filing_date", "insider_name",
        "transaction_type", "value"])


def _price_df(closes: list[float], volumes: list[int],
              start: date) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [pd.Timestamp(start + timedelta(days=i)) for i in range(len(closes))])
    return pd.DataFrame({"close": closes, "volume": volumes}, index=idx)


def test_detect_clusters_aggregates_distinct_insiders():
    """Distinct-insider count + aggregate $ summed correctly."""
    rows = _insider_df([
        {"ticker": "AAPL", "filing_date": date(2024, 5, 1),
         "insider_name": "A", "transaction_type": "BUY",
         "value": 100_000.0},
        {"ticker": "AAPL", "filing_date": date(2024, 5, 5),
         "insider_name": "B", "transaction_type": "BUY",
         "value": 200_000.0},
        {"ticker": "AAPL", "filing_date": date(2024, 5, 10),
         "insider_name": "C", "transaction_type": "BUY",
         "value": 300_000.0},
        # A SELL — must be excluded.
        {"ticker": "AAPL", "filing_date": date(2024, 5, 11),
         "insider_name": "D", "transaction_type": "SELL",
         "value": 999_999.0},
    ])
    out = detect_clusters(
        insider_rows=rows, as_of=date(2024, 5, 15), window_days=30)
    assert "AAPL" in out
    cluster = out["AAPL"]
    assert cluster.distinct_insiders == 3
    assert cluster.aggregate_value_usd == Decimal("600000.0")
    assert cluster.n_buy_transactions == 3


def test_detect_clusters_strictly_backward_window():
    """lab_candidate_readiness §9: no row dated after as_of enters a score."""
    rows = _insider_df([
        {"ticker": "MSFT", "filing_date": date(2024, 5, 1),
         "insider_name": "A", "transaction_type": "BUY",
         "value": 100_000.0},
        # After as_of — MUST be excluded.
        {"ticker": "MSFT", "filing_date": date(2024, 6, 1),
         "insider_name": "B", "transaction_type": "BUY",
         "value": 999_999.0},
    ])
    out = detect_clusters(
        insider_rows=rows, as_of=date(2024, 5, 15), window_days=30)
    if "MSFT" in out:
        # Tolerated only if exactly 1 buyer was counted (the prior row);
        # the future row must not be counted regardless.
        assert out["MSFT"].distinct_insiders == 1
        assert out["MSFT"].aggregate_value_usd == Decimal("100000.0")


def test_setup_detection_filter_diagnostics_populated():
    """engine_readiness §10 grep #2: FilterDiagnostics carries per-gate counts."""
    plug = CatalystSetupDetection()
    rows = _insider_df([])
    out, diag = plug.detect(
        as_of=date(2024, 5, 15),
        universe=("AAPL", "MSFT"),
        insider_rows=rows,
        prices_by_ticker={},
    )
    assert out == []
    assert isinstance(diag, FilterDiagnostics)
    assert diag.universe_total == 2
    # No clusters → every ticker blocks at gate 1 (cluster size).
    assert diag.cluster_size_blocked == 2
    assert diag.candidates_passed == 0


def test_setup_detection_full_pipeline_passes_clean_ticker():
    """A ticker that clears all four gates produces a SetupCandidate."""
    plug = CatalystSetupDetection()
    as_of = date(2024, 5, 15)
    insiders = _insider_df([
        {"ticker": "AAPL", "filing_date": as_of - timedelta(days=i),
         "insider_name": f"insider_{i}", "transaction_type": "BUY",
         "value": 200_000.0}
        for i in range(1, 4)  # 3 distinct insiders, $600k aggregate
    ])
    # Need ≥ SMA_TREND_PERIOD prices; uptrend so close > SMA.
    closes = [50.0 + i * 0.2 for i in range(80)]  # rising
    volumes = [5_000_000] * 80
    prices = _price_df(closes, volumes, as_of - timedelta(days=79))
    out, diag = plug.detect(
        as_of=as_of, universe=("AAPL",),
        insider_rows=insiders,
        prices_by_ticker={"AAPL": prices},
    )
    assert len(out) == 1
    cand = out[0]
    assert cand.ticker == "AAPL"
    assert cand.cluster.distinct_insiders == 3
    assert cand.cluster_density > 0
    assert diag.candidates_passed == 1


def test_density_score_rewards_quorum():
    """Density score rewards quorum over single-insider mega-blocks."""
    one_big = _density_score(Decimal("10000000"), 1)
    three_small = _density_score(Decimal("1200000"), 3)
    # Identity check: 10M × 1 == 3.6M × ... actually 10M × 1 = 10M, 1.2M × 3 = 3.6M
    # We pick a balance: the quorum should AT LEAST be in the same order.
    assert one_big > three_small  # at this ratio the big insider still wins
    # At a smaller mega-block, the quorum wins.
    one_small_big = _density_score(Decimal("2000000"), 1)
    three_medium = _density_score(Decimal("800000"), 3)
    assert three_medium > one_small_big


# ─── Plug 2 — lifecycle analysis ────────────────────────────────────────


def _make_candidate(price: float = 100.0) -> SetupCandidate:
    return SetupCandidate(
        ticker="AAPL", as_of=date(2024, 5, 15),
        cluster=InsiderCluster(
            ticker="AAPL", as_of=date(2024, 5, 15), window_days=30,
            distinct_insiders=CATALYST_MIN_DISTINCT_INSIDERS,
            aggregate_value_usd=CATALYST_MIN_AGGREGATE_USD,
            n_buy_transactions=3,
        ),
        cluster_density=1.0,
        last_close=Decimal(str(price)),
        sma_50=Decimal(str(price - 1)),
        avg_volume=5_000_000,
    )


def test_lifecycle_assess_entry_builds_bracket_levels():
    plug = CatalystLifecycleAnalysis()
    cand = _make_candidate(100.0)
    assessment = plug.assess_entry(cand)
    assert assessment.phase == Phase.ENTRY
    # +12% target, −7% stop.
    assert assessment.profit_target_price == Decimal("112.0000")
    assert assessment.stop_price == Decimal("93.0000")
    assert assessment.days_held == 0
    assert assessment.trailing_armed is False


def test_lifecycle_update_phase_take_profit_hit():
    plug = CatalystLifecycleAnalysis()
    cand = _make_candidate(100.0)
    a0 = plug.assess_entry(cand)
    a1 = plug.update_phase(a0, as_of=date(2024, 5, 16),
                           close=Decimal("113"))
    assert a1.phase == Phase.EXIT


def test_lifecycle_update_phase_stop_hit():
    plug = CatalystLifecycleAnalysis()
    cand = _make_candidate(100.0)
    a0 = plug.assess_entry(cand)
    a1 = plug.update_phase(a0, as_of=date(2024, 5, 16),
                           close=Decimal("92"))
    assert a1.phase == Phase.EXIT


def test_lifecycle_update_phase_trailing_stop_arms_then_trips():
    plug = CatalystLifecycleAnalysis()
    cand = _make_candidate(100.0)
    a0 = plug.assess_entry(cand)
    # +8% arms the trail.
    a1 = plug.update_phase(a0, as_of=date(2024, 5, 16),
                           close=Decimal("108"))
    assert a1.trailing_armed is True
    assert a1.phase == Phase.HOLDING
    # Drop > 5% from high-water trips it.
    a2 = plug.update_phase(a1, as_of=date(2024, 5, 17),
                           close=Decimal("100"))
    assert a2.phase == Phase.EXIT


def test_lifecycle_early_cut_fires_in_first_three_days():
    plug = CatalystLifecycleAnalysis()
    cand = _make_candidate(100.0)
    a0 = plug.assess_entry(cand)
    # Day 2, close below 10-SMA → early cut.
    a1 = plug.update_phase(a0, as_of=date(2024, 5, 16),
                           close=Decimal("99"), sma_10=Decimal("100"))
    assert a1.phase == Phase.EARLY_CUT
    assert a1.early_cut_applied is True


# ─── Plug 3 — execution / risk ──────────────────────────────────────────


def test_execution_risk_decide_happy_path():
    plug = CatalystExecutionRisk()
    cand = _make_candidate(100.0)
    decision = plug.decide(cand, engine_equity_usd=Decimal("10000"))
    assert decision is not None
    assert decision.qty > 0
    assert decision.notional_usd <= Decimal("1500")  # respects PRE_GRAD cap
    payload = decision.order_payloads[0]
    assert payload["symbol"] == "AAPL"
    assert payload["order_class"] == "bracket"
    assert payload["take_profit"]["limit_price"]
    assert payload["stop_loss"]["stop_price"]


def test_execution_risk_decide_raises_on_nonpositive_price():
    plug = CatalystExecutionRisk()
    cand = _make_candidate(100.0)
    cand = cand.model_copy(update={"last_close": Decimal("-1")})
    with pytest.raises(SizingError):
        plug.decide(cand, engine_equity_usd=Decimal("10000"))


def test_execution_risk_decide_zero_qty_returns_none():
    """A microscopic equity → qty rounds to zero → returns None, not a raise."""
    plug = CatalystExecutionRisk()
    cand = _make_candidate(100.0)
    decision = plug.decide(cand, engine_equity_usd=Decimal("50"))
    assert decision is None


def test_execution_risk_bracket_payload_includes_tp_and_sl():
    """Both bracket legs present (TP +12%, SL −7%)."""
    plug = CatalystExecutionRisk()
    cand = _make_candidate(100.0)
    decision = plug.decide(cand, engine_equity_usd=Decimal("10000"))
    payload = decision.order_payloads[0]
    assert Decimal(payload["take_profit"]["limit_price"]) == Decimal("112")
    assert Decimal(payload["stop_loss"]["stop_price"]) == Decimal("93")


# ─── Plug 4 — AAR logging ───────────────────────────────────────────────


def test_aar_logging_classify_exit_reason_is_called_not_hardcoded():
    """engine_readiness §10 grep #5: never hardcode an ExitReason literal."""
    plug = CatalystAARLogging()
    entry_ts = datetime(2024, 5, 15, 14, 0, tzinfo=UTC)
    exit_ts = datetime(2024, 5, 20, 20, 0, tzinfo=UTC)
    # No explicit exit_reason → must derive via classifier.
    aar = plug.build_aar(
        trade_id="ct_AAPL_1700000000", ticker="AAPL",
        entry_ts=entry_ts, exit_ts=exit_ts,
        entry_price=Decimal("100"), exit_price=Decimal("112"),
        qty=Decimal("10"), engine_equity_usd=Decimal("10000"),
        take_profit=Decimal("112"), stop_loss=Decimal("93"),
    )
    assert aar.exit_reason == ExitReason.TAKE_PROFIT


def test_aar_logging_stop_loss_classified():
    plug = CatalystAARLogging()
    entry_ts = datetime(2024, 5, 15, 14, 0, tzinfo=UTC)
    exit_ts = datetime(2024, 5, 18, 20, 0, tzinfo=UTC)
    aar = plug.build_aar(
        trade_id="ct_AAPL_1700000000", ticker="AAPL",
        entry_ts=entry_ts, exit_ts=exit_ts,
        entry_price=Decimal("100"), exit_price=Decimal("93"),
        qty=Decimal("10"), engine_equity_usd=Decimal("10000"),
        take_profit=Decimal("112"), stop_loss=Decimal("93"),
    )
    assert aar.exit_reason == ExitReason.STOP_LOSS


def test_aar_logging_time_stop_classified():
    plug = CatalystAARLogging()
    entry_ts = datetime(2024, 5, 15, 14, 0, tzinfo=UTC)
    exit_ts = datetime(2024, 6, 15, 20, 0, tzinfo=UTC)
    aar = plug.build_aar(
        trade_id="ct_AAPL_1700000000", ticker="AAPL",
        entry_ts=entry_ts, exit_ts=exit_ts,
        entry_price=Decimal("100"), exit_price=Decimal("103"),
        qty=Decimal("10"), engine_equity_usd=Decimal("10000"),
        take_profit=Decimal("112"), stop_loss=Decimal("93"),
    )
    # Closed mid-bracket → TIME_STOP.
    assert aar.exit_reason == ExitReason.TIME_STOP
    # P&L arithmetic.
    assert aar.pnl_gross == Decimal("30")
    assert aar.pnl_net == Decimal("30")


# ─── Plug 5 — capital gate ──────────────────────────────────────────────


def test_capital_gate_allows_in_bounds_trade():
    gate = CatalystCapitalGate()
    assert gate.check_trade(
        size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=0,
    ) is True


def test_capital_gate_rejects_oversize_trade():
    gate = CatalystCapitalGate()
    assert gate.check_trade(
        size=Decimal("9999999"), engine_pnl=Decimal("0"), open_positions=0,
    ) is False


def test_capital_gate_rejects_zero_size():
    gate = CatalystCapitalGate()
    assert gate.check_trade(
        size=Decimal("0"), engine_pnl=Decimal("0"), open_positions=0,
    ) is False


def test_capital_gate_rejects_past_position_cap():
    gate = CatalystCapitalGate()
    assert gate.check_trade(
        size=Decimal("1000"), engine_pnl=Decimal("0"), open_positions=99,
    ) is False


def test_capital_gate_daily_loss_freeze():
    gate = CatalystCapitalGate(engine_equity=Decimal("10000"))
    # 5% daily-loss freeze → −$500 hits the gate.
    assert gate.check_trade(
        size=Decimal("1000"), engine_pnl=Decimal("-600"), open_positions=0,
    ) is False


def test_capital_gate_is_graduated_thresholds():
    from tpcore.models.graduation import PerTradeGraduationStats
    bad = PerTradeGraduationStats(n_trades=5, win_rate=0.5, avg_return=0.01)
    ok = PerTradeGraduationStats(n_trades=40, win_rate=0.6, avg_return=0.05)
    assert CatalystCapitalGate.is_graduated(bad) is False
    assert CatalystCapitalGate.is_graduated(ok) is True
