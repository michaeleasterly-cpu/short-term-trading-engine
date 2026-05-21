"""Frozen + extra='forbid' invariant tests for the LLM finder models.

Per spec §4 + §10.1. Each model must:
- Reject extra fields (extra='forbid').
- Be frozen (assignment raises).
- Validate field constraints (range, length, regime_tuple_id SHA12).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tpcore.lab.llm_finder import (
    ANALYSIS_TURN_QUOTA,
    BLEED_CAP_PER_ENGINE_USD,
    DEFAULT_COST_BPS_ROUNDTRIP_T1,
    DEFAULT_COST_BPS_ROUNDTRIP_T2,
    EDGE_FINDER_RUN_QUOTA,
    GLOBAL_BLEED_PAUSE_THRESHOLD_USD,
    GLOBAL_BLEED_RESUME_THRESHOLD_USD,
    GLOBAL_FINDER_BLEED_CAP_USD,
    INACTIVITY_AUTO_RETIRE_SESSIONS,
    MANDATORY_REFERENCE_BUNDLES,
    MAX_SNAPSHOT_BYTES,
    MAX_TOOL_CALLS_PER_TURN,
    MIN_TRADE_COUNT_FOR_NO_VERDICT,
    PERSONA_VERSION,
    AnalysisRequest,
    AnalysisResult,
    CalendarContext,
    EvidenceRef,
    FinderRun,
    LedgerEntry,
    LiveOutcome,
    MarketRegime,
    MarketSnapshot,
    NumericSummary,
    ProposedSpec,
    RosterTarget,
    ToolCall,
    ToolResult,
)
from tpcore.lab.llm_finder.models import _compute_regime_tuple_id

# ───────────────────────── constants ─────────────────────────


def test_constants_pinned_per_spec() -> None:
    """Sanity-check constant values (spec §2 + §4.5)."""
    assert EDGE_FINDER_RUN_QUOTA == 3
    assert ANALYSIS_TURN_QUOTA == 10
    assert MAX_TOOL_CALLS_PER_TURN == 4
    assert MAX_SNAPSHOT_BYTES == 512 * 1024
    assert BLEED_CAP_PER_ENGINE_USD == 5_000.0
    assert GLOBAL_FINDER_BLEED_CAP_USD == 15_000.0
    assert GLOBAL_BLEED_PAUSE_THRESHOLD_USD == 12_000.0
    assert GLOBAL_BLEED_RESUME_THRESHOLD_USD == 7_500.0
    assert INACTIVITY_AUTO_RETIRE_SESSIONS == 60
    assert MIN_TRADE_COUNT_FOR_NO_VERDICT == 30
    assert DEFAULT_COST_BPS_ROUNDTRIP_T1 == 8
    assert DEFAULT_COST_BPS_ROUNDTRIP_T2 == 12
    assert PERSONA_VERSION == "v2.0"


def test_mandatory_bundles_present() -> None:
    """The 3 mandatory-always-include bundles are pinned (spec §3.1 + §7)."""
    assert MANDATORY_REFERENCE_BUNDLES == (
        "dsr_ntrials_discipline",
        "regime_aware_trading",
        "market_structure_primer",
    )


# ───────────────────────── MarketRegime ─────────────────────────


def _regime(
    vol: str = "normal",
    trend: str = "range",
    macro: str = "expansion",
    sentiment: str = "neutral",
    cycle: tuple = ("normal",),
) -> MarketRegime:
    return MarketRegime(
        vol_regime=vol,  # type: ignore[arg-type]
        trend_regime=trend,  # type: ignore[arg-type]
        macro_regime=macro,  # type: ignore[arg-type]
        sentiment_regime=sentiment,  # type: ignore[arg-type]
        cycle_position=cycle,
        regime_tuple_id=_compute_regime_tuple_id(vol, trend, macro, sentiment),
    )


def test_market_regime_tuple_id_validates() -> None:
    r = _regime()
    assert len(r.regime_tuple_id) == 12


def test_market_regime_rejects_wrong_tuple_id() -> None:
    with pytest.raises(ValidationError, match="regime_tuple_id"):
        MarketRegime(
            vol_regime="normal",
            trend_regime="range",
            macro_regime="expansion",
            sentiment_regime="neutral",
            cycle_position=("normal",),
            regime_tuple_id="DEADBEEF1234",
        )


def test_market_regime_frozen() -> None:
    r = _regime()
    with pytest.raises(ValidationError):
        r.vol_regime = "crisis"  # type: ignore[misc]


def test_market_regime_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        MarketRegime(
            vol_regime="normal",
            trend_regime="range",
            macro_regime="expansion",
            sentiment_regime="neutral",
            cycle_position=("normal",),
            regime_tuple_id=_compute_regime_tuple_id(
                "normal", "range", "expansion", "neutral"
            ),
            extra_field="forbidden",  # type: ignore[call-arg]
        )


def test_market_regime_vol_states_4() -> None:
    """Reconciled with primer §8 — 4 states (post-fold fix #8b)."""
    for vol in ("calm", "normal", "stress", "crisis"):
        _regime(vol=vol)


def test_market_regime_trend_states_3() -> None:
    """Reconciled with primer §8 — 3 states (post-fold fix #8b)."""
    for trend in ("range", "trend_up", "trend_down"):
        _regime(trend=trend)


def test_market_regime_macro_states_3() -> None:
    """Reconciled with primer §8 — 3 states (post-fold fix #8b)."""
    for macro in ("expansion", "slowing", "contraction"):
        _regime(macro=macro)


# ───────────────────────── ProposedSpec ─────────────────────────


def _proposed_spec(**overrides) -> ProposedSpec:
    base = {
        "candidate_name": "test_candidate",
        "target_engine": "momentum",
        "intent": "fold_existing",
        "primary_hypothesis": "Mean-reversion in range × calm regime.",
        "primary_metric": "cost_net_sharpe",
        "param_ranges": {"lookback_days": "5..20"},
        "rationale": "Cited tool_result_index=0 below.",
        "falsification_criterion": "Fails if cost_net_sharpe < 0.5 over holdout.",
        "expected_trials": 10,
        "cost_assumption_bps_roundtrip": 8.0,
        "regime_tuple_id": _compute_regime_tuple_id(
            "normal", "range", "expansion", "neutral"
        ),
        "analysis_evidence_refs": (
            EvidenceRef(
                tool_result_index=0,
                callable_name="OLS_HAC_NW",
                claimed_statistic="beta",
                claimed_value=0.42,
                claimed_threshold=None,
            ),
        ),
    }
    base.update(overrides)
    return ProposedSpec(**base)


def test_proposed_spec_happy_path() -> None:
    s = _proposed_spec()
    assert s.primary_metric == "cost_net_sharpe"
    assert s.cost_assumption_bps_roundtrip == 8.0


def test_proposed_spec_rejects_raw_sharpe_metric() -> None:
    """Only cost_net_sharpe accepted (post-fold fix #1 BLOCKS)."""
    with pytest.raises(ValidationError):
        _proposed_spec(primary_metric="sharpe")


def test_proposed_spec_no_engine_add_in_v1() -> None:
    """ENGINE-ADD via engine_template is v1.5 scope (post-fold fix #7 BLOCKS)."""
    with pytest.raises(ValidationError, match="v1.5"):
        _proposed_spec(rationale="Plans to use engine_add_path=True for new engine")


def test_proposed_spec_no_engine_add_field() -> None:
    """The field itself is forbidden (extra='forbid')."""
    with pytest.raises(ValidationError):
        ProposedSpec(
            candidate_name="x",
            target_engine="momentum",
            intent="fold_existing",
            primary_hypothesis="h",
            primary_metric="cost_net_sharpe",
            param_ranges={},
            rationale="r",
            falsification_criterion="f",
            expected_trials=1,
            cost_assumption_bps_roundtrip=8.0,
            regime_tuple_id=_compute_regime_tuple_id(
                "normal", "range", "expansion", "neutral"
            ),
            analysis_evidence_refs=(),
            engine_add_path=True,  # type: ignore[call-arg]
        )


def test_proposed_spec_cost_bps_bounded() -> None:
    """0 ≤ cost_bps ≤ 100."""
    with pytest.raises(ValidationError):
        _proposed_spec(cost_assumption_bps_roundtrip=-1.0)
    with pytest.raises(ValidationError):
        _proposed_spec(cost_assumption_bps_roundtrip=101.0)


# ───────────────────────── ToolCall whitelist ─────────────────────────


def test_tool_call_whitelist_includes_v1_callables() -> None:
    """7 base + 3 NEW (rolling_spearmanr/pearsonr, fama_macbeth) + cost_net_simulation (post-fold)."""
    for name in (
        "OLS_HAC_NW",
        "adfuller",
        "coint",
        "ARIMA_1_0_0",
        "spearmanr",
        "pearsonr",
        "ttest_1samp_HAC",
        "variance_ratio",
        "hurst_exponent",
        "ljung_box",
        "rolling_spearmanr",
        "rolling_pearsonr",
        "fama_macbeth",
        "cost_net_simulation",
    ):
        ToolCall(callable_name=name, args_json="{}")  # type: ignore[arg-type]


def test_tool_call_rejects_arch_garch() -> None:
    """v2 surface (subprocess sandbox) — not v1."""
    with pytest.raises(ValidationError):
        ToolCall(callable_name="arch_garch", args_json="{}")  # type: ignore[arg-type]


def test_tool_call_rejects_sklearn() -> None:
    """Permanently out of scope per spec §6.1."""
    with pytest.raises(ValidationError):
        ToolCall(callable_name="sklearn_lasso", args_json="{}")  # type: ignore[arg-type]


# ───────────────────────── ToolResult ─────────────────────────


def test_tool_result_exactly_one_payload() -> None:
    call = ToolCall(callable_name="OLS_HAC_NW", args_json="{}")
    # numeric_summary only — OK
    ToolResult(call=call, numeric_summary=NumericSummary(), error=None)
    # error only — OK
    ToolResult(call=call, numeric_summary=None, error="oops")
    # both — REJECT
    with pytest.raises(ValidationError, match="exactly one"):
        ToolResult(call=call, numeric_summary=NumericSummary(), error="oops")
    # neither — REJECT
    with pytest.raises(ValidationError, match="exactly one"):
        ToolResult(call=call, numeric_summary=None, error=None)


# ───────────────────────── AnalysisRequest quota ─────────────────────────


def test_analysis_request_turn_quota() -> None:
    """turn ∈ [1, ANALYSIS_TURN_QUOTA=10]."""
    AnalysisRequest(turn=1, rationale="r", tool_calls=())
    AnalysisRequest(turn=10, rationale="r", tool_calls=())
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=0, rationale="r", tool_calls=())
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=11, rationale="r", tool_calls=())


def test_analysis_request_tool_calls_cap() -> None:
    """≤ MAX_TOOL_CALLS_PER_TURN=4."""
    call = ToolCall(callable_name="OLS_HAC_NW", args_json="{}")
    AnalysisRequest(turn=1, rationale="r", tool_calls=(call,) * 4)
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=1, rationale="r", tool_calls=(call,) * 5)


# ───────────────────────── AnalysisResult cap ─────────────────────────


def test_analysis_result_specs_cap() -> None:
    """≤ EDGE_FINDER_RUN_QUOTA=3."""
    AnalysisResult(
        tool_results=(),
        proposed_specs=(_proposed_spec(),) * 3,
        finder_rationale="r",
    )
    with pytest.raises(ValidationError):
        AnalysisResult(
            tool_results=(),
            proposed_specs=(_proposed_spec(),) * 4,
            finder_rationale="r",
        )


# ───────────────────────── LiveOutcome (post-fold shape) ─────────────────────────


def test_live_outcome_post_fold_shape() -> None:
    """operator_verdict + auto_retire_reason present; no outcome_criterion_status."""
    lo = LiveOutcome(
        engine="momentum",
        as_of_session=date(2026, 5, 21),
        session_count=15,
        pnl_realised_total_usd=120.0,
        pnl_unrealised_total_usd=-30.0,
        sharpe_30d_net_costs_hac=0.6,
        max_single_session_drawdown_pct=0.012,
        cumulative_bleed_usd=200.0,
        trade_count_total=22,
        operator_verdict="none",
        auto_retire_triggered=False,
        auto_retire_reason="none",
    )
    assert lo.operator_verdict == "none"
    assert lo.auto_retire_reason == "none"


def test_live_outcome_auto_retire_reasons() -> None:
    """All 5 reason codes accepted (post-fold)."""
    for reason in (
        "none",
        "bleed_cap",
        "operator_failure",
        "inactivity_timeout",
        "global_bleed_cap",
    ):
        LiveOutcome(
            engine="momentum",
            as_of_session=date(2026, 5, 21),
            session_count=15,
            pnl_realised_total_usd=0.0,
            pnl_unrealised_total_usd=0.0,
            sharpe_30d_net_costs_hac=None,
            max_single_session_drawdown_pct=None,
            cumulative_bleed_usd=0.0,
            trade_count_total=0,
            operator_verdict="none",
            auto_retire_triggered=reason != "none",
            auto_retire_reason=reason,  # type: ignore[arg-type]
        )


def test_live_outcome_no_outcome_criterion_status_field() -> None:
    """Field removed post-fold; extra='forbid' rejects it."""
    with pytest.raises(ValidationError):
        LiveOutcome(
            engine="momentum",
            as_of_session=date(2026, 5, 21),
            session_count=0,
            pnl_realised_total_usd=0.0,
            pnl_unrealised_total_usd=0.0,
            sharpe_30d_net_costs_hac=None,
            max_single_session_drawdown_pct=None,
            cumulative_bleed_usd=0.0,
            trade_count_total=0,
            operator_verdict="none",
            auto_retire_triggered=False,
            auto_retire_reason="none",
            outcome_criterion_status="pending",  # type: ignore[call-arg]
        )


# ───────────────────────── LedgerEntry (constraint 17 + 20) ─────────────────────────


def test_ledger_entry_carries_aggregate_and_per_regime() -> None:
    """Aggregate ledger field (post-fold constraint 17 — hard fence)."""
    le = LedgerEntry(
        target_engine="momentum",
        regime_tuple_id=_compute_regime_tuple_id(
            "normal", "range", "expansion", "neutral"
        ),
        cumulative_n_trials_by_regime=5,
        cumulative_n_trials_aggregate=150,
        cumulative_analysis_turns_by_regime=22,
    )
    assert le.cumulative_n_trials_aggregate == 150
    assert le.cumulative_analysis_turns_by_regime == 22


# ───────────────────────── FinderRun ─────────────────────────


def test_finder_run_minimal() -> None:
    """Trigger Literals + provenance shape."""
    FinderRun(
        run_id=uuid4(),
        started_ts=datetime.now(UTC),
        completed_ts=None,
        trigger="operator_command",
        snapshot_session_date=date(2026, 5, 21),
        snapshot_regime_tuple_id=_compute_regime_tuple_id(
            "normal", "range", "expansion", "neutral"
        ),
        persona_version="v2.0",
        reference_bundles=("dsr_ntrials_discipline", "regime_aware_trading"),
        analysis_turn_count=3,
        proposed_spec_count=1,
        emitted_pr_urls=(),
        auto_merged_pr_urls=(),
        auto_issued_ecr_refs=(),
        rejection_reason=None,
    )


# ───────────────────────── MarketSnapshot composes ─────────────────────────


def test_market_snapshot_composes() -> None:
    """Snapshot accepts the 14+ ingested-table substrate (post-fold)."""
    regime = _regime()
    snap = MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 21),
        universe="sp500",
        market_regime=regime,
        calendar=CalendarContext(
            session_date=date(2026, 5, 21),
            is_earnings_season=False,
            is_fomc_week=False,
            is_opex_week=False,
            is_year_end_week=False,
            days_to_next_fomc=14,
            days_to_next_earnings_season=42,
        ),
        price_window=(),
        fundamentals=(),
        spreads=(),
        sentiment=(),
        macro=(),
        ledger_state=(),
        roster=(
            RosterTarget(
                engine="momentum",
                lifecycle_state="PAPER",
                primary_metric="SHARPE",
            ),
        ),
    )
    assert snap.market_regime.regime_tuple_id == regime.regime_tuple_id
