"""LLM-AAR model invariants — spec §3 + §6 fence #4 (evidence) + #11 (closed-vocab) + #12 (confidence band)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tpcore.lab.llm_aar.models import (
    AARCriticRun,
    AARFinding,
    AARRowSummary,
    EnginePerformanceWindow,
    compute_finding_id,
)

# ───────────────────────── AARFinding ─────────────────────────


def _make_finding(**overrides: object) -> AARFinding:
    """Build a valid finding; overrides patch fields for negative tests."""
    defaults: dict[str, object] = {
        "engine": "catalyst",
        "theme": "exit_timing",
        "pattern_observed": "Time-stop exits dominate negative-P&L outcomes.",
        "suggested_emission_axis": "Test 10-session hold variant.",
        "evidence_aar_count": 9,
        "evidence_window_sessions": 90,
        "confidence": "medium",
        "observation_session": date(2026, 5, 22),
        "persona_version": "v1.0",
    }
    defaults.update(overrides)
    # finding_id is deterministic from (engine, theme, observation_session)
    fid = compute_finding_id(
        str(defaults["engine"]),
        str(defaults["theme"]),
        defaults["observation_session"],  # type: ignore[arg-type]
    )
    defaults["finding_id"] = fid
    return AARFinding(**defaults)  # type: ignore[arg-type]


def test_aar_finding_frozen_and_extra_forbid() -> None:
    f = _make_finding()
    # extra='forbid'
    with pytest.raises(ValidationError):
        AARFinding(
            engine="catalyst",
            finding_id=f.finding_id,
            theme="exit_timing",
            pattern_observed="x",
            suggested_emission_axis="y",
            evidence_aar_count=3,
            evidence_window_sessions=10,
            confidence="low",
            observation_session=date(2026, 5, 22),
            persona_version="v1.0",
            extra_field="reject me",  # type: ignore[call-arg]
        )
    # frozen=True
    with pytest.raises(ValidationError):
        f.engine = "vector"  # type: ignore[misc]


def test_aar_finding_id_deterministic() -> None:
    """Same (engine, theme, observation_session) → same finding_id."""
    f1 = _make_finding(engine="catalyst", theme="exit_timing", observation_session=date(2026, 5, 22))
    f2 = _make_finding(engine="catalyst", theme="exit_timing", observation_session=date(2026, 5, 22))
    assert f1.finding_id == f2.finding_id

    f3 = _make_finding(engine="catalyst", theme="entry_quality", observation_session=date(2026, 5, 22))
    assert f3.finding_id != f1.finding_id


def test_aar_finding_id_validator_rejects_mismatch() -> None:
    """A hand-built finding_id that doesn't match the SHA-12 is rejected."""
    with pytest.raises(ValidationError, match="finding_id"):
        AARFinding(
            engine="catalyst",
            finding_id="000000000000",  # wrong sha
            theme="exit_timing",
            pattern_observed="x",
            suggested_emission_axis="y",
            evidence_aar_count=3,
            evidence_window_sessions=10,
            confidence="low",
            observation_session=date(2026, 5, 22),
            persona_version="v1.0",
        )


def test_confidence_low_band_accepts_3_to_7() -> None:
    for n in (3, 5, 7):
        f = _make_finding(evidence_aar_count=n, confidence="low")
        assert f.evidence_aar_count == n


def test_confidence_low_band_rejects_above_7() -> None:
    with pytest.raises(ValidationError, match="confidence='low'"):
        _make_finding(evidence_aar_count=8, confidence="low")


def test_confidence_medium_band_accepts_8_to_20() -> None:
    for n in (8, 15, 20):
        f = _make_finding(evidence_aar_count=n, confidence="medium")
        assert f.evidence_aar_count == n


def test_confidence_medium_band_rejects_above_20() -> None:
    with pytest.raises(ValidationError, match="confidence='medium'"):
        _make_finding(evidence_aar_count=21, confidence="medium")


def test_confidence_medium_band_rejects_below_8() -> None:
    with pytest.raises(ValidationError, match="confidence='medium'"):
        _make_finding(evidence_aar_count=7, confidence="medium")


def test_confidence_high_band_requires_21_plus() -> None:
    f = _make_finding(evidence_aar_count=25, confidence="high")
    assert f.confidence == "high"
    with pytest.raises(ValidationError, match="confidence='high'"):
        _make_finding(evidence_aar_count=20, confidence="high")


def test_evidence_aar_count_below_3_rejected() -> None:
    """Fence #4: minimum 3 AARs."""
    with pytest.raises(ValidationError):
        _make_finding(evidence_aar_count=2, confidence="low")


def test_theme_closed_vocabulary() -> None:
    """Fence #11: LLM cannot invent new themes."""
    with pytest.raises(ValidationError):
        _make_finding(theme="my_new_theme_that_doesnt_exist")  # type: ignore[arg-type]


# ───────────────────────── EnginePerformanceWindow ─────────────────────


def test_engine_perf_window_frozen() -> None:
    w = EnginePerformanceWindow(
        engine="catalyst",
        as_of_session=date(2026, 5, 22),
        trade_count_total=24,
        trade_count_window=24,
        pnl_net_total_usd=Decimal("1500"),
        pnl_net_window_usd=Decimal("1500"),
        win_rate_window=0.5,
        win_rate_total=0.5,
        exit_reason_distribution={"time_stop": 9, "take_profit": 15},
        exit_reason_pnl_by_reason_usd={
            "time_stop": Decimal("-1840"),
            "take_profit": Decimal("3340"),
        },
        hold_duration_buckets={"0-1d": 5, "1-3d": 10, "3-7d": 9, "7-21d": 0, "21d+": 0},
        pnl_per_hold_bucket_usd={
            "0-1d": Decimal("400"),
            "1-3d": Decimal("800"),
            "3-7d": Decimal("300"),
            "7-21d": Decimal("0"),
            "21d+": Decimal("0"),
        },
        slippage_bps_p50=4.2,
        slippage_bps_p95=12.0,
        rule_compliance_rate=1.0,
        recent_aars=(),
    )
    assert w.engine == "catalyst"
    with pytest.raises(ValidationError):
        w.engine = "vector"  # type: ignore[misc]


def test_engine_perf_window_recent_aars_capped_at_20() -> None:
    """recent_aars over 20 → validation error per spec §2.2 cap."""
    too_many = tuple(
        AARRowSummary(
            ticker=f"T{i:02d}",
            entry_session=None,
            exit_session=date(2026, 5, 22),
            pnl_net_usd=Decimal("10"),
            exit_reason="take_profit",
            hold_sessions=1,
        )
        for i in range(21)
    )
    with pytest.raises(ValidationError, match="recent_aars"):
        EnginePerformanceWindow(
            engine="catalyst",
            as_of_session=date(2026, 5, 22),
            trade_count_total=21,
            trade_count_window=21,
            pnl_net_total_usd=Decimal("210"),
            pnl_net_window_usd=Decimal("210"),
            win_rate_window=1.0,
            win_rate_total=1.0,
            exit_reason_distribution={"take_profit": 21},
            exit_reason_pnl_by_reason_usd={"take_profit": Decimal("210")},
            hold_duration_buckets={"0-1d": 21, "1-3d": 0, "3-7d": 0, "7-21d": 0, "21d+": 0},
            pnl_per_hold_bucket_usd={
                "0-1d": Decimal("210"),
                "1-3d": Decimal("0"),
                "3-7d": Decimal("0"),
                "7-21d": Decimal("0"),
                "21d+": Decimal("0"),
            },
            slippage_bps_p50=None,
            slippage_bps_p95=None,
            rule_compliance_rate=1.0,
            recent_aars=too_many,
        )


def test_aar_row_summary_frozen() -> None:
    r = AARRowSummary(
        ticker="AAPL",
        entry_session=date(2026, 5, 1),
        exit_session=date(2026, 5, 3),
        pnl_net_usd=Decimal("42.50"),
        exit_reason="take_profit",
        hold_sessions=2,
    )
    with pytest.raises(ValidationError):
        r.ticker = "TSLA"  # type: ignore[misc]


# ───────────────────────── AARCriticRun ─────────────────────


def test_aar_critic_run_frozen() -> None:
    now = datetime.now(UTC)
    run = AARCriticRun(
        run_id=uuid4(),
        started_ts=now,
        completed_ts=now,
        trigger="nightly_cron",
        as_of_session=date(2026, 5, 22),
        engines_examined=("catalyst", "vector"),
        findings_emitted=("abc123def456",),
        persona_version="v1.0",
        rejection_reason=None,
    )
    assert run.trigger == "nightly_cron"
    with pytest.raises(ValidationError):
        run.trigger = "operator_command"  # type: ignore[misc]


def test_aar_critic_run_trigger_closed_vocab() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        AARCriticRun(
            run_id=uuid4(),
            started_ts=now,
            completed_ts=now,
            trigger="not_a_valid_trigger",  # type: ignore[arg-type]
            as_of_session=date(2026, 5, 22),
            engines_examined=(),
            findings_emitted=(),
            persona_version="v1.0",
        )
