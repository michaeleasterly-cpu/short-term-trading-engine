"""Frozen Pydantic v2 models for the LLM-AAR critic — spec §3.

All models are frozen + ``extra='forbid'``. The LLM sees only these
schemas — never raw Postgres rows, repo paths, or live credentials.

Models follow spec §3.1 (AARFinding), §3.2 (AARCriticRun), and §2.2
(EnginePerformanceWindow) verbatim. Mirrors the discipline of
``tpcore/lab/llm_finder/models.py``.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ───────────────────────── Theme vocabulary (closed) ─────────────────────

AARTheme = Literal[
    "exit_timing",
    "entry_quality",
    "sizing_drift",
    "regime_conditional_perf",
    "exit_reason_skew",
    "rule_compliance_drift",
    "hold_duration_skew",
    "slippage_drift",
    "win_rate_decay",
]
"""Closed-vocabulary theme classes the critic emits (spec §3.1).

Closed Literal = LLM cannot invent new themes (fence #11). New theme
classes require operator-staged persona + code update.
"""

AARConfidence = Literal["low", "medium", "high"]
"""Confidence band mapped mechanically against evidence_aar_count (spec §3.1)."""

HoldBucket = Literal["0-1d", "1-3d", "3-7d", "7-21d", "21d+"]
"""Hold-duration buckets surfaced in EnginePerformanceWindow (spec §2.2)."""


# ───────────────────────── AARRowSummary (bounded substrate) ────────────


class AARRowSummary(BaseModel):
    """One-AAR slice surfaced to the LLM — ticker + outcome only.

    Strips trade_id (never seen by the LLM) and full aar_data jsonb.
    Mirrors the principle that the LLM sees aggregates + bounded
    representative samples, never raw DB rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    entry_session: date | None
    exit_session: date
    pnl_net_usd: Decimal
    exit_reason: Annotated[str, Field(min_length=1, max_length=64)]
    hold_sessions: int | None


# ───────────────────────── EnginePerformanceWindow ──────────────────────


class EnginePerformanceWindow(BaseModel):
    """Per-engine performance aggregate the critic consumes (spec §2.2).

    Deterministic input — assembled by ``payload_assembler.py`` from
    ``platform.aar_events`` BEFORE the LLM sees anything. Bounded by
    ``MAX_AAR_PAYLOAD_BYTES`` at the assembler level (fail-loud on overflow).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: Annotated[str, Field(min_length=1, max_length=32)]
    as_of_session: date
    trade_count_total: Annotated[int, Field(ge=0)]
    trade_count_window: Annotated[int, Field(ge=0)]
    pnl_net_total_usd: Decimal
    pnl_net_window_usd: Decimal
    win_rate_window: Annotated[float, Field(ge=0.0, le=1.0)]
    win_rate_total: Annotated[float, Field(ge=0.0, le=1.0)]
    # Exit-reason distributions are dict-of-string (the ExitReason value):
    exit_reason_distribution: dict[str, int]
    exit_reason_pnl_by_reason_usd: dict[str, Decimal]
    hold_duration_buckets: dict[HoldBucket, int]
    pnl_per_hold_bucket_usd: dict[HoldBucket, Decimal]
    slippage_bps_p50: float | None
    slippage_bps_p95: float | None
    rule_compliance_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    recent_aars: tuple[AARRowSummary, ...]

    @model_validator(mode="after")
    def _check_recent_aars_bounded(self) -> EnginePerformanceWindow:
        """recent_aars is capped at 20 entries (spec §2.2 — 20 most recent)."""
        if len(self.recent_aars) > 20:
            raise ValueError(
                f"recent_aars carries {len(self.recent_aars)} entries; "
                f"cap is 20 per spec §2.2"
            )
        return self


# ───────────────────────── AARFinding (LLM output contract) ─────────────


def _compute_finding_id(engine: str, theme: str, observation_session: date) -> str:
    """SHA-12 of (engine, theme, observation_session) — spec §3.1.

    Deterministic finding_id means re-emitting the same pattern on the
    same observation_session is idempotent (memstore write at the same
    path; existing finding overwritten with the latest LLM judgement).
    """
    payload = f"engine:{engine}|theme:{theme}|obs:{observation_session.isoformat()}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:12]


# Confidence-band thresholds (mechanical mapping — fence #12).
# Critic LLM picks confidence; the model_validator enforces the band.
_CONFIDENCE_LOW_RANGE: tuple[int, int] = (3, 7)
_CONFIDENCE_MEDIUM_RANGE: tuple[int, int] = (8, 20)
_CONFIDENCE_HIGH_MIN: int = 21


class AARFinding(BaseModel):
    """Single LLM-AAR pattern observation (spec §3.1).

    Frozen + extra='forbid'. Closed-vocabulary theme. Evidence-grounded
    (>=3 AARs supporting). Confidence cross-validated against
    evidence_aar_count band (fence #12, spec §6).

    The finding is ADVISORY ONLY. The finder may cite it in
    ProposedSpec.rationale but tool-call evidence still binds the SP-A gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: Annotated[str, Field(min_length=1, max_length=32)]
    finding_id: Annotated[str, Field(min_length=12, max_length=12)]
    theme: AARTheme
    pattern_observed: Annotated[str, Field(min_length=1, max_length=2048)]
    suggested_emission_axis: Annotated[str, Field(min_length=1, max_length=1024)]
    evidence_aar_count: Annotated[int, Field(ge=3)]
    evidence_window_sessions: Annotated[int, Field(ge=1, le=90)]
    confidence: AARConfidence
    observation_session: date
    persona_version: Annotated[str, Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def _check_finding_id_deterministic(self) -> AARFinding:
        """finding_id MUST be SHA-12 of (engine, theme, observation_session).

        Defense against LLM-emitted random finding_ids that would break
        memstore-path idempotency. The same (engine, theme, session) tuple
        always produces the same finding_id, so re-emitting overwrites
        rather than accumulating duplicates.
        """
        expected = _compute_finding_id(
            self.engine, self.theme, self.observation_session
        )
        if self.finding_id != expected:
            raise ValueError(
                f"finding_id={self.finding_id} does not match SHA-12 of "
                f"(engine={self.engine}, theme={self.theme}, "
                f"observation_session={self.observation_session.isoformat()}); "
                f"expected={expected}"
            )
        return self

    @model_validator(mode="after")
    def _check_confidence_band(self) -> AARFinding:
        """Cross-validate confidence against evidence_aar_count band.

        Fence #12 (spec §6). The LLM picks confidence; the validator
        enforces:
        - 'low':    3 <= evidence_aar_count <= 7
        - 'medium': 8 <= evidence_aar_count <= 20
        - 'high':   evidence_aar_count >= 21

        A 'high'-confidence finding from 3 AARs is structurally over-claimed
        and the model rejects it at construction time.
        """
        n = self.evidence_aar_count
        if self.confidence == "low":
            low, high = _CONFIDENCE_LOW_RANGE
            if not (low <= n <= high):
                raise ValueError(
                    f"confidence='low' requires evidence_aar_count in "
                    f"[{low},{high}]; got {n}"
                )
        elif self.confidence == "medium":
            low, high = _CONFIDENCE_MEDIUM_RANGE
            if not (low <= n <= high):
                raise ValueError(
                    f"confidence='medium' requires evidence_aar_count in "
                    f"[{low},{high}]; got {n}"
                )
        else:  # 'high'
            if n < _CONFIDENCE_HIGH_MIN:
                raise ValueError(
                    f"confidence='high' requires evidence_aar_count >= "
                    f"{_CONFIDENCE_HIGH_MIN}; got {n}"
                )
        return self


# ───────────────────────── AARCriticRun (provenance) ────────────────────


class AARCriticRun(BaseModel):
    """One AAR-critic run; provenance written to application_log (spec §3.2).

    Append-only via ``record_aar_critic_run``. Mirrors FinderRun shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    started_ts: datetime
    completed_ts: datetime
    trigger: Literal["nightly_cron", "operator_command"]
    as_of_session: date
    engines_examined: tuple[str, ...]
    findings_emitted: tuple[str, ...]
    persona_version: Annotated[str, Field(min_length=1, max_length=16)]
    rejection_reason: str | None = None


# ───────────────────────── Public helpers ──────────────────────────────


def compute_finding_id(
    engine: str, theme: str, observation_session: date
) -> str:
    """Public wrapper around the SHA-12 finding_id computation."""
    return _compute_finding_id(engine, theme, observation_session)


__all__ = [
    "AARConfidence",
    "AARCriticRun",
    "AARFinding",
    "AARRowSummary",
    "AARTheme",
    "EnginePerformanceWindow",
    "HoldBucket",
    "compute_finding_id",
]
