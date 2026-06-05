"""Unit tests for the 2026-05-13 ops.py additions.

Covers the helpers the expert flagged as untested:
  * ``_market_open_block_reason`` — NYSE-session guard
  * ``_self_heal_failed_stages`` — retry classifier
  * ``_RETRYABLE_FAILURE_REASONS`` — the actual matching set

Plus the idempotency contract in `_stage_daily_bars` (skip when bars
already present for the target session).

We don't exercise the real Postgres / Alpaca paths — those are
integration-tested by actually running the CLI. These are the pure-
function gates around the orchestration.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


@pytest.fixture
def ops_module():
    """Hermetic re-import of ``scripts/ops.py`` as the top-level ``ops``.

    Operator memory ``feedback_ops_package_shadow_full_suite_gate`` and
    ``.claude/rules/tests-and-ci.md`` forbid a collection-time
    ``sys.modules`` purge. This fixture moves the purge IN-BODY (test
    execution, not module collection) so each test gets a fresh,
    file-shadow ``ops`` even when an earlier test cached the ``ops/``
    package.

    Also forces ``scripts/`` ahead of the repo root on ``sys.path``
    because ``scripts/search_parameters.py`` deliberately reorders
    sys.path to put the repo root FIRST so its ``from ops.lab.run
    import …`` resolves the ``ops/`` package — a no-op ``insert(0, …)``
    check is not enough.
    """
    scripts_str = str(SCRIPTS_DIR)
    saved_path = list(sys.path)
    saved_ops = sys.modules.pop("ops", None)
    sys.path[:] = [p for p in sys.path if p != scripts_str]
    sys.path.insert(0, scripts_str)
    try:
        yield importlib.import_module("ops")
    finally:
        sys.modules.pop("ops", None)
        if saved_ops is not None:
            sys.modules["ops"] = saved_ops
        sys.path[:] = saved_path


# ────────────────────────────────────────────────────────────────────────────
# _market_open_block_reason — refuses to run during NYSE regular session
# ────────────────────────────────────────────────────────────────────────────


def test_market_blocked_during_regular_session(ops_module):
    ops = ops_module
    # 2024-07-15 18:00 UTC = 14:00 ET — mid-session on a Monday.
    now = datetime(2024, 7, 15, 18, 0, tzinfo=UTC)
    reason = ops._market_open_block_reason(now)  # noqa: SLF001
    assert reason is not None
    assert "NYSE" in reason
    assert "open" in reason.lower()


def test_market_open_after_close(ops_module):
    ops = ops_module
    # 2024-07-15 21:00 UTC = 17:00 ET — 1h after close.
    now = datetime(2024, 7, 15, 21, 0, tzinfo=UTC)
    assert ops._market_open_block_reason(now) is None  # noqa: SLF001


def test_market_open_pre_market(ops_module):
    ops = ops_module
    # 2024-07-15 12:00 UTC = 08:00 ET — before 09:30 open.
    now = datetime(2024, 7, 15, 12, 0, tzinfo=UTC)
    assert ops._market_open_block_reason(now) is None  # noqa: SLF001


def test_market_open_weekend(ops_module):
    ops = ops_module
    # 2024-07-13 is a Saturday — no session at all.
    now = datetime(2024, 7, 13, 18, 0, tzinfo=UTC)
    assert ops._market_open_block_reason(now) is None  # noqa: SLF001


def test_market_open_holiday_thanksgiving(ops_module):
    ops = ops_module
    # 2024-11-28 was Thanksgiving — NYSE closed.
    now = datetime(2024, 11, 28, 18, 0, tzinfo=UTC)
    assert ops._market_open_block_reason(now) is None  # noqa: SLF001


# ────────────────────────────────────────────────────────────────────────────
# _RETRYABLE_FAILURE_REASONS — the classifier set
# ────────────────────────────────────────────────────────────────────────────


def test_retryable_set_covers_observed_failures(ops_module):
    """Grounded in the 14-day survey: transient failures we saw must be retryable."""
    ops = ops_module
    # Every one of these substrings appeared in real INGESTION_FAILED messages.
    for token in ("timeout", "ReadError", "429", "ConnectError"):
        assert token in ops._RETRYABLE_FAILURE_REASONS, f"{token!r} missing from retryable set"  # noqa: SLF001


def test_retryable_set_excludes_logical_failures(ops_module):
    """Logical errors (real data state, not transient) MUST NOT auto-retry."""
    ops = ops_module
    # These are real failure shapes we don't want retried automatically.
    for token in ("no_data", "validation_failed", "RuntimeError"):
        assert token not in ops._RETRYABLE_FAILURE_REASONS  # noqa: SLF001


# ────────────────────────────────────────────────────────────────────────────
# _self_heal_failed_stages — orchestrator behaviour
# ────────────────────────────────────────────────────────────────────────────


def _summary_with_stages(ops, stages: list):
    s = ops.UpdateSummary(
        run_id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    s.stages = stages
    return s


def _async_noop_log():
    """Minimal db_log stand-in: any await call resolves to None."""
    m = MagicMock()
    m.log = AsyncMock(return_value=None)
    m._run_id = uuid.uuid4()  # noqa: SLF001
    return m


async def test_self_heal_retries_transient_timeout(ops_module):
    """A FAILED stage with a 'timeout' error should be retried once."""
    ops = ops_module
    pool_stub = MagicMock()  # _run_stage doesn't acquire here — uses factory
    log = MagicMock()
    log.info = MagicMock()
    log.error = MagicMock()
    log.bind = MagicMock(return_value=log)
    db_log = _async_noop_log()

    failed = ops.StageResult(name="fundamentals_refresh", status="TIMEOUT",
                             duration_ms=3_600_000, error="timed out after 3600s")
    summary = _summary_with_stages(ops, [failed])

    # Replace _run_stage with an async stub that records the retry call.
    retry_calls = []

    async def fake_run_stage(name, factory, *, log, db_log, timeout, dry_run):  # noqa: ARG001
        retry_calls.append((name, timeout, dry_run))
        return ops.StageResult(name=name, status="OK", duration_ms=42, detail={"rows_upserted": 7})

    orig = ops._run_stage  # noqa: SLF001
    ops._run_stage = fake_run_stage  # noqa: SLF001
    try:
        await ops._self_heal_failed_stages(summary, pool_stub, {}, log=log, db_log=db_log)  # noqa: SLF001
    finally:
        ops._run_stage = orig  # noqa: SLF001

    # Retried exactly once for the failed stage.
    assert len(retry_calls) == 1
    assert retry_calls[0][0] == "fundamentals_refresh"
    # The summary's stage entry was replaced with the retry result.
    assert summary.stages[0].status == "OK"
    assert summary.stages[0].detail.get("retried") is True


async def test_self_heal_skips_non_retryable(ops_module):
    """A FAILED stage with a logical-failure error should NOT be retried."""
    ops = ops_module
    failed = ops.StageResult(
        name="data_validation",
        status="FAILED",
        duration_ms=12_000,
        error="validation suite failed: ['row_integrity']",
    )
    summary = _summary_with_stages(ops, [failed])
    pool_stub = MagicMock()
    log = MagicMock()
    log.info = MagicMock()
    log.error = MagicMock()
    log.bind = MagicMock(return_value=log)
    db_log = _async_noop_log()

    retry_calls = []

    async def fake_run_stage(name, factory, *, log, db_log, timeout, dry_run):  # noqa: ARG001
        retry_calls.append(name)
        return ops.StageResult(name=name, status="OK", duration_ms=1)

    orig = ops._run_stage  # noqa: SLF001
    ops._run_stage = fake_run_stage  # noqa: SLF001
    try:
        await ops._self_heal_failed_stages(summary, pool_stub, {}, log=log, db_log=db_log)  # noqa: SLF001
    finally:
        ops._run_stage = orig  # noqa: SLF001

    assert retry_calls == [], "validation suite failure must NOT auto-retry"
    # The original failed result is preserved (not silently green-flipped).
    assert summary.stages[0].status == "FAILED"


async def test_self_heal_only_processes_failed_or_timeout(ops_module):
    """OK / DRY_RUN / SKIPPED stages should not be retried."""
    ops = ops_module
    stages = [
        ops.StageResult(name="daily_bars", status="OK", duration_ms=1),
        ops.StageResult(name="x", status="DRY_RUN", duration_ms=0),
    ]
    summary = _summary_with_stages(ops, stages)
    pool_stub = MagicMock()
    log = MagicMock()
    log.info = MagicMock()
    log.bind = MagicMock(return_value=log)
    db_log = _async_noop_log()

    calls = []

    async def fake_run_stage(*args, **kwargs):  # noqa: ARG001
        calls.append(args)
        return ops.StageResult(name="x", status="OK", duration_ms=1)

    orig = ops._run_stage  # noqa: SLF001
    ops._run_stage = fake_run_stage  # noqa: SLF001
    try:
        await ops._self_heal_failed_stages(summary, pool_stub, {}, log=log, db_log=db_log)  # noqa: SLF001
    finally:
        ops._run_stage = orig  # noqa: SLF001

    assert calls == []


# ────────────────────────────────────────────────────────────────────────────
# _stage_daily_bars idempotency threshold — skip when bars already ingested
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, count: int) -> None:
        self._count = count
        self.fetchval_calls = []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        # Only one fetchval site in _stage_daily_bars: the bar-count check.
        return self._count

    async def fetch(self, sql, *args):
        # Producer self-validation's post-ingest coverage query. Simulate
        # a HEALTHY post-ingest DB: the real target_session (the same
        # previous_close ops.py computes) plus 20 trailing sessions, all
        # at full coverage — so the collapse guard passes and this test
        # exercises the under-threshold handler path, not the guard.
        from datetime import UTC, datetime, timedelta

        from tpcore.calendar import previous_close

        tgt = previous_close(datetime.now(UTC)).date()
        return [
            {"date": tgt - timedelta(days=i), "n": 7_000}
            for i in range(21)
        ]


class _FakeCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return None


class _FakePool:
    def __init__(self, count: int) -> None:
        self.conn = _FakeConn(count)

    def acquire(self):
        return _FakeCM(self.conn)


async def test_daily_bars_skips_when_already_ingested(ops_module, monkeypatch):
    """When ≥6,500 tickers already have a bar for the target session,
    the stage skips the handler entirely."""
    ops = ops_module
    pool = _FakePool(count=7_300)

    # Patch handle_daily_bars to a sentinel so we can assert it WASN'T called.
    handler_calls = []

    async def fake_handler(*args, **kwargs):
        handler_calls.append((args, kwargs))
        return 99_999  # would be a huge ingest if we ran

    import tpcore.ingestion.handlers as h
    monkeypatch.setattr(h, "handle_daily_bars", fake_handler)

    result = await ops._stage_daily_bars(pool, {"universe": "active"})  # noqa: SLF001
    assert result["rows_upserted"] == 0
    assert result.get("skipped") == "already_ingested"
    assert handler_calls == [], "handler must NOT be called when idempotency check passes"


async def test_daily_bars_runs_when_under_threshold(ops_module, monkeypatch):
    """When count < 6,500 the handler IS called."""
    ops = ops_module
    pool = _FakePool(count=100)

    handler_calls = []

    async def fake_handler(pool_arg, config):
        handler_calls.append(config)
        return 4_242

    import tpcore.ingestion.handlers as h
    monkeypatch.setattr(h, "handle_daily_bars", fake_handler)

    result = await ops._stage_daily_bars(pool, {"universe": "active"})  # noqa: SLF001
    assert result["rows_upserted"] == 4_242
    assert "skipped" not in result
    assert len(handler_calls) == 1


# ────────────────────────────────────────────────────────────────────────
# cmd_audit — cross-table integrity. Verifies the SQL list is canonical
# and that the passed flag is the conjunction of every check.
# ────────────────────────────────────────────────────────────────────────


def test_audit_checks_cover_every_dependent_table(ops_module):
    """Each table that joins prices_daily must have a ticker_not_in_prices check.
    Catches the regression where someone adds a new table and forgets to
    extend _AUDIT_CHECKS."""
    ops = ops_module
    expected_tables = {
        "earnings_events", "corporate_actions", "fundamentals_quarterly",
        "liquidity_tiers", "universe_candidates",
    }  # tradier_options_chains dropped (Tradier closed, Plan 2 migration 20260604_0300)
    audited = {
        table for table, check, _ in ops._AUDIT_CHECKS  # noqa: SLF001
        if check == "ticker_not_in_prices"
    }
    missing = expected_tables - audited
    assert not missing, f"missing ticker_not_in_prices for: {missing}"


def test_audit_includes_freshness_checks(ops_module):
    ops = ops_module
    by_kind = {(t, c) for t, c, _ in ops._AUDIT_CHECKS}  # noqa: SLF001
    # The historical pain point — keep wired. (The tradier_options_chains
    # 'expired' check was removed with the table — Plan 2, Tradier closed.)
    assert ("liquidity_tiers", "stale_30d") in by_kind


# ────────────────────────────────────────────────────────────────────────
# Parser — every consolidated mode is wired
# ────────────────────────────────────────────────────────────────────────


def test_cli_has_audit_reconcile_allocate_status_modes(ops_module):
    """Regression guard: each consolidated command must remain
    parseable so the dashboard's daemon-status fetchers don't break."""
    ops = ops_module
    parser = ops._build_parser()  # noqa: SLF001
    for mode in ("--audit", "--reconcile", "--allocate", "--status"):
        args = parser.parse_args([mode])
        # exactly one boolean mode flag should be True
        active = [a for a in ("audit", "reconcile", "allocate", "status",
                              "update", "check", "full")
                  if getattr(args, a, False)]
        assert active == [mode.lstrip("-")], f"{mode} mis-parsed to {active}"


def test_cli_enforce_freeze_flag_paired_with_allocate(ops_module):
    """--enforce-freeze is a modifier only useful with --allocate.
    Parser accepts it standalone (no validation there), but the spec
    requires this combination is the live-mode path."""
    ops = ops_module
    parser = ops._build_parser()  # noqa: SLF001
    args = parser.parse_args(["--allocate", "--enforce-freeze"])
    assert args.allocate is True
    assert args.enforce_freeze is True
