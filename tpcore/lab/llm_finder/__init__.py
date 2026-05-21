"""LLM edge-finder package — Task #25 Path B v1.0.

Public surface: models + constants. Per spec
``docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md``.

Engine-FREE: stdlib + pydantic v2 + structlog + tpcore.lab.ledger +
tpcore.engine_profile + tightly-scoped statsmodels/scipy imports
inside tool_sandbox.py only.
"""
from __future__ import annotations

# Quotas + caps — pinned per spec §2 (constraints 14-20) + §3.
EDGE_FINDER_RUN_QUOTA: int = 3
"""Max ProposedSpecs per finder run (spec §3.2 + §4.5)."""

ANALYSIS_TURN_QUOTA: int = 10
"""Max AnalysisRequest turns per run (spec §4.5)."""

MAX_TOOL_CALLS_PER_TURN: int = 4
"""Max ToolCalls per AnalysisRequest turn (spec §4.5)."""

MAX_SNAPSHOT_BYTES: int = 512 * 1024
"""MarketSnapshot serialised byte cap; fail-loud on overflow (spec §4.1)."""

BLEED_CAP_PER_ENGINE_USD: float = 5_000.0
"""Per-engine cumulative bleed cap; mechanical auto-retire (spec §2.15)."""

GLOBAL_FINDER_BLEED_CAP_USD: float = 15_000.0
"""Aggregate cap across all finder-emitted PAPER engines (spec §2.18)."""

GLOBAL_BLEED_PAUSE_THRESHOLD_USD: float = 12_000.0
"""80% of global cap — finder co-task auto-pauses (spec §2.18)."""

GLOBAL_BLEED_RESUME_THRESHOLD_USD: float = 7_500.0
"""50% of global cap — pause lifts (spec §2.18)."""

INACTIVITY_AUTO_RETIRE_SESSIONS: int = 60
"""NYSE sessions before flat-not-bleeding engine auto-retires (spec §2.19)."""

MIN_TRADE_COUNT_FOR_NO_VERDICT: int = 30
"""Trade floor below which inactivity-timeout fires (spec §2.19)."""

DEFAULT_COST_BPS_ROUNDTRIP_T1: int = 8
"""Default cost assumption for T1-liquidity proposals (spec §4.5)."""

DEFAULT_COST_BPS_ROUNDTRIP_T2: int = 12
"""Default cost assumption for T2-liquidity proposals (spec §4.5)."""

MANDATORY_REFERENCE_BUNDLES: tuple[str, ...] = (
    "dsr_ntrials_discipline",
    "regime_aware_trading",
    "market_structure_primer",
)
"""Always-include reference bundles regardless of --reference-bundle (spec §3.1 + §7)."""

PERSONA_VERSION: str = "v2.0"
"""Bumped for Path B reversal (spec §7.1)."""

from tpcore.lab.llm_finder.models import (  # noqa: E402
    AnalysisRequest,
    AnalysisResult,
    CalendarContext,
    EvidenceRef,
    FinderRun,
    FundRow,
    LedgerEntry,
    LiveOutcome,
    MacroRow,
    MarketRegime,
    MarketSnapshot,
    NumericSummary,
    PricePanelRow,
    ProposedSpec,
    RosterTarget,
    SentimentRow,
    SpreadObs,
    ToolCall,
    ToolResult,
)

__all__ = [
    "ANALYSIS_TURN_QUOTA",
    "AnalysisRequest",
    "AnalysisResult",
    "BLEED_CAP_PER_ENGINE_USD",
    "CalendarContext",
    "DEFAULT_COST_BPS_ROUNDTRIP_T1",
    "DEFAULT_COST_BPS_ROUNDTRIP_T2",
    "EDGE_FINDER_RUN_QUOTA",
    "EvidenceRef",
    "FinderRun",
    "FundRow",
    "GLOBAL_BLEED_PAUSE_THRESHOLD_USD",
    "GLOBAL_BLEED_RESUME_THRESHOLD_USD",
    "GLOBAL_FINDER_BLEED_CAP_USD",
    "INACTIVITY_AUTO_RETIRE_SESSIONS",
    "LedgerEntry",
    "LiveOutcome",
    "MANDATORY_REFERENCE_BUNDLES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_TOOL_CALLS_PER_TURN",
    "MIN_TRADE_COUNT_FOR_NO_VERDICT",
    "MacroRow",
    "MarketRegime",
    "MarketSnapshot",
    "NumericSummary",
    "PERSONA_VERSION",
    "PricePanelRow",
    "ProposedSpec",
    "RosterTarget",
    "SentimentRow",
    "SpreadObs",
    "ToolCall",
    "ToolResult",
]
