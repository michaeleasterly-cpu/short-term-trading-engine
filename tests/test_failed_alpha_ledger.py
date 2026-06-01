"""F1 (2026-06-01) — failed-alpha ledger hermetic tests.

Pydantic validation + async record_failed_alpha mocking +
write_credibility_score_with_failed_alpha auto-emission +
backfill-script dry-run shape.

NO live DB; the mock pool stub records every execute/fetchrow call
so the tests can assert what was (or wasn't) persisted.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.forensics.alpha_ledger import (
    LEDGER_TABLE,
    BlockingConstraint,
    FailedAlphaRecord,
    FailedAlphaStatus,
    RecordResult,
    ledger_source,
    record_failed_alpha,
)


def _mock_pool_returning_id(record_id: int | None = 42) -> MagicMock:
    """asyncpg.Pool stub whose ``acquire().fetchrow(INSERT_SQL, ...)``
    returns ``{'id': record_id}`` on insert, or ``None`` to simulate
    the ON CONFLICT DO NOTHING path."""
    conn = MagicMock()
    if record_id is None:
        conn.fetchrow = AsyncMock(return_value=None)
    else:
        conn.fetchrow = AsyncMock(return_value={"id": record_id})
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.conn_for_assertions = conn
    return pool


def _good_record_kwargs() -> dict:
    """Minimal kwargs producing a valid FailedAlphaRecord."""
    return {
        "engine": "sigma",
        "sweep_id": "test-sweep-1",
        "data_window_start": date(2020, 1, 1),
        "data_window_end": date(2024, 1, 1),
        "universe": "T1+T2",
        "n_trials": 150,
        "blocking_constraint": BlockingConstraint.DSR_FAILURE,
        "failure_summary": "Sweep produced positive OOS edge but DSR < 0.95.",
    }


# ─── TEST-F1-01..05 — Pydantic invariants


def test_failed_alpha_record_requires_blocking_constraint() -> None:
    """A FailedAlphaRecord MUST have a blocking_constraint —
    operator hard rule: no graveyard of unclassified failures."""
    kwargs = _good_record_kwargs()
    del kwargs["blocking_constraint"]
    with pytest.raises(Exception, match="blocking_constraint"):
        FailedAlphaRecord(**kwargs)


def test_failed_alpha_record_requires_nonempty_failure_summary() -> None:
    """Empty / whitespace-only failure_summary rejected by the
    field validator."""
    kwargs = _good_record_kwargs()
    kwargs["failure_summary"] = "   "
    with pytest.raises(ValueError, match="explain"):
        FailedAlphaRecord(**kwargs)


def test_failed_alpha_record_validates_score_range() -> None:
    """credibility_score must be 0..100 inclusive when present."""
    kwargs = _good_record_kwargs()
    kwargs["credibility_score"] = 101
    with pytest.raises(Exception):  # noqa: B017
        FailedAlphaRecord(**kwargs)
    kwargs["credibility_score"] = -1
    with pytest.raises(Exception):  # noqa: B017
        FailedAlphaRecord(**kwargs)


def test_failed_alpha_record_validates_window_order() -> None:
    """data_window_end must be >= data_window_start."""
    kwargs = _good_record_kwargs()
    kwargs["data_window_end"] = date(2019, 1, 1)  # before start (2020-01-01)
    with pytest.raises(ValueError, match="data_window_end"):
        FailedAlphaRecord(**kwargs)


def test_failed_alpha_record_extra_forbid() -> None:
    """Pydantic model has extra='forbid' so misspelled fields fail
    loudly instead of silently dropping."""
    kwargs = _good_record_kwargs()
    kwargs["mispelled_field"] = "oops"
    with pytest.raises(Exception, match="mispelled_field|extra"):
        FailedAlphaRecord(**kwargs)


# ─── TEST-F1-06..07 — Enum pinning sentinels


def test_blocking_constraint_enum_pinned() -> None:
    """The blocking_constraint set is enforced both at the Pydantic
    layer AND at the SQL CHECK enum (migration 20260601_0100).
    Pinning the membership here forces a deliberate co-update."""
    expected = {
        "n_trades_low",
        "liquidity_failure",
        "dsr_failure",
        "psr_failure",
        "pbo_failure",
        "min_btl_failure",
        "signal_class_mismatch",
        "multi_gate_intersection",
        "regime_fragility",
        "cost_dominated",
        "data_quality_failure",
        "lookahead_or_bias_risk",
        "operator_rejected",
        "deprecated_signal_source",
    }
    actual = {v.value for v in BlockingConstraint}
    assert actual == expected, (
        f"BlockingConstraint enum drift detected.\n"
        f"  added (Pydantic-only, MISSING SQL CHECK): {actual - expected}\n"
        f"  removed (Pydantic-only, STALE SQL CHECK): {expected - actual}\n"
        "Migration 20260601_0100 must be co-updated."
    )


def test_status_enum_pinned() -> None:
    """FailedAlphaStatus enum pinned (SQL CHECK enum mirror)."""
    expected = {"FAILED", "SHELVED", "ARCHIVED", "REVISIT_QUEUED"}
    actual = {v.value for v in FailedAlphaStatus}
    assert actual == expected


# ─── TEST-F1-08..09 — record_failed_alpha behavior


@pytest.mark.asyncio
async def test_record_failed_alpha_inserts_typed_row() -> None:
    """Happy path: record_failed_alpha calls INSERT with the
    expected positional args."""
    record = FailedAlphaRecord(**_good_record_kwargs())
    pool = _mock_pool_returning_id(record_id=99)
    result = await record_failed_alpha(pool, record)

    assert isinstance(result, RecordResult)
    assert result.inserted is True
    assert result.engine == "sigma"
    assert result.sweep_id == "test-sweep-1"
    assert result.record_id == 99
    assert "dsr_failure" in result.reason

    # Inspect the SQL that ran.
    conn = pool.conn_for_assertions
    assert conn.fetchrow.await_count == 1
    sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO platform.failed_alpha_ledger" in sql
    assert "ON CONFLICT (engine, sweep_id) DO NOTHING" in sql

    # Engine / sweep_id / blocking_constraint values flow through.
    args = conn.fetchrow.await_args.args
    assert args[1] == "sigma"  # engine = $1
    assert args[3] == "test-sweep-1"  # sweep_id = $3
    # blocking_constraint is $23 (1-indexed in SQL); positional arg
    # index = 22 (after sql string).
    assert args[23] == "dsr_failure"


@pytest.mark.asyncio
async def test_record_failed_alpha_idempotent() -> None:
    """ON CONFLICT DO NOTHING — re-running with the same
    (engine, sweep_id) returns inserted=False, record_id=None."""
    record = FailedAlphaRecord(**_good_record_kwargs())
    pool = _mock_pool_returning_id(record_id=None)  # simulate conflict
    result = await record_failed_alpha(pool, record)

    assert result.inserted is False
    assert result.record_id is None
    assert "already" in result.reason.lower()


# ─── TEST-F1-10..12 — write_credibility_score_with_failed_alpha


@pytest.mark.asyncio
async def test_write_credibility_score_emits_failed_alpha_on_low_score() -> None:
    """Score < 60 + a complete FailedAlphaRecord → BOTH
    credibility row AND ledger row land."""
    from tpcore.backtest.credibility import CredibilityScore
    from tpcore.backtest.statistical_validation import (
        write_credibility_score_with_failed_alpha,
    )

    failing_score = CredibilityScore(
        score=45,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=False,
        monte_carlo_drawdown=False,
        notes=None,
    )
    record = FailedAlphaRecord(**_good_record_kwargs())

    # The mock pool services BOTH writes:
    # 1) write_credibility_score → DataQualityWriter.write() →
    #    conn.execute INSERT INTO data_quality_log
    # 2) record_failed_alpha → conn.fetchrow INSERT INTO ledger
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value={"id": 7})
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    cred_inserted, rec_result = (
        await write_credibility_score_with_failed_alpha(
            pool,
            engine_name="sigma",
            score=failing_score,
            failed_alpha_record=record,
        )
    )

    assert cred_inserted is True
    assert isinstance(rec_result, RecordResult)
    assert rec_result.inserted is True
    assert rec_result.record_id == 7


@pytest.mark.asyncio
async def test_write_credibility_score_requires_constraint_on_low_score_when_auto_emitting() -> None:
    """Score < 60 + blocking_constraint provided WITHOUT a complete
    record → ValueError. Partial classification is refused (the
    auto-emission path will NOT fabricate the missing fields)."""
    from tpcore.backtest.credibility import CredibilityScore
    from tpcore.backtest.statistical_validation import (
        write_credibility_score_with_failed_alpha,
    )

    failing_score = CredibilityScore(
        score=45,
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=False, monte_carlo_drawdown=False,
        notes=None,
    )

    # Mock pool services the credibility write only.
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value=None)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    with pytest.raises(ValueError, match="WITHOUT a complete"):
        await write_credibility_score_with_failed_alpha(
            pool,
            engine_name="sigma",
            score=failing_score,
            blocking_constraint=BlockingConstraint.DSR_FAILURE,
            failed_alpha_record=None,
        )


@pytest.mark.asyncio
async def test_write_credibility_score_no_failed_alpha_on_pass() -> None:
    """Score ≥ 60 → ledger row NOT written even if a record is
    supplied. A passing credibility is not a research-failure event."""
    from tpcore.backtest.credibility import CredibilityScore
    from tpcore.backtest.statistical_validation import (
        write_credibility_score_with_failed_alpha,
    )

    passing_score = CredibilityScore(
        score=65,
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True,
        notes=None,
    )
    record = FailedAlphaRecord(**_good_record_kwargs())

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)

    cred_inserted, rec_result = (
        await write_credibility_score_with_failed_alpha(
            pool,
            engine_name="sigma",
            score=passing_score,
            failed_alpha_record=record,
        )
    )

    assert cred_inserted is True
    assert rec_result is None  # ledger row NOT written
    # fetchrow (the ledger INSERT) MUST NOT have been called.
    assert conn.fetchrow.await_count == 0


# ─── TEST-F1-13 — Backfill script shape


def test_backfill_script_dry_run_has_exactly_five_edge_validation_records() -> None:
    """The backfill script's hand-coded records must match what
    ``docs/EDGE_VALIDATION_PLAN.md`` reports: Sigma×2, Reversion,
    Vector, Momentum."""
    from scripts.backfill_failed_alpha_ledger import _build_backfill_records

    records = _build_backfill_records()
    assert len(records) == 5, (
        f"Expected 5 EDGE_VALIDATION_PLAN.md records, got {len(records)}"
    )
    engines = [r.engine for r in records]
    assert engines == ["sigma", "sigma", "reversion", "vector", "momentum"]
    sweep_ids = [r.sweep_id for r in records]
    assert sweep_ids == [
        "sigma-2026-05-13", "sigma-2026-05-14",
        "reversion-2026-05-14", "vector-2026-05-14",
        "momentum-2026-05-14",
    ]
    # Every record carries a blocking_constraint (Pydantic enforces;
    # this re-asserts at the data layer).
    for r in records:
        assert isinstance(r.blocking_constraint, BlockingConstraint)
    # Sigma 2026-05-14 is ARCHIVED (per archive/sigma/EULOGY.md).
    sigma_archived = [
        r for r in records
        if r.engine == "sigma" and r.sweep_id == "sigma-2026-05-14"
    ][0]
    assert sigma_archived.status is FailedAlphaStatus.ARCHIVED


# ─── TEST-F1-14 — ledger_source helper


def test_ledger_source_format() -> None:
    """The optional audit-event prefix never collides with the live
    gate's ``backtest_credibility.*`` or the lab ledger's
    ``lab_trial_ledger.*`` namespaces."""
    s = ledger_source("sigma", "sigma-2026-05-13")
    assert s == "failed_alpha_ledger.sigma.sigma-2026-05-13"
    assert not s.startswith("backtest_credibility.")
    assert not s.startswith("lab_trial_ledger.")


# ─── TEST-F1-16 — Module __all__ export surface pinned


def test_ledger_module_export_surface() -> None:
    """Pinning ``__all__`` keeps the public surface deliberate."""
    from tpcore.forensics import alpha_ledger
    expected = {
        "BlockingConstraint",
        "FailedAlphaRecord",
        "FailedAlphaStatus",
        "LEDGER_SCHEMA_VERSION",
        "LEDGER_TABLE",
        "RecordResult",
        "ledger_source",
        "list_failed_alpha",
        "record_failed_alpha",
    }
    assert set(alpha_ledger.__all__) == expected


def test_ledger_table_constant() -> None:
    """The migration uses the literal ``platform.failed_alpha_ledger``;
    the module must reference it via the constant to prevent drift."""
    assert LEDGER_TABLE == "platform.failed_alpha_ledger"
