"""LLM-AAR critic → memstore end-to-end integration test — spec §9.2.

Fake AsyncAnthropic + fake asyncpg pool + fake AARReader rows. Verifies:

- run_aar_critic reads fake AARs → invokes fake LLM → 2 findings emitted
- AAR memstore receives 2 findings at /findings/<engine>/<finding_id>.md
- Finder memstore receives 2 curated copies at /aar-findings/<engine>/<finding_id>.md
- application_log writes carry LAB_AAR_CRITIC_RUN + 2 LAB_AAR_CRITIC_FINDING rows
- Per-finding markdown is well-formed (rendered via render_finding_markdown)
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── Fake DB pool ─────────────────────────


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


# ───────────────────────── Fake Anthropic memstore ───────────────────


class _FakeMemoryReturn:
    def __init__(self, memory_id: str) -> None:
        self.id = memory_id


class _FakeMemories:
    """Captures memory_stores.memories.create calls per memstore_id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 0

    async def create(self, **kwargs: Any) -> _FakeMemoryReturn:
        self.calls.append(kwargs)
        self._next_id += 1
        return _FakeMemoryReturn(memory_id=f"mem_fake_{self._next_id:04d}")


class _FakeMemoryStores:
    def __init__(self) -> None:
        self.memories = _FakeMemories()


class _FakeBeta:
    def __init__(self) -> None:
        self.memory_stores = _FakeMemoryStores()


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.beta = _FakeBeta()


# ───────────────────────── Helpers ─────────────────────────


def _build_aar_rows() -> list[dict[str, Any]]:
    """Five catalyst AARs in window — enough for 'low' confidence."""
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for i in range(5):
        aar_data = {
            "engine": "catalyst",
            "trade_id": f"T{i:03d}",
            "ticker": f"AAPL{i}",
            "entry_ts": (now - timedelta(days=2)).isoformat(),
            "exit_ts": (now - timedelta(days=1)).isoformat(),
            "pnl_net": "10.0",
            "exit_reason": "take_profit",
            "rule_compliance": True,
            "slippage_bps": 3.0,
        }
        rows.append({
            "engine": "catalyst",
            "trade_id": f"T{i:03d}",
            "ticker": f"AAPL{i}",
            "aar_data": json.dumps(aar_data),
            "recorded_at": now,
        })
    return rows


async def _fake_llm_two_findings(_sys: str, _user: str, _transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthetic envelope with two findings — both 'low' confidence valid."""
    return {
        "kind": "AARCriticResponse",
        "findings": [
            {
                "engine": "catalyst",
                "theme": "exit_timing",
                "pattern_observed": "Synthetic pattern A.",
                "suggested_emission_axis": "Test variant A.",
                "evidence_aar_count": 5,
                "evidence_window_sessions": 90,
                "confidence": "low",
                "observation_session": "2026-05-22",
            },
            {
                "engine": "catalyst",
                "theme": "entry_quality",
                "pattern_observed": "Synthetic pattern B.",
                "suggested_emission_axis": "Test variant B.",
                "evidence_aar_count": 5,
                "evidence_window_sessions": 90,
                "confidence": "low",
                "observation_session": "2026-05-22",
            },
        ],
        "rationale": "Test envelope; two findings.",
    }


# ───────────────────────── E2E test ─────────────────────────


@pytest.mark.asyncio
async def test_run_aar_critic_to_memstore_e2e() -> None:
    """End-to-end: AAR reads → LLM call → findings → AAR memstore + finder memstore + application_log."""
    from ops.llm_aar_critic import run_aar_critic
    from tpcore.lab.llm_aar.memstore_writer import (
        archive_finding_to_aar_memstore,
        copy_finding_to_finder_memstore,
    )

    pool = _FakePool(_build_aar_rows())
    fake_client = _FakeAnthropicClient()
    aar_memstore_id = "memstore_FAKE_AAR_001"
    finder_memstore_id = "memstore_FAKE_FINDER_002"

    # Run the critic
    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=_fake_llm_two_findings,
    )

    # 2 findings emitted
    assert len(run.findings_emitted) == 2

    # application_log carries 1 RUN + 2 FINDING rows (already verified in
    # test_llm_aar_critic.py but we re-check here for the E2E story).
    sqls = [c[0] for c in pool.captured]
    assert sum("LAB_AAR_CRITIC_RUN" in s for s in sqls) == 1
    assert sum("LAB_AAR_CRITIC_FINDING" in s for s in sqls) == 2

    # Now mimic what the daemon does: write each finding to AAR memstore +
    # curated-copy to finder memstore.
    # (The full run_aar_critic does this in Phase D when anthropic_client +
    # memstore_ids are passed; here we exercise the writers directly so the
    # E2E assertion is independent of that wiring.)
    from tpcore.lab.llm_aar.models import (
        AARFinding,
        compute_finding_id,
    )

    # Construct the same two findings explicitly for memstore-write assertions
    # (matches the IDs the LLM-side envelope would have produced).
    findings = [
        AARFinding(
            engine="catalyst",
            finding_id=compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22)),
            theme="exit_timing",
            pattern_observed="A",
            suggested_emission_axis="X",
            evidence_aar_count=5,
            evidence_window_sessions=90,
            confidence="low",
            observation_session=date(2026, 5, 22),
            persona_version="v1.0",
        ),
        AARFinding(
            engine="catalyst",
            finding_id=compute_finding_id("catalyst", "entry_quality", date(2026, 5, 22)),
            theme="entry_quality",
            pattern_observed="B",
            suggested_emission_axis="Y",
            evidence_aar_count=5,
            evidence_window_sessions=90,
            confidence="low",
            observation_session=date(2026, 5, 22),
            persona_version="v1.0",
        ),
    ]

    for f in findings:
        await archive_finding_to_aar_memstore(
            f, memstore_id=aar_memstore_id, client=fake_client,  # type: ignore[arg-type]
        )
        await copy_finding_to_finder_memstore(
            f, finder_memstore_id=finder_memstore_id, client=fake_client,  # type: ignore[arg-type]
        )

    # 4 memory.create calls total (2 findings * 2 memstores).
    calls = fake_client.beta.memory_stores.memories.calls
    assert len(calls) == 4
    # First call: AAR memstore at /findings/catalyst/<id>.md
    aar_calls = [c for c in calls if c["memory_store_id"] == aar_memstore_id]
    finder_calls = [c for c in calls if c["memory_store_id"] == finder_memstore_id]
    assert len(aar_calls) == 2
    assert len(finder_calls) == 2
    for c in aar_calls:
        assert c["path"].startswith("/findings/catalyst/")
        assert c["path"].endswith(".md")
        assert "Engine:" in c["content"]
        assert "catalyst" in c["content"]
    for c in finder_calls:
        assert c["path"].startswith("/aar-findings/catalyst/")
        assert c["path"].endswith(".md")


@pytest.mark.asyncio
async def test_run_aar_critic_archives_findings_to_both_memstores() -> None:
    """run_aar_critic with anthropic_client + both memstore IDs writes to both."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool(_build_aar_rows())
    fake_client = _FakeAnthropicClient()
    aar_id = "memstore_FAKE_AAR_INLINE"
    finder_id = "memstore_FAKE_FINDER_INLINE"

    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=_fake_llm_two_findings,
        anthropic_client=fake_client,  # type: ignore[arg-type]
        aar_memstore_id=aar_id,
        finder_memstore_id=finder_id,
    )
    assert len(run.findings_emitted) == 2

    calls = fake_client.beta.memory_stores.memories.calls
    # 2 findings * 2 memstores = 4 memstore.create calls
    assert len(calls) == 4
    aar_paths = sorted(c["path"] for c in calls if c["memory_store_id"] == aar_id)
    finder_paths = sorted(c["path"] for c in calls if c["memory_store_id"] == finder_id)
    assert all(p.startswith("/findings/catalyst/") for p in aar_paths)
    assert all(p.startswith("/aar-findings/catalyst/") for p in finder_paths)


@pytest.mark.asyncio
async def test_run_aar_critic_skips_memstore_when_no_client() -> None:
    """Without an anthropic_client, run_aar_critic must NOT raise + must NOT attempt memstore writes."""
    from ops.llm_aar_critic import run_aar_critic

    pool = _FakePool(_build_aar_rows())
    run = await run_aar_critic(
        pool,  # type: ignore[arg-type]
        trigger="operator_command",
        as_of_session=date(2026, 5, 22),
        llm_callable=_fake_llm_two_findings,
        anthropic_client=None,
        aar_memstore_id="memstore_IGNORED",
        finder_memstore_id="memstore_IGNORED_2",
    )
    # Findings still emitted to application_log
    assert len(run.findings_emitted) == 2


@pytest.mark.asyncio
async def test_render_finding_markdown_well_formed() -> None:
    """Markdown rendering captures all key finding fields."""
    from tpcore.lab.llm_aar.memstore_writer import render_finding_markdown
    from tpcore.lab.llm_aar.models import AARFinding, compute_finding_id

    fid = compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22))
    f = AARFinding(
        engine="catalyst",
        finding_id=fid,
        theme="exit_timing",
        pattern_observed="Pattern PHRASE_X.",
        suggested_emission_axis="Axis PHRASE_Y.",
        evidence_aar_count=12,
        evidence_window_sessions=90,
        confidence="medium",
        observation_session=date(2026, 5, 22),
        persona_version="v1.0",
    )
    md = render_finding_markdown(f)
    assert f"# {fid}" in md
    assert "**Engine:** catalyst" in md
    assert "**Theme:** exit_timing" in md
    assert "**Confidence:** medium" in md
    assert "12 AARs" in md
    assert "Pattern PHRASE_X" in md
    assert "Axis PHRASE_Y" in md


@pytest.mark.asyncio
async def test_memstore_write_failure_swallowed_not_raised() -> None:
    """Memstore writer must not raise on API failure — application_log is the binding record."""
    from tpcore.lab.llm_aar.memstore_writer import archive_finding_to_aar_memstore
    from tpcore.lab.llm_aar.models import AARFinding, compute_finding_id

    class _FailingMemories:
        async def create(self, **_kwargs: Any) -> Any:
            raise RuntimeError("synthetic API failure")

    class _FailingMemoryStores:
        memories = _FailingMemories()

    class _FailingBeta:
        memory_stores = _FailingMemoryStores()

    class _FailingClient:
        beta = _FailingBeta()

    fid = compute_finding_id("catalyst", "exit_timing", date(2026, 5, 22))
    f = AARFinding(
        engine="catalyst", finding_id=fid, theme="exit_timing",
        pattern_observed="x", suggested_emission_axis="y",
        evidence_aar_count=5, evidence_window_sessions=90,
        confidence="low", observation_session=date(2026, 5, 22),
        persona_version="v1.0",
    )
    # Returns None on failure, does NOT raise.
    result = await archive_finding_to_aar_memstore(
        f, memstore_id="fake", client=_FailingClient(),  # type: ignore[arg-type]
    )
    assert result is None
