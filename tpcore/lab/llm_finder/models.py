"""Frozen Pydantic v2 models for the LLM edge-finder — Task #25 §4.

All models are frozen + ``extra='forbid'``. The LLM sees only these
schemas — never raw Postgres rows, repo paths, or live credentials.

Models follow spec §4 verbatim (see comments per field for spec
§-pointers). Post-fold (PR #232): cost_assumption_bps_roundtrip is
mandatory on ProposedSpec; engine_add_path REMOVED (v1.5 scope);
operator_verdict + auto_retire_reason added to LiveOutcome; aggregate
+ per-regime ledger fields on LedgerEntry.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ───────────────────────── MarketRegime + snapshot rows ─────────────────────


class MarketRegime(BaseModel):
    """The 5-axis regime decomposition (spec §4.2; reconciled with primer §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vol_regime: Literal["calm", "normal", "stress", "crisis"]
    trend_regime: Literal["range", "trend_up", "trend_down"]
    macro_regime: Literal["expansion", "slowing", "contraction"]
    sentiment_regime: Literal["extreme_bull", "neutral", "extreme_bear"]
    cycle_position: tuple[
        Literal[
            "earnings_season",
            "fomc_week",
            "opex_week",
            "year_end",
            "normal",
        ],
        ...,
    ]
    regime_tuple_id: Annotated[str, Field(min_length=12, max_length=12)]

    @model_validator(mode="after")
    def _check_tuple_id(self) -> MarketRegime:
        expected = _compute_regime_tuple_id(
            self.vol_regime,
            self.trend_regime,
            self.macro_regime,
            self.sentiment_regime,
        )
        if self.regime_tuple_id != expected:
            raise ValueError(
                f"regime_tuple_id={self.regime_tuple_id} does not match "
                f"SHA12 of axes; expected={expected}"
            )
        return self


def _compute_regime_tuple_id(
    vol: str,
    trend: str,
    macro: str,
    sentiment: str,
) -> str:
    """SHA12 hash of the 4 hash-eligible axes (cycle_position excluded; spec §4.2)."""
    axes_sorted = sorted([f"v:{vol}", f"t:{trend}", f"m:{macro}", f"s:{sentiment}"])
    digest = hashlib.sha256("|".join(axes_sorted).encode("utf-8")).hexdigest()
    return digest[:12]


class CalendarContext(BaseModel):
    """Per-session calendar metadata (spec §4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    is_earnings_season: bool
    is_fomc_week: bool
    is_opex_week: bool
    is_year_end_week: bool
    days_to_next_fomc: Annotated[int, Field(ge=0)]
    days_to_next_earnings_season: Annotated[int, Field(ge=0)]


class PricePanelRow(BaseModel):
    """Per-(ticker, session) bar (spec §4.1 — bounded substrate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    session_date: date
    adj_open: float
    adj_high: float
    adj_low: float
    adj_close: float
    volume: Annotated[int, Field(ge=0)]
    dollar_volume: Annotated[float, Field(ge=0.0)]
    log_return: float
    liquidity_tier: Literal["T1", "T2", "T3"]


class FundRow(BaseModel):
    """Latest-quarter fundamentals per ticker (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    fiscal_period_end: date
    revenue: float | None
    net_income: float | None
    eps_diluted: float | None
    book_value: float | None
    debt_to_equity: float | None
    pb_ratio: float | None


class SpreadObs(BaseModel):
    """Corwin-Schultz / Roll spread observations (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    session_date: date
    effective_spread_bps: Annotated[float, Field(ge=0.0)]
    roll_implied_spread_bps: Annotated[float, Field(ge=0.0)] | None


class SentimentRow(BaseModel):
    """AAII / ApeWisdom / Fear&Greed composite (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of_date: date
    aaii_bull_pct: float | None
    aaii_bear_pct: float | None
    aaii_neutral_pct: float | None
    fear_greed_score: Annotated[int, Field(ge=0, le=100)] | None
    apewisdom_mention_rank: int | None
    ticker: str | None


class MacroRow(BaseModel):
    """FRED macro indicators (spec §4.1 — 58-series substrate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    series_id: Annotated[str, Field(min_length=1, max_length=64)]
    observation_date: date
    value: float


# ───────────────────────── LedgerEntry + RosterTarget ───────────────────────


class LedgerEntry(BaseModel):
    """SP-A cumulative ledger state per target/regime (spec §4.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_engine: str
    regime_tuple_id: Annotated[str, Field(min_length=12, max_length=12)]
    cumulative_n_trials_by_regime: Annotated[int, Field(ge=0)]
    cumulative_n_trials_aggregate: Annotated[int, Field(ge=0)]
    cumulative_analysis_turns_by_regime: Annotated[int, Field(ge=0)]


class RosterTarget(BaseModel):
    """SP-B lab_targetable_engines() row (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    lifecycle_state: Literal["LAB", "PAPER", "LIVE", "RETIRED"]
    primary_metric: str  # LabPrimaryMetric enum name


# ───────────────────────── MarketSnapshot ───────────────────────────────────


class MarketSnapshot(BaseModel):
    """Phase A output — the only data the LLM sees (spec §4.1).

    Bounded by MAX_SNAPSHOT_BYTES; fail-loud on overflow per the
    serialised-size check in snapshot.py (not enforced here — this is
    the contract; the assembler enforces the cap).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_ts: datetime
    session_date: date
    universe: Literal["sp500", "sp1500", "rus3k"]
    market_regime: MarketRegime
    calendar: CalendarContext
    price_window: tuple[PricePanelRow, ...]
    fundamentals: tuple[FundRow, ...]
    spreads: tuple[SpreadObs, ...]
    sentiment: tuple[SentimentRow, ...]
    macro: tuple[MacroRow, ...]
    ledger_state: tuple[LedgerEntry, ...]
    roster: tuple[RosterTarget, ...]


# ───────────────────────── Tool sandbox + analysis loop ─────────────────────


class NumericSummary(BaseModel):
    """Bounded ToolResult payload — no raw arrays (spec §6.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coefficients: tuple[float, ...] = ()
    pvalues: tuple[float, ...] = ()
    statistic: float | None = None
    summary_text: Annotated[str, Field(max_length=4_096)] = ""
    extra: dict[str, float] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """LLM-emitted analysis tool call (spec §4.5 + §6.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    callable_name: Literal[
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
    ]
    args_json: Annotated[str, Field(max_length=16_000)]


class ToolResult(BaseModel):
    """Dispatcher result; either numeric_summary OR error (spec §6.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call: ToolCall
    numeric_summary: NumericSummary | None = None
    error: Annotated[str, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ToolResult:
        if (self.numeric_summary is None) == (self.error is None):
            raise ValueError(
                "ToolResult must carry exactly one of numeric_summary OR error"
            )
        return self


class EvidenceRef(BaseModel):
    """Citation linking a ProposedSpec claim to a tool result (spec §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_result_index: Annotated[int, Field(ge=0)]
    callable_name: str
    claimed_statistic: str
    claimed_value: float
    claimed_threshold: float | None


class ProposedSpec(BaseModel):
    """LLM emission — upstream of SP-G EmittedSpec (spec §4.5).

    Post-fold: ENGINE-ADD path moved to v1.5; cost_assumption_bps
    mandatory; regime_tuple_id pinned to the snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_name: Annotated[str, Field(min_length=1, max_length=64)]
    target_engine: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Literal["fold_existing", "promote_new"]
    primary_hypothesis: Annotated[str, Field(min_length=1, max_length=2_048)]
    primary_metric: Literal[
        "cost_net_sharpe",
    ]
    param_ranges: dict[str, str]
    rationale: Annotated[str, Field(min_length=1, max_length=4_096)]
    falsification_criterion: Annotated[str, Field(min_length=1, max_length=2_048)]
    expected_trials: Annotated[int, Field(ge=1, le=200)]
    cost_assumption_bps_roundtrip: Annotated[float, Field(ge=0.0, le=100.0)]
    regime_tuple_id: Annotated[str, Field(min_length=12, max_length=12)]
    analysis_evidence_refs: tuple[EvidenceRef, ...]

    @model_validator(mode="after")
    def _no_engine_add_in_v1(self) -> ProposedSpec:
        # ENGINE-ADD via engine_template is v1.5 scope (spec §4.5 + §9.2).
        # No engine_add_path field; the validator simply doesn't accept it
        # (extra='forbid'). This check is defensive against attempts to
        # smuggle it via the rationale string.
        if "engine_add_path" in self.rationale.lower():
            raise ValueError(
                "ENGINE-ADD via engine_template is v1.5 scope; "
                "v1 intent must be fold_existing or promote_new against "
                "an existing engine slot."
            )
        return self


class AnalysisRequest(BaseModel):
    """LLM → agent message (spec §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: Annotated[int, Field(ge=1, le=10)]
    rationale: Annotated[str, Field(min_length=1, max_length=4_096)]
    tool_calls: Annotated[tuple[ToolCall, ...], Field(max_length=4)]


class AnalysisResult(BaseModel):
    """Final LLM emission for the run (spec §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_results: tuple[ToolResult, ...]
    proposed_specs: Annotated[tuple[ProposedSpec, ...], Field(max_length=3)]
    finder_rationale: Annotated[str, Field(min_length=1, max_length=8_192)]


# ───────────────────────── Provenance + outcome models ──────────────────────


class FinderRun(BaseModel):
    """Run-level provenance row (spec §4.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    started_ts: datetime
    completed_ts: datetime | None
    trigger: Literal[
        "operator_command",
        "ledger_capacity_event",
        "regime_change_event",
    ]
    snapshot_session_date: date
    snapshot_regime_tuple_id: Annotated[str, Field(min_length=12, max_length=12)]
    persona_version: str
    reference_bundles: tuple[str, ...]
    analysis_turn_count: Annotated[int, Field(ge=0, le=10)]
    proposed_spec_count: Annotated[int, Field(ge=0, le=3)]
    emitted_pr_urls: tuple[str, ...]
    auto_merged_pr_urls: tuple[str, ...]
    auto_issued_ecr_refs: tuple[str, ...]
    rejection_reason: str | None


class LiveOutcome(BaseModel):
    """Phase E rolling snapshot per finder-emitted PAPER engine (spec §4.6).

    Post-fold: descriptive stats for the §12 dashboard surface;
    operator_verdict + auto_retire_reason added; outcome_criterion_
    status REMOVED (operator-discretion via §12, not auto-decision).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    as_of_session: date
    session_count: Annotated[int, Field(ge=0)]
    pnl_realised_total_usd: float
    pnl_unrealised_total_usd: float
    sharpe_30d_net_costs_hac: float | None
    max_single_session_drawdown_pct: float | None
    cumulative_bleed_usd: Annotated[float, Field(ge=0.0)]
    trade_count_total: Annotated[int, Field(ge=0)]
    operator_verdict: Literal["none", "success", "failure"]
    auto_retire_triggered: bool
    auto_retire_reason: Literal[
        "none",
        "bleed_cap",
        "operator_failure",
        "inactivity_timeout",
        "global_bleed_cap",
    ]
