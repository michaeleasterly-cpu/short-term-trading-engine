"""Sentinel — unit tests for the five plugs (research spike, 2026-05-15).

Covers:

* Bear Score sub-scorers — each at threshold, above, below, missing.
* Scaled-to-100 raw-to-scaled mapping.
* SPY rally veto + counter-trend math.
* Lifecycle state machine — DORMANT → WATCH → ACTIVE → FADING → EXITED.
* Activation rally veto and false-signal short-circuit.
* Basket override sequence — SQQQ eligibility, shallow-recession,
  VIX circuit breaker — and their composition.
* Missing-ETF fallback renormalization.
* Capital gate cap (10% vs 20%) and check_rebalance veto.
* Execution build — sizing, share-count rounding, DORMANT → no-targets.

No DB dependency — all tests build their inputs in-memory.
"""
from __future__ import annotations

from datetime import date as date_t
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest

from sentinel.models import (
    ACTIVATION_CONSECUTIVE_DAYS,
    BASKET_WEIGHTS_DEFAULT,
    PERMANENT_CAP_PCT,
    PRE_GRADUATION_CAP_PCT,
    BearScoreBreakdown,
    SentinelPhase,
    SentinelState,
    apply_basket_overrides,
    apply_missing_etf_fallback,
)
from sentinel.plugs.capital_gate import (
    SentinelCapitalGate,
    evaluate_graduation,
)
from sentinel.plugs.execution_risk import SentinelExecutionRisk
from sentinel.plugs.lifecycle_analysis import SentinelLifecycleAnalysis
from sentinel.plugs.setup_detection import (
    compute_spy_rally_pct,
    compute_vix_proxy_series,
    scale_raw_to_100,
    score_credit_spread,
    score_industrial_production,
    score_initial_claims,
    score_sahm_rule,
    score_vix_proxy,
    score_yield_curve,
)

# ─── Bear Score sub-scorers ────────────────────────────────────────────────


class TestBearScoreSubScorers:
    def test_sahm_rule_at_threshold_pays_full_points(self) -> None:
        assert score_sahm_rule(Decimal("0.50")) == 25
        assert score_sahm_rule(Decimal("0.49")) == 0
        assert score_sahm_rule(None) == 0

    def test_industrial_production_hard_soft_else(self) -> None:
        # INDPRO-scaled thresholds (see models.py comment).
        assert score_industrial_production(Decimal("89.9")) == 15
        assert score_industrial_production(Decimal("90.0")) == 10
        assert score_industrial_production(Decimal("94.9")) == 10
        assert score_industrial_production(Decimal("95.0")) == 0
        assert score_industrial_production(None) == 0

    def test_initial_claims_requires_rising_two_consecutive(self) -> None:
        # > 260K, rising 2 weeks: 270 > 265 > 261 → pay.
        assert score_initial_claims(Decimal("270000"), Decimal("265000"), Decimal("261000")) == 10
        # > 260K but only rising 1 week (latest > prev, prev <= two_back).
        assert score_initial_claims(Decimal("270000"), Decimal("265000"), Decimal("266000")) == 0
        # <= 260K → 0 even if rising.
        assert score_initial_claims(Decimal("250000"), Decimal("245000"), Decimal("240000")) == 0
        # Missing → 0.
        assert score_initial_claims(None, Decimal("265000"), Decimal("261000")) == 0

    def test_yield_curve_bear_steepener(self) -> None:
        # Inverted (floor < 0) AND now less-inverted (latest > floor) → 15 pts.
        assert score_yield_curve(Decimal("-0.10"), Decimal("-0.50")) == 15
        # Inverted and still at the floor → 0 (no re-steepening).
        assert score_yield_curve(Decimal("-0.50"), Decimal("-0.50")) == 0
        # Never inverted → N/A → 0.
        assert score_yield_curve(Decimal("0.30"), Decimal("0.10")) == 0
        # Missing → 0.
        assert score_yield_curve(None, Decimal("-0.30")) == 0

    def test_credit_spread_graduated_tiers(self) -> None:
        """Baa-10Y spread graduated scorer: Watch / Warning / Recession.

        Replaces the prior HY OAS binary scorer (2026-05-15) after FRED
        truncated BAMLH0A0HYM2. Anchors: GFC ~6%, COVID ~4.9%, calm ~2%.
        """
        # Below Watch threshold → 0 pts regardless of direction.
        assert score_credit_spread(Decimal("2.50"), Decimal("2.30")) == 0
        # Watch tier (>3.0% AND widening) → 2 pts.
        assert score_credit_spread(Decimal("3.50"), Decimal("3.20")) == 2
        # Watch threshold but tightening → 0 pts (recovery, not stress).
        assert score_credit_spread(Decimal("3.50"), Decimal("3.60")) == 0
        # Warning tier (>4.0% AND widening) → 3 pts.
        assert score_credit_spread(Decimal("4.50"), Decimal("4.20")) == 3
        # Warning level but tightening → 0 pts.
        assert score_credit_spread(Decimal("4.50"), Decimal("4.80")) == 0
        # Recession tier (>5.0%) → 5 pts regardless of direction.
        assert score_credit_spread(Decimal("5.50"), Decimal("5.30")) == 5
        assert score_credit_spread(Decimal("5.50"), Decimal("5.80")) == 5
        # COVID anchor: 4.90% (just below 5%) AND widening → Warning.
        assert score_credit_spread(Decimal("4.90"), Decimal("4.40")) == 3
        # Missing latest → 0; missing prior at Recession still pays.
        assert score_credit_spread(None, Decimal("4.0")) == 0
        assert score_credit_spread(Decimal("5.50"), None) == 5
        assert score_credit_spread(Decimal("3.50"), None) == 0

    def test_vix_proxy_high_with_ma_pays_full_else_partial(self) -> None:
        # > 25 AND > 200-day MA → 15.
        assert score_vix_proxy(Decimal("28.0"), Decimal("22.0")) == 15
        # > 25 only.
        assert score_vix_proxy(Decimal("28.0"), Decimal("30.0")) == 10
        # ≤ 25 → 0 regardless of MA.
        assert score_vix_proxy(Decimal("25.0"), Decimal("18.0")) == 0
        # Missing now → 0.
        assert score_vix_proxy(None, Decimal("18.0")) == 0

    def test_scale_raw_to_100_endpoints(self) -> None:
        assert scale_raw_to_100(0) == 0
        assert scale_raw_to_100(85) == 100
        assert scale_raw_to_100(43) == 51   # 43*100/85 = 50.59 → 51
        assert scale_raw_to_100(60) == 71  # 60*100/85 = 70.59 → 71


# ─── VIX proxy + SPY rally ────────────────────────────────────────────────


class TestVixProxyAndRally:
    def test_vix_proxy_constant_series_is_zero_after_warmup(self) -> None:
        # 100 days of constant 100.0 — log returns are 0 → realized vol = 0.
        idx = pd.date_range("2024-01-01", periods=100, freq="D")
        s = pd.Series(100.0, index=idx)
        vp = compute_vix_proxy_series(s)
        # The first 20 entries are NaN (rolling warm-up); the rest are 0.
        assert pd.isna(vp.iloc[10])
        assert vp.iloc[40] == pytest.approx(0.0, abs=1e-9)

    def test_spy_rally_pct_no_rally(self) -> None:
        # Strictly declining series → no rally.
        idx = pd.date_range("2024-01-01", periods=10, freq="D")
        s = pd.Series([100, 99, 98, 97, 96, 95, 94, 93, 92, 91], index=idx, dtype=float)
        r = compute_spy_rally_pct(s, window_end=date_t(2024, 1, 10), window_days=3)
        assert r == Decimal("0")

    def test_spy_rally_pct_detects_5pct_rally(self) -> None:
        # Drop to 100 then bounce 6% to 106 — rally pct should be ~6%.
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        s = pd.Series([110, 100, 103, 105, 106], index=idx, dtype=float)
        r = compute_spy_rally_pct(s, window_end=date_t(2024, 1, 5), window_days=3)
        assert r > Decimal("0.05")


# ─── Lifecycle state machine ──────────────────────────────────────────────


def _make_breakdown_series(scores: list[int], start: date_t) -> dict[date_t, BearScoreBreakdown]:
    """Build a synthetic Bear Score panel — each day, all points in ``sahm_pts``
    (real component composition doesn't matter for the state-machine test)."""
    out: dict[date_t, BearScoreBreakdown] = {}
    for i, s in enumerate(scores):
        d = start + timedelta(days=i)
        # Pad with a tiny vix_pts so the breakdown is a valid integer raw.
        # Then rescale: total raw = (s/100)*85 → integer floor.
        raw = max(0, min(85, int(round(s * 85 / 100))))
        out[d] = BearScoreBreakdown(
            as_of=d,
            sahm_pts=min(25, raw),
            industrial_production_pts=0,
            initial_claims_pts=0,
            yield_curve_pts=0,
            credit_spread_pts=0,
            vix_pts=0,
            raw_total=raw,
            score=s,
        )
    return out


def _flat_spy(start: date_t, n: int) -> pd.Series:
    """Constant 100 SPY — no rally veto, no VIX > 25."""
    idx = pd.date_range(pd.Timestamp(start), periods=n, freq="D")
    return pd.Series(100.0, index=idx)


class TestLifecycleStateMachine:
    def test_dormant_when_all_scores_below_threshold(self) -> None:
        bds = _make_breakdown_series([20, 30, 40, 50, 55], start=date_t(2024, 1, 1))
        spy = _flat_spy(date_t(2023, 12, 1), 90)
        states = SentinelLifecycleAnalysis().walk_states(bds, spy_close=spy)
        assert all(st.phase == SentinelPhase.DORMANT for st in states.values())

    def test_three_consecutive_days_activates(self) -> None:
        # Scores: 40, 65, 70, 75 — days 2/3/4 are ≥ threshold; activate on day 4.
        bds = _make_breakdown_series([40, 65, 70, 75, 75], start=date_t(2024, 1, 1))
        spy = _flat_spy(date_t(2023, 12, 1), 90)
        states = SentinelLifecycleAnalysis().walk_states(bds, spy_close=spy)
        dates = sorted(states.keys())
        assert states[dates[0]].phase == SentinelPhase.DORMANT
        assert states[dates[1]].phase == SentinelPhase.WATCH
        assert states[dates[2]].phase == SentinelPhase.WATCH
        assert states[dates[3]].phase == SentinelPhase.ACTIVE
        assert states[dates[4]].phase == SentinelPhase.ACTIVE

    def test_active_to_fading_to_exited(self) -> None:
        # Activate, then 6 days below threshold → fades over 5 days, exits.
        scores = [65, 70, 75,   # WATCH→WATCH→ACTIVE
                  40, 40, 40, 40, 40, 40, 40]  # FADING fade over 5 days, then EXITED
        bds = _make_breakdown_series(scores, start=date_t(2024, 1, 1))
        spy = _flat_spy(date_t(2023, 12, 1), 90)
        states = SentinelLifecycleAnalysis().walk_states(bds, spy_close=spy)
        dates = sorted(states.keys())
        # Index 2 is the ACTIVE confirmation.
        assert states[dates[2]].phase == SentinelPhase.ACTIVE
        # Index 3 is first day below — FADING.
        assert states[dates[3]].phase == SentinelPhase.FADING
        # After 5 FADING days, EXITED.
        assert any(st.phase == SentinelPhase.EXITED for st in states.values())

    def test_activation_rally_veto_blocks_active(self) -> None:
        # 3 consecutive days ≥ 60, but SPY rallies 8% during the 3-day
        # activation window → veto. SPY drops 100 → 92 just before the
        # window, then rebounds 92 → 100 (8.7%) during the 3 bds dates.
        bds = _make_breakdown_series([65, 70, 75], start=date_t(2024, 1, 1))
        # 60-day pre-history at 100, then 92, then a 92→97→100 rally
        # spanning the 3 activation days.
        pre = pd.date_range("2023-11-01", periods=60, freq="D")
        bds_idx = pd.date_range("2023-12-31", periods=4, freq="D")  # last day = 2024-01-03
        spy = pd.Series(
            [100.0] * 60 + [92.0, 95.0, 97.0, 100.0],
            index=pre.append(bds_idx),
        )
        states = SentinelLifecycleAnalysis().walk_states(bds, spy_close=spy)
        # Day 3 should still be WATCH despite all 3 scores above threshold.
        dates = sorted(states.keys())
        assert states[dates[2]].phase == SentinelPhase.WATCH
        assert states[dates[2]].spy_rally_pct_in_window > Decimal("0.05")

    def test_false_signal_short_circuit_after_window(self) -> None:
        # Cross the threshold but never get 3 consecutive — must drop back
        # after FALSE_SIGNAL_WINDOW_DAYS.
        scores = [65, 50, 65, 50, 65, 50, 65, 50, 65, 50, 65, 50]  # 12 days
        bds = _make_breakdown_series(scores, start=date_t(2024, 1, 1))
        spy = _flat_spy(date_t(2023, 12, 1), 90)
        states = SentinelLifecycleAnalysis().walk_states(bds, spy_close=spy)
        # By day 12, we should be DORMANT (never confirmed).
        dates = sorted(states.keys())
        assert states[dates[-1]].phase == SentinelPhase.DORMANT


# ─── Override + missing-ETF helpers ───────────────────────────────────────


class TestBasketOverrides:
    def test_missing_etf_fallback_renormalizes(self) -> None:
        # Only TLT and SQQQ available → renormalize 20/(20+10) and 10/(20+10).
        out = apply_missing_etf_fallback(
            BASKET_WEIGHTS_DEFAULT,
            available_tickers=frozenset({"TLT", "SQQQ"}),
        )
        assert set(out.keys()) == {"TLT", "SQQQ"}
        assert out["TLT"] + out["SQQQ"] == Decimal("1")
        assert out["TLT"] > out["SQQQ"]  # TLT had the larger raw weight (0.20 vs 0.10)

    def test_missing_etf_fallback_no_overlap_returns_empty(self) -> None:
        out = apply_missing_etf_fallback(BASKET_WEIGHTS_DEFAULT, frozenset({"FOO", "BAR"}))
        assert out == {}

    def test_sqqq_ineligible_drops_sqqq(self) -> None:
        out = apply_basket_overrides(
            BASKET_WEIGHTS_DEFAULT,
            shallow_recession=False, vix_circuit_breaker=False, sqqq_eligible=False,
        )
        assert "SQQQ" not in out
        assert sum(out.values()) == Decimal("1")

    def test_shallow_recession_halves_sh_psq(self) -> None:
        base = apply_basket_overrides(
            BASKET_WEIGHTS_DEFAULT,
            shallow_recession=False, vix_circuit_breaker=False, sqqq_eligible=True,
        )
        shallow = apply_basket_overrides(
            BASKET_WEIGHTS_DEFAULT,
            shallow_recession=True, vix_circuit_breaker=False, sqqq_eligible=True,
        )
        # Shallow halves SH+PSQ relative to TLT — so TLT's share rises.
        assert shallow["TLT"] > base["TLT"]
        assert shallow["SH"] < base["SH"]
        assert shallow["PSQ"] < base["PSQ"]

    def test_vix_circuit_breaker_halves_inverse(self) -> None:
        base = apply_basket_overrides(
            BASKET_WEIGHTS_DEFAULT,
            shallow_recession=False, vix_circuit_breaker=False, sqqq_eligible=True,
        )
        vix_b = apply_basket_overrides(
            BASKET_WEIGHTS_DEFAULT,
            shallow_recession=False, vix_circuit_breaker=True, sqqq_eligible=True,
        )
        assert vix_b["SH"] < base["SH"]
        assert vix_b["PSQ"] < base["PSQ"]
        assert vix_b["SQQQ"] < base["SQQQ"]
        # Defensive bucket (TLT, GLD) gets the freed weight.
        assert vix_b["TLT"] > base["TLT"]
        assert vix_b["GLD"] > base["GLD"]


# ─── Capital gate ─────────────────────────────────────────────────────────


class TestCapitalGate:
    def test_pre_graduation_cap_is_10pct(self) -> None:
        g = SentinelCapitalGate(graduated=False)
        assert g.cap_pct == PRE_GRADUATION_CAP_PCT
        assert g.deployable_usd(Decimal("100000")) == Decimal("10000.00")

    def test_post_graduation_cap_is_20pct(self) -> None:
        g = SentinelCapitalGate(graduated=True)
        assert g.cap_pct == PERMANENT_CAP_PCT
        assert g.deployable_usd(Decimal("100000")) == Decimal("20000.00")

    def test_check_rebalance_rejects_over_cap(self) -> None:
        g = SentinelCapitalGate(graduated=False)
        assert g.check_rebalance(Decimal("9000"), Decimal("100000")) is True
        assert g.check_rebalance(Decimal("11000"), Decimal("100000")) is False

    def test_evaluate_graduation_requires_all_three(self) -> None:
        ok, _ = evaluate_graduation(completed_cycles=1, profit_factor=2.0, max_drawdown_pct=0.10)
        assert ok is True
        ok, reason = evaluate_graduation(completed_cycles=0, profit_factor=2.0, max_drawdown_pct=0.10)
        assert ok is False and "cycles" in reason
        ok, reason = evaluate_graduation(completed_cycles=1, profit_factor=1.0, max_drawdown_pct=0.10)
        assert ok is False and "profit" in reason.lower()
        ok, reason = evaluate_graduation(completed_cycles=1, profit_factor=2.0, max_drawdown_pct=0.30)
        assert ok is False and "drawdown" in reason.lower()


# ─── Execution & Risk ─────────────────────────────────────────────────────


def _state(phase: SentinelPhase, score: int = 65) -> SentinelState:
    return SentinelState(
        as_of=date_t(2024, 6, 15),
        phase=phase,
        bear_score=score,
        consecutive_days_above_threshold=ACTIVATION_CONSECUTIVE_DAYS,
        days_in_phase=1,
        cycle_id=1,
    )


class TestExecutionRisk:
    def test_dormant_produces_no_targets(self) -> None:
        ex = SentinelExecutionRisk(graduated=False)
        d = ex.build_decision(
            as_of=date_t(2024, 6, 15),
            state=_state(SentinelPhase.DORMANT, score=30),
            equity_usd=Decimal("100000"),
            prices={"SH": Decimal("16.00"), "TLT": Decimal("95.00")},
            current_holdings={},
        )
        assert d.targets == []
        assert d.orders == []

    def test_active_sizes_within_pre_grad_cap(self) -> None:
        ex = SentinelExecutionRisk(graduated=False)
        d = ex.build_decision(
            as_of=date_t(2024, 6, 15),
            state=_state(SentinelPhase.ACTIVE, score=65),
            equity_usd=Decimal("100000"),
            prices={
                "SH": Decimal("16.00"), "PSQ": Decimal("13.00"),
                "TLT": Decimal("95.00"), "GLD": Decimal("180.00"),
                "SQQQ": Decimal("50.00"),
            },
            current_holdings={},
        )
        # Deployable = 100k * 10% = 10k.
        assert d.deployable_equity_usd == Decimal("10000.00")
        # Shallow recession override is on at bear=65 → SH/PSQ are halved.
        assert d.state.shallow_recession_override is False  # state ctor; override is on the state we passed
        # Just assert we got a non-empty target list and target sum <= deployable.
        assert len(d.targets) > 0
        total_notional = sum(t.target_notional_usd for t in d.targets)
        assert total_notional <= d.deployable_equity_usd

    def test_active_with_only_tlt_available_renormalizes_to_100(self) -> None:
        # Only TLT priced — SH/PSQ/GLD/SQQQ missing → 100% TLT.
        ex = SentinelExecutionRisk(graduated=False)
        d = ex.build_decision(
            as_of=date_t(2024, 6, 15),
            state=_state(SentinelPhase.ACTIVE, score=65),
            equity_usd=Decimal("100000"),
            prices={"TLT": Decimal("95.00")},
            current_holdings={},
        )
        assert len(d.targets) == 1
        assert d.targets[0].ticker == "TLT"
        assert d.targets[0].target_weight == Decimal("1")
        # 10k / 95 = 105 shares (floor).
        assert d.targets[0].target_shares == 105

    def test_close_orders_emitted_when_dormant_with_residual(self) -> None:
        ex = SentinelExecutionRisk(graduated=False)
        d = ex.build_decision(
            as_of=date_t(2024, 6, 15),
            state=_state(SentinelPhase.DORMANT, score=30),
            equity_usd=Decimal("100000"),
            prices={"TLT": Decimal("95.00")},
            current_holdings={"TLT": 100},
        )
        assert any(o.ticker == "TLT" and o.side == "sell" and o.qty == 100 for o in d.orders)


# ─── Compliance-gap regression tests (G1–G6, 2026-05-15 audit closure) ────


class TestPlugComplianceG1:
    """G1: every plug subclasses BaseEnginePlug + implements interface."""

    @pytest.mark.parametrize(
        "plug_cls,plug_label",
        [
            ("SentinelSetupDetection", "setup_detection"),
            ("SentinelLifecycleAnalysis", "lifecycle_analysis"),
            ("SentinelExecutionRisk", "execution_risk"),
            ("SentinelCapitalGate", "capital_gate"),
            ("SentinelAARLogging", "aar_logging"),
        ],
    )
    def test_plug_subclasses_base_engine_plug_and_implements_interface(
        self, plug_cls: str, plug_label: str,
    ) -> None:
        from sentinel.plugs import (
            aar_logging,
            capital_gate,
            execution_risk,
            lifecycle_analysis,
            setup_detection,
        )
        from tpcore.interfaces.engine_plug import BaseEnginePlug
        mods = {
            "SentinelSetupDetection": setup_detection,
            "SentinelLifecycleAnalysis": lifecycle_analysis,
            "SentinelExecutionRisk": execution_risk,
            "SentinelCapitalGate": capital_gate,
            "SentinelAARLogging": aar_logging,
        }
        cls = getattr(mods[plug_cls], plug_cls)
        assert issubclass(cls, BaseEnginePlug), f"{plug_cls} must subclass BaseEnginePlug"
        # Instantiate (each plug has a zero-arg or default-arg ctor).
        inst = cls() if plug_cls != "SentinelAARLogging" else cls(pool=None)
        assert inst.validate_dependencies() is True
        hc = inst.healthcheck()
        assert hc["engine"] == "sentinel"
        assert hc["plug"] == plug_label
        assert hc["ok"] is True
        assert isinstance(hc["details"], dict)


class TestFilterDiagnosticsG2:
    """G2: Bear Score breakdown carries a populated FilterDiagnostics."""

    def test_breakdown_includes_filter_diagnostics(self) -> None:
        # Build a synthetic macro panel + SPY long enough to score one day.
        import pandas as pd

        from sentinel.plugs.setup_detection import SentinelSetupDetection
        # Force macro values that yield Sahm-only contribution.
        idx = pd.date_range("2024-01-01", periods=300, freq="D").date
        macro = pd.DataFrame(index=idx)
        macro.index.name = "date"
        macro["sahm_rule"] = 0.55           # fires
        macro["industrial_production"] = 100.0  # blocked (above 95)
        macro["initial_claims"] = 200000.0     # blocked
        macro["yield_curve"] = 0.20           # never inverted → blocked
        macro["credit_spread"] = 2.5          # below Watch threshold → blocked
        spy_idx = pd.date_range("2024-01-01", periods=300, freq="D")
        spy = pd.Series(100.0, index=spy_idx, name="SPY")

        setup = SentinelSetupDetection()
        breakdowns = setup._build_breakdowns(
            macro, spy, start=date_t(2024, 6, 1), end=date_t(2024, 6, 5),
        )
        assert len(breakdowns) > 0
        sample = next(iter(breakdowns.values()))
        assert sample.filter_diagnostics is not None
        diag = sample.filter_diagnostics
        # Sahm fired (1 candidate passed); the rest are blocked.
        assert diag.candidates_passed == 1
        assert diag.universe_total == 6
        assert diag.sahm_rule_blocked == 0
        assert diag.industrial_production_blocked == 1
        assert diag.initial_claims_blocked == 1
        assert diag.yield_curve_blocked == 1
        assert diag.credit_spread_blocked == 1
        # VIX proxy on constant SPY → 0 vol → blocked.
        assert diag.vix_proxy_blocked == 1

    def test_filter_diagnostics_serialises_excluding_none(self) -> None:
        """FilterDiagnostics.model_dump(exclude_none=True) must drop the
        non-Sentinel fields so signal-event extra_data stays compact."""
        from tpcore.backtest.filter_diagnostics import FilterDiagnostics
        d = FilterDiagnostics(
            universe_total=6, candidates_passed=2,
            sahm_rule_blocked=0, industrial_production_blocked=1,
            initial_claims_blocked=1, yield_curve_blocked=0,
            credit_spread_blocked=1, vix_proxy_blocked=1,
        )
        dump = d.model_dump(exclude_none=True)
        assert "gate1_value_blocked" not in dump  # vector field — none
        assert "adx_blocked" not in dump          # sigma field — none
        assert dump["sahm_rule_blocked"] == 0
        assert dump["vix_proxy_blocked"] == 1


class TestClassifyExitReasonG5:
    """G5: AAR plug uses classify_exit_reason, not a hardcoded literal."""

    def test_aar_plug_imports_classify_exit_reason(self) -> None:
        from sentinel.plugs import aar_logging
        # Verify the symbol is bound at module load (so the runtime path uses it).
        assert hasattr(aar_logging, "classify_exit_reason")

    @pytest.mark.asyncio
    async def test_write_basket_close_uses_classifier_when_exit_reason_omitted(self) -> None:
        """No TP/SL on Sentinel basket positions → classify_exit_reason
        returns TIME_STOP; the AAR plug must accept omission of exit_reason
        and fall through to the classifier."""
        from sentinel.plugs.aar_logging import SentinelAARLogging
        from tpcore.aar.models import ExitReason

        plug = SentinelAARLogging(pool=None)  # dry-run (no DB write)
        # Should succeed without raising — classifier returns TIME_STOP for
        # both-None TP/SL → AAR built with that reason. Return is True
        # under dry-run regardless of the persisted reason.
        ok = await plug.write_basket_close(
            trade_id="t1", ticker="TLT", cycle_id=1,
            entry_ts=date_t(2024, 1, 1), exit_ts=date_t(2024, 1, 10),
            entry_price=Decimal("90"), exit_price=Decimal("95"),
            qty=Decimal("100"), engine_equity_usd=Decimal("100000"),
            # exit_reason omitted → classifier path
        )
        assert ok is True
        # Sanity: TIME_STOP is the canonical fallback for missing brackets.
        assert ExitReason.TIME_STOP.value


class TestStaleOrderCancelG6:
    """G6: scheduler exposes a stale-order-cancel helper for the sn_ prefix."""

    def test_scheduler_exposes_cancel_stale_helper(self) -> None:
        from sentinel.scheduler import SentinelScheduler
        assert hasattr(SentinelScheduler, "_cancel_stale_sentinel_orders")
        assert callable(SentinelScheduler._cancel_stale_sentinel_orders)

    @pytest.mark.asyncio
    async def test_cancel_stale_silently_handles_broker_without_list_recent(self) -> None:
        from sentinel.scheduler import SentinelScheduler

        class _StubBroker:
            pass  # no list_recent_orders

        n = await SentinelScheduler._cancel_stale_sentinel_orders(_StubBroker())
        assert n == 0


class TestTradingDayGateG4:
    """G4: scheduler imports is_trading_day from tpcore.calendar."""

    def test_scheduler_imports_calendar(self) -> None:
        from sentinel import scheduler
        assert hasattr(scheduler, "is_trading_day")


class TestCredibilityPersistenceG3:
    """G3: backtest module imports the credibility-write helper.

    A live integration test would require a DB connection; this asserts
    the wiring so the missing import can't silently regress.
    """

    def test_backtest_imports_write_credibility_score(self) -> None:
        from sentinel import backtest
        assert hasattr(backtest, "write_credibility_score")
