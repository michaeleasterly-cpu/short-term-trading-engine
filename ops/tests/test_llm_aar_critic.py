"""LLM-AAR critic main-loop unit tests — spec §9.1.

Covers run_aar_critic + helpers in ops/llm_aar_critic.py.

- run_aar_critic in smoke mode (no llm_callable) → empty findings + provenance.
- run_aar_critic with a fake LLMCallable returning a valid envelope → findings recorded.
- run_aar_critic tolerates malformed LLM envelopes (kind-mismatch, non-list findings).
- _normalize_finding stamps persona_version + computes deterministic finding_id.
- _compose_user_prompt + _render_engine_section produce non-empty prose for the LLM.
- Per-engine emission cap is enforced.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── Fake pool / conn ─────────────────────────


class _FakeConn:
    def __init__(self, fetch_rows: list[dict[str, Any]], capture: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._fetch_rows = fetch_rows
        self._capture = capture

    async def fetch(self, _sql: str, *_args: Any) -> list[dict[str, Any]]:
        return self._fetch_rows

    async def execute(self, sql: str, *args: Any) -> None:
        self._capture.append((sql, args))

    async def fetchval(self, _sql: str, *_args: Any) -> int:
        return 0


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, fetch_rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_rows = fetch_rows or []
        self.captured: list[tuple[str, tuple[Any, ...]]] = []
        self._conn = _FakeConn(self._fetch_rows, self.captured)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


# ───────────────────────── Smoke mode (no LLM) ──────────────────────


@pytest.mark.asyncio
async def test_run_aar_critic_smoke_mode_no_findings() -> None:
    """No llm_callable + no AARs → empty payload, smoke rejection_reason."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool([])
    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=None,
    )
    assert run.trigger == "operator_command"
    assert run.findings_emitted == ()
    assert run.engines_examined == ()
    assert run.rejection_reason == "smoke_mode_no_llm"
    # Exactly one provenance row written.
    assert len(pool.captured) == 1
    sql, _args = pool.captured[0]
    assert "LAB_AAR_CRITIC_RUN" in sql


# ───────────────────────── LLM envelope → findings flow ─────────────


def _build_catalyst_rows() -> list[dict[str, Any]]:
    """Five catalyst AARs in the recent window — enough for 'low' confidence."""
    import json

    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for i in range(5):
        aar_data = {
            "engine": "catalyst",
            "trade_id": f"T{i:03d}",
            "ticker": f"TKR{i:02d}",
            "entry_ts": (now.replace(microsecond=0)).isoformat(),
            "exit_ts": (now.replace(microsecond=0)).isoformat(),
            "pnl_net": "10.0",
            "exit_reason": "take_profit",
            "rule_compliance": True,
            "slippage_bps": 3.5,
        }
        rows.append({
            "engine": "catalyst",
            "trade_id": f"T{i:03d}",
            "ticker": f"TKR{i:02d}",
            "aar_data": json.dumps(aar_data),
            "recorded_at": now,
        })
    return rows


@pytest.mark.asyncio
async def test_run_aar_critic_emits_findings_from_envelope() -> None:
    """A valid LLM envelope with 1 finding lands as 1 AARFinding row."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool(_build_catalyst_rows())

    async def fake_llm(_sys: str, _user: str, _transcript: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "kind": "AARCriticResponse",
            "findings": [
                {
                    "engine": "catalyst",
                    "theme": "exit_timing",
                    "pattern_observed": "Synthetic test finding.",
                    "suggested_emission_axis": "Test a stricter time_stop band.",
                    "evidence_aar_count": 5,
                    "evidence_window_sessions": 90,
                    "confidence": "low",
                    "observation_session": "2026-05-22",
                }
            ],
            "rationale": "Test envelope.",
        }

    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=fake_llm,
    )
    assert len(run.findings_emitted) == 1
    # 1 AARCriticRun row + 1 AARFinding row.
    assert len(pool.captured) == 2
    sqls = [c[0] for c in pool.captured]
    assert any("LAB_AAR_CRITIC_RUN" in s for s in sqls)
    assert any("LAB_AAR_CRITIC_FINDING" in s for s in sqls)


@pytest.mark.asyncio
async def test_run_aar_critic_tolerates_kind_mismatch() -> None:
    """LLM envelope with wrong 'kind' → no findings; run still completes."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool(_build_catalyst_rows())

    async def fake_llm(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"kind": "WrongKind", "findings": []}

    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=fake_llm,
    )
    assert run.findings_emitted == ()
    # Provenance still recorded.
    assert any("LAB_AAR_CRITIC_RUN" in c[0] for c in pool.captured)


@pytest.mark.asyncio
async def test_run_aar_critic_drops_findings_with_invalid_band() -> None:
    """LLM emits a 'high' confidence finding with 3 AARs → rejected at validation."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool(_build_catalyst_rows())

    async def fake_llm(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "kind": "AARCriticResponse",
            "findings": [
                {
                    "engine": "catalyst",
                    "theme": "exit_timing",
                    "pattern_observed": "Over-claimed.",
                    "suggested_emission_axis": "Whatever.",
                    "evidence_aar_count": 3,  # band low
                    "evidence_window_sessions": 90,
                    "confidence": "high",  # band high → mismatch
                    "observation_session": "2026-05-22",
                }
            ],
            "rationale": "Will be rejected.",
        }

    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=fake_llm,
    )
    # Finding rejected at validation; none emitted.
    assert run.findings_emitted == ()


@pytest.mark.asyncio
async def test_run_aar_critic_enforces_per_engine_cap() -> None:
    """LLM emits 10 findings for catalyst → only first 5 land."""
    from ops.llm_aar_critic import run_aar_critic
    from tpcore.lab.llm_aar import MAX_FINDINGS_PER_ENGINE_PER_RUN

    pool = _FakePool(_build_catalyst_rows())

    themes = [
        "exit_timing", "entry_quality", "sizing_drift",
        "regime_conditional_perf", "exit_reason_skew",
        "rule_compliance_drift", "hold_duration_skew",
    ]

    async def fake_llm(*_a: Any, **_k: Any) -> dict[str, Any]:
        findings = []
        for theme in themes:
            findings.append({
                "engine": "catalyst",
                "theme": theme,
                "pattern_observed": f"Test {theme}.",
                "suggested_emission_axis": f"Test axis for {theme}.",
                "evidence_aar_count": 5,
                "evidence_window_sessions": 90,
                "confidence": "low",
                "observation_session": "2026-05-22",
            })
        return {"kind": "AARCriticResponse", "findings": findings, "rationale": "many"}

    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=fake_llm,
    )
    assert len(run.findings_emitted) == MAX_FINDINGS_PER_ENGINE_PER_RUN


# ───────────────────────── _normalize_finding ─────────────────────


def test_normalize_finding_stamps_persona_and_computes_id() -> None:
    """_normalize_finding fills persona_version + finding_id deterministically."""
    from ops.llm_aar_critic import _normalize_finding
    from tpcore.lab.llm_aar import PERSONA_VERSION
    from tpcore.lab.llm_aar.models import compute_finding_id

    raw = {
        "engine": "catalyst",
        "theme": "exit_timing",
        "pattern_observed": "x",
        "suggested_emission_axis": "y",
        "evidence_aar_count": 5,
        "evidence_window_sessions": 90,
        "confidence": "low",
        "observation_session": "2026-05-22",
    }
    norm = _normalize_finding(raw, as_of_session=date(2026, 5, 22))
    assert norm["persona_version"] == PERSONA_VERSION
    expected_id = compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22))
    assert norm["finding_id"] == expected_id


def test_normalize_finding_defaults_bad_session_to_as_of() -> None:
    """observation_session bad string → falls back to as_of_session."""
    from ops.llm_aar_critic import _normalize_finding

    raw = {
        "engine": "catalyst",
        "theme": "exit_timing",
        "observation_session": "not-a-date",
    }
    norm = _normalize_finding(raw, as_of_session=date(2026, 5, 22))
    assert norm["observation_session"] == date(2026, 5, 22)


# ───────────────────────── prompt rendering ─────────────────────


def test_compose_user_prompt_empty_payload() -> None:
    """Empty payload prompt mentions no engines + asks for empty findings."""
    from ops.llm_aar_critic import _compose_user_prompt

    text = _compose_user_prompt((), date(2026, 5, 22))
    assert "No engines" in text
    assert "findings" in text


def test_compose_user_prompt_renders_engine_section() -> None:
    """Per-engine payload section has all key fields."""
    from ops.llm_aar_critic import _compose_user_prompt
    from tpcore.lab.llm_aar.models import EnginePerformanceWindow

    w = EnginePerformanceWindow(
        engine="catalyst",
        as_of_session=date(2026, 5, 22),
        trade_count_total=10,
        trade_count_window=10,
        pnl_net_total_usd=Decimal("100"),
        pnl_net_window_usd=Decimal("100"),
        win_rate_window=0.6,
        win_rate_total=0.6,
        exit_reason_distribution={"take_profit": 6, "stop_loss": 4},
        exit_reason_pnl_by_reason_usd={"take_profit": Decimal("160"), "stop_loss": Decimal("-60")},
        hold_duration_buckets={"0-1d": 5, "1-3d": 3, "3-7d": 2, "7-21d": 0, "21d+": 0},
        pnl_per_hold_bucket_usd={"0-1d": Decimal("50"), "1-3d": Decimal("30"), "3-7d": Decimal("20"), "7-21d": Decimal("0"), "21d+": Decimal("0")},
        slippage_bps_p50=2.5,
        slippage_bps_p95=10.0,
        rule_compliance_rate=1.0,
        recent_aars=(),
    )
    text = _compose_user_prompt((w,), date(2026, 5, 22))
    assert "catalyst" in text
    assert "trade_count_total" in text
    assert "exit_reason_distribution" in text
    assert "hold_duration_buckets" in text
    assert "win_rate_window: 0.600" in text
    # Closed theme vocabulary listed
    assert "exit_timing" in text
    assert "rule_compliance_drift" in text
