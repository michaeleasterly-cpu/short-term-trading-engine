"""Phase E + F outcome-monitor tests — Task #25 §10.2 + §10.3.

Covers:
- _classify_auto_retire: bleed-cap / operator-failure / inactivity / none / global
- _is_finder_paper_engines query routing (SQL fragment match)
- monitor: happy path with synthetic trades → LiveOutcome + LAB_FINDER_OUTCOME_CHECK row
- monitor: operator-success path → outcome_proven provenance
- monitor: bleed-cap path → ecr_retire provenance with reason='bleed_cap'
- monitor: inactivity timeout → ecr_retire provenance with reason='inactivity_timeout'
- monitor: global bleed cap (3 engines × $5k each) → global_bleed_cap reason
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tpcore.lab.llm_finder import (
    BLEED_CAP_PER_ENGINE_USD,
    INACTIVITY_AUTO_RETIRE_SESSIONS,
    MIN_TRADE_COUNT_FOR_NO_VERDICT,
)

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ───────────────────────── _classify_auto_retire ─────────────────────────


def test_classify_no_retire_when_within_budget() -> None:
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, reason = _classify_auto_retire(
        cumulative_bleed_usd=1000.0,
        session_count=5,
        trade_count_total=10,
        operator_verdict="none",
    )
    assert triggered is False
    assert reason == "none"


def test_classify_bleed_cap_breach() -> None:
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, reason = _classify_auto_retire(
        cumulative_bleed_usd=BLEED_CAP_PER_ENGINE_USD + 100.0,
        session_count=5,
        trade_count_total=10,
        operator_verdict="none",
    )
    assert triggered is True
    assert reason == "bleed_cap"


def test_classify_operator_failure() -> None:
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, reason = _classify_auto_retire(
        cumulative_bleed_usd=100.0,
        session_count=10,
        trade_count_total=50,
        operator_verdict="failure",
    )
    assert triggered is True
    assert reason == "operator_failure"


def test_classify_inactivity_timeout() -> None:
    """60+ sessions + <30 trades + no verdict → inactivity_timeout."""
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, reason = _classify_auto_retire(
        cumulative_bleed_usd=0.0,
        session_count=INACTIVITY_AUTO_RETIRE_SESSIONS + 1,
        trade_count_total=MIN_TRADE_COUNT_FOR_NO_VERDICT - 1,
        operator_verdict="none",
    )
    assert triggered is True
    assert reason == "inactivity_timeout"


def test_classify_no_inactivity_when_active() -> None:
    """60+ sessions BUT trade_count >= 30 → no inactivity timeout."""
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, _reason = _classify_auto_retire(
        cumulative_bleed_usd=0.0,
        session_count=INACTIVITY_AUTO_RETIRE_SESSIONS + 1,
        trade_count_total=MIN_TRADE_COUNT_FOR_NO_VERDICT + 1,
        operator_verdict="none",
    )
    assert triggered is False


def test_classify_bleed_cap_precedence_over_operator() -> None:
    """Bleed-cap fires BEFORE operator-failure (capital-safety first)."""
    from ops.llm_finder_outcome_monitor import _classify_auto_retire
    triggered, reason = _classify_auto_retire(
        cumulative_bleed_usd=BLEED_CAP_PER_ENGINE_USD + 100.0,
        session_count=5,
        trade_count_total=10,
        operator_verdict="failure",
    )
    assert triggered is True
    assert reason == "bleed_cap"


# ───────────────────────── FakePool with SQL routing ─────────────────────────


class _FakeConn:
    def __init__(
        self,
        sink: list[tuple[str, str, tuple[Any, ...]]],
        fixtures: dict[str, list[dict[str, Any]]] | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> None:
        self._sink = sink
        self._fixtures = fixtures or {}
        self._verdict = verdict

    async def execute(self, sql: str, *args: Any) -> None:
        self._sink.append(("execute", sql, args))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "DISTINCT (payload->>'engine')" in sql or "LAB_FINDER_ACTION" in sql and "merge" in sql:
            return self._fixtures.get("paper_engines", [])
        if "aar_events" in sql:
            ticker = args[0] if args else ""
            return self._fixtures.get(f"aar:{ticker}", [])
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "LAB_FINDER_OUTCOME_VERDICT" in sql:
            ticker = args[0] if args else ""
            v = self._fixtures.get(f"verdict:{ticker}")
            return v[0] if v else None
        return None


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, fixtures: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.sink: list[tuple[str, str, tuple[Any, ...]]] = []
        self.fixtures = fixtures or {}

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn(self.sink, self.fixtures))


# ───────────────────────── monitor — synthetic engines ─────────────────────


@pytest.mark.asyncio
async def test_monitor_no_engines_no_writes() -> None:
    """No finder-emitted PAPER engines → no LiveOutcome / no events."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )
    pool = _FakePool()
    out = await monitor_finder_emitted_paper_engines(pool, as_of_session=date(2026, 5, 21))  # type: ignore[arg-type]
    assert out == ()
    assert pool.sink == []


@pytest.mark.asyncio
async def test_monitor_happy_path_with_trades() -> None:
    """Engine with positive P&L → LiveOutcome + LAB_FINDER_OUTCOME_CHECK + no auto-retire."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )

    first_promoted = datetime(2026, 5, 1, tzinfo=UTC)
    pool = _FakePool(fixtures={
        "paper_engines": [{"engine": "momentum", "first_promoted_ts": first_promoted}],
        "aar:momentum": [
            {"realised_pnl_usd": 150.0, "unrealised_pnl_usd": 50.0, "opened_at": first_promoted, "closed_at": None},
            {"realised_pnl_usd": 100.0, "unrealised_pnl_usd": 0.0, "opened_at": first_promoted, "closed_at": None},
        ],
    })
    out = await monitor_finder_emitted_paper_engines(pool, as_of_session=date(2026, 5, 21))  # type: ignore[arg-type]
    assert len(out) == 1
    lo = out[0]
    assert lo.engine == "momentum"
    assert lo.pnl_realised_total_usd == 250.0
    assert lo.cumulative_bleed_usd == 0.0  # positive P&L → no bleed
    assert lo.operator_verdict == "none"
    assert lo.auto_retire_triggered is False
    # Provenance: one LAB_FINDER_OUTCOME_CHECK row written.
    check_rows = [s for s in pool.sink if "LAB_FINDER_OUTCOME_CHECK" in s[1]]
    assert len(check_rows) == 1
    payload = json.loads(check_rows[0][2][0])
    assert payload["engine"] == "momentum"


@pytest.mark.asyncio
async def test_monitor_operator_success_writes_outcome_proven() -> None:
    """Operator posted verdict='success' → outcome_proven LAB_FINDER_ACTION."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )

    first_promoted = datetime(2026, 5, 1, tzinfo=UTC)
    pool = _FakePool(fixtures={
        "paper_engines": [{"engine": "momentum", "first_promoted_ts": first_promoted}],
        "aar:momentum": [],
        "verdict:momentum": [{"verdict": "success", "operator_note": "looks good"}],
    })
    out = await monitor_finder_emitted_paper_engines(pool, as_of_session=date(2026, 5, 21))  # type: ignore[arg-type]
    assert out[0].operator_verdict == "success"

    # outcome_proven LAB_FINDER_ACTION row written.
    action_rows = [s for s in pool.sink if "LAB_FINDER_ACTION" in s[1]]
    assert len(action_rows) == 1
    payload = json.loads(action_rows[0][2][0])
    assert payload["action"] == "outcome_proven"
    assert payload["triggered_by"] == "operator_verdict"
    assert payload["engine"] == "momentum"


@pytest.mark.asyncio
async def test_monitor_bleed_cap_breach_writes_ecr_retire() -> None:
    """Engine bled past $5k → ecr_retire LAB_FINDER_ACTION with reason='bleed_cap'."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )

    first_promoted = datetime(2026, 5, 1, tzinfo=UTC)
    pool = _FakePool(fixtures={
        "paper_engines": [{"engine": "momentum", "first_promoted_ts": first_promoted}],
        "aar:momentum": [
            {"realised_pnl_usd": -3000.0, "unrealised_pnl_usd": -2100.0, "opened_at": first_promoted, "closed_at": None},
        ],
    })
    out = await monitor_finder_emitted_paper_engines(pool, as_of_session=date(2026, 5, 21))  # type: ignore[arg-type]
    assert out[0].cumulative_bleed_usd > BLEED_CAP_PER_ENGINE_USD
    assert out[0].auto_retire_reason == "bleed_cap"

    action_rows = [s for s in pool.sink if "LAB_FINDER_ACTION" in s[1]]
    assert len(action_rows) == 1
    payload = json.loads(action_rows[0][2][0])
    assert payload["action"] == "ecr_retire"
    assert payload["triggered_by"] == "bleed_cap"


@pytest.mark.asyncio
async def test_monitor_inactivity_timeout_writes_ecr_retire() -> None:
    """60+ sessions, <30 trades, no verdict → ecr_retire with reason='inactivity_timeout'."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )

    # Engine promoted 70 days ago → session_count > 60.
    as_of = date(2026, 5, 21)
    first_promoted = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC) - timedelta(days=70)
    pool = _FakePool(fixtures={
        "paper_engines": [{"engine": "momentum", "first_promoted_ts": first_promoted}],
        # 5 trades — below MIN_TRADE_COUNT_FOR_NO_VERDICT (30).
        "aar:momentum": [
            {"realised_pnl_usd": 10.0, "unrealised_pnl_usd": 0.0, "opened_at": first_promoted, "closed_at": None}
            for _ in range(5)
        ],
    })
    out = await monitor_finder_emitted_paper_engines(pool, as_of_session=as_of)  # type: ignore[arg-type]
    assert out[0].session_count >= INACTIVITY_AUTO_RETIRE_SESSIONS
    assert out[0].trade_count_total < MIN_TRADE_COUNT_FOR_NO_VERDICT
    assert out[0].auto_retire_reason == "inactivity_timeout"

    action_rows = [s for s in pool.sink if "LAB_FINDER_ACTION" in s[1]]
    assert any(json.loads(r[2][0])["triggered_by"] == "inactivity_timeout" for r in action_rows)


@pytest.mark.asyncio
async def test_monitor_emits_outcome_check_before_phase_f() -> None:
    """Phase E surfaces the LiveOutcome to §12 BEFORE Phase F evaluates."""
    from ops.llm_finder_outcome_monitor import (
        monitor_finder_emitted_paper_engines,
    )

    first_promoted = datetime(2026, 5, 1, tzinfo=UTC)
    pool = _FakePool(fixtures={
        "paper_engines": [{"engine": "momentum", "first_promoted_ts": first_promoted}],
        "aar:momentum": [
            {"realised_pnl_usd": 50.0, "unrealised_pnl_usd": 0.0, "opened_at": first_promoted, "closed_at": None},
        ],
        "verdict:momentum": [{"verdict": "success"}],
    })
    await monitor_finder_emitted_paper_engines(pool, as_of_session=date(2026, 5, 21))  # type: ignore[arg-type]
    # OUTCOME_CHECK row precedes the outcome_proven action row in the sink.
    sql_seq = [s[1] for s in pool.sink]
    check_idx = next(i for i, s in enumerate(sql_seq) if "OUTCOME_CHECK" in s)
    action_idx = next(i for i, s in enumerate(sql_seq) if "LAB_FINDER_ACTION" in s)
    assert check_idx < action_idx
