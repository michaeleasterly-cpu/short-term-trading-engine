"""F1 (2026-06-01) — schema sentinel for the failed-alpha ledger migration.

Static parse of ``platform/migrations/versions/20260601_0100_
failed_alpha_ledger.py``. Verifies the migration declares the
load-bearing pieces of the schema:

  * Table name = ``platform.failed_alpha_ledger``
  * Down-revision pinned to the prior head (``20260530_0300``)
  * Required NOT NULL columns present
  * CHECK constraints for blocking_constraint + status + score range
    + n_trials non-negative + failure_summary non-empty
  * UNIQUE (engine, sweep_id) idempotency index

Static-source assertion (no live DB needed) — CI runs without an
operator DSN, and we want this sentinel to red on a deliberate
migration mutation regardless of the env. Live-DB introspection is
exercised by ``tests/test_failed_alpha_ledger.py`` via mocked pools.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _REPO / "platform" / "migrations" / "versions"
    / "20260601_0100_failed_alpha_ledger.py"
)


def _src() -> str:
    assert _MIGRATION.is_file(), f"missing migration: {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_revision_pinned() -> None:
    src = _src()
    assert 'revision: str = "20260601_0100"' in src
    assert 'down_revision: str | None = "20260530_0300"' in src


def test_migration_creates_failed_alpha_ledger_table() -> None:
    src = _src()
    assert "CREATE TABLE IF NOT EXISTS platform.failed_alpha_ledger" in src


def test_migration_declares_required_not_null_columns() -> None:
    """The operator hard rules surface as SQL NOT NULL on the load-
    bearing fields: engine, sweep_id, data_window_start,
    data_window_end, universe, n_trials, blocking_constraint,
    failure_summary, status."""
    src = _src()
    required_not_null = (
        "engine              text        NOT NULL",
        "sweep_id            text        NOT NULL",
        "data_window_start   date        NOT NULL",
        "data_window_end     date        NOT NULL",
        "universe            text        NOT NULL",
        "n_trials            integer     NOT NULL",
        "blocking_constraint text        NOT NULL",
        "failure_summary     text        NOT NULL",
        "status              text        NOT NULL DEFAULT 'FAILED'",
    )
    for col in required_not_null:
        assert col in src, f"migration missing required NOT NULL column: {col}"


def test_migration_declares_check_constraints() -> None:
    src = _src()
    # blocking_constraint enum.
    assert "failed_alpha_ledger_blocking_constraint_chk" in src
    # status enum.
    assert "failed_alpha_ledger_status_chk" in src
    # numeric range guards.
    assert "failed_alpha_ledger_n_trials_chk" in src
    assert "failed_alpha_ledger_n_trades_chk" in src
    assert "failed_alpha_ledger_parameter_count_chk" in src
    assert "failed_alpha_ledger_credibility_score_chk" in src
    # failure_summary non-empty.
    assert "failed_alpha_ledger_failure_summary_chk" in src
    assert "length(trim(failure_summary)) > 0" in src


def test_migration_blocking_constraint_enum_matches_python() -> None:
    """The SQL CHECK enum must include every value the Pydantic
    BlockingConstraint declares. Drift is caught here."""
    src = _src()
    from tpcore.forensics.alpha_ledger import BlockingConstraint
    for value in BlockingConstraint:
        assert f"\"{value.value}\"" in src or f"'{value.value}'" in src, (
            f"SQL CHECK enum missing BlockingConstraint value: "
            f"{value.value!r} — migration drift"
        )


def test_migration_status_enum_matches_python() -> None:
    src = _src()
    from tpcore.forensics.alpha_ledger import FailedAlphaStatus
    for value in FailedAlphaStatus:
        assert f"\"{value.value}\"" in src or f"'{value.value}'" in src, (
            f"SQL CHECK enum missing FailedAlphaStatus value: "
            f"{value.value!r} — migration drift"
        )


def test_migration_declares_unique_engine_sweep() -> None:
    """ON CONFLICT DO NOTHING depends on UNIQUE (engine, sweep_id)
    — the idempotency invariant of the backfill script."""
    src = _src()
    assert "ux_failed_alpha_ledger_engine_sweep" in src
    assert "CREATE UNIQUE INDEX" in src
    assert "(engine, sweep_id)" in src


def test_migration_declares_operator_facing_indexes() -> None:
    """The dashboard queries by (engine), (blocking_constraint),
    (status), (recorded_at DESC), and (engine, blocking_constraint)."""
    src = _src()
    for ix in (
        "ix_failed_alpha_ledger_engine",
        "ix_failed_alpha_ledger_blocking_constraint",
        "ix_failed_alpha_ledger_status",
        "ix_failed_alpha_ledger_recorded_at",
        "ix_failed_alpha_ledger_engine_blocking",
    ):
        assert ix in src, f"missing query index: {ix}"


def test_migration_downgrade_drops_table_and_indexes() -> None:
    """Symmetric downgrade — DROP INDEX ... DROP TABLE."""
    src = _src()
    assert "DROP TABLE IF EXISTS platform.failed_alpha_ledger" in src
    assert "DROP INDEX IF EXISTS platform.ix_failed_alpha_ledger_engine" in src
    assert "DROP INDEX IF EXISTS platform.ux_failed_alpha_ledger_engine_sweep" in src
