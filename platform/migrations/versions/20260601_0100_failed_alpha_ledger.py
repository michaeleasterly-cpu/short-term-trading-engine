"""failed alpha ledger — durable structured research-failure record

Revision ID: 20260601_0100
Revises: 20260530_0300
Create Date: 2026-06-01

F1 from the 2026-06-01 STE × Anthropic audit. ``docs/EDGE_VALIDATION_PLAN.md``
holds critical research-failure findings (Sigma archived, Reversion +
Vector capped at 45/100, Momentum paper-trading per spec, all four
engines failing DSR ≥ 0.95) as prose only. F1 makes those findings
queryable, typed, and constrained — turning the operator's research
narrative into a durable platform substrate the dashboard and any
future Claude reviewer can read directly.

Adds ``platform.failed_alpha_ledger`` (NEW TABLE; no other tables
touched).

Schema (every column typed; no canonical fields stored only in JSON):

  id                  bigserial PRIMARY KEY
  recorded_at         timestamptz NOT NULL DEFAULT NOW()  -- ingest time
  engine              text        NOT NULL                -- canonical engine name
  strategy_family     text                                -- 'mean_reversion' etc.
  sweep_id            text        NOT NULL                -- operator-stable sweep ID
  run_id              text                                -- optional CLI run UUID
  commit_sha          text                                -- code SHA at sweep time
  source_doc          text                                -- e.g. EDGE_VALIDATION_PLAN.md

  data_window_start   date        NOT NULL                -- backtest window start
  data_window_end     date        NOT NULL                -- backtest window end
  universe            text        NOT NULL                -- 'T1+T2', 'all_active', …
  n_trials            integer     NOT NULL CHECK (≥ 0)
  n_trades            integer              CHECK (≥ 0)
  parameter_count     integer              CHECK (≥ 0)

  dsr                 double precision                    -- López de Prado Deflated Sharpe
  psr_at_zero         double precision                    -- P(true Sharpe > 0)
  pbo                 double precision                    -- CSCV Probability of BT Overfitting
  min_btl_periods     integer                             -- Minimum Backtest Length req'd
  backtest_periods    integer                             -- actual observations
  credibility_score   integer     CHECK (0..100)          -- the 0..100 rubric score
  max_drawdown        double precision
  sharpe              double precision
  turnover            double precision
  cost_assumption_bps double precision

  blocking_constraint text        NOT NULL CHECK enum     -- the dispositive reason
  blocking_metric     text                                -- 'n_trades=2', 'DSR=0.42'
  failure_summary     text        NOT NULL CHECK (length > 0)
  revisit_condition   text                                -- 'T3+ fundamentals available'
  status              text        NOT NULL DEFAULT 'FAILED' CHECK enum
  metadata            jsonb                               -- non-canonical extras

  UNIQUE (engine, sweep_id)                               -- backfill idempotency

CHECK enums:

  blocking_constraint ∈ {
    'n_trades_low', 'liquidity_failure', 'dsr_failure', 'psr_failure',
    'pbo_failure', 'min_btl_failure', 'signal_class_mismatch',
    'multi_gate_intersection', 'regime_fragility', 'cost_dominated',
    'data_quality_failure', 'lookahead_or_bias_risk',
    'operator_rejected', 'deprecated_signal_source'
  }
  status ∈ {'FAILED', 'SHELVED', 'ARCHIVED', 'REVISIT_QUEUED'}

Indexes:
  * ix_failed_alpha_ledger_engine (engine)
  * ix_failed_alpha_ledger_blocking_constraint (blocking_constraint)
  * ix_failed_alpha_ledger_status (status)
  * ix_failed_alpha_ledger_recorded_at (recorded_at DESC)
  * ix_failed_alpha_ledger_engine_blocking (engine, blocking_constraint)

Operator hard rules enforced AT THE SCHEMA:
  * blocking_constraint NOT NULL → no graveyard of unclassified failures
  * failure_summary NOT NULL with length > 0 → every row explains itself
  * status CHECK ∈ enum → no drift on lifecycle vocabulary
  * UNIQUE (engine, sweep_id) → re-running the backfill never duplicates

Idempotent migration: CREATE TABLE IF NOT EXISTS; each CHECK is
DROP-then-ADD guarded.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260601_0100"
down_revision: str | None = "20260530_0300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VALID_BLOCKING_CONSTRAINTS = (
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
)
_VALID_STATUSES = (
    "FAILED",
    "SHELVED",
    "ARCHIVED",
    "REVISIT_QUEUED",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.failed_alpha_ledger (
            id                  bigserial PRIMARY KEY,
            recorded_at         timestamptz NOT NULL DEFAULT NOW(),
            engine              text        NOT NULL,
            strategy_family     text,
            sweep_id            text        NOT NULL,
            run_id              text,
            commit_sha          text,
            source_doc          text,
            data_window_start   date        NOT NULL,
            data_window_end     date        NOT NULL,
            universe            text        NOT NULL,
            n_trials            integer     NOT NULL,
            n_trades            integer,
            parameter_count     integer,
            dsr                 double precision,
            psr_at_zero         double precision,
            pbo                 double precision,
            min_btl_periods     integer,
            backtest_periods    integer,
            credibility_score   integer,
            max_drawdown        double precision,
            sharpe              double precision,
            turnover            double precision,
            cost_assumption_bps double precision,
            blocking_constraint text        NOT NULL,
            blocking_metric     text,
            failure_summary     text        NOT NULL,
            revisit_condition   text,
            status              text        NOT NULL DEFAULT 'FAILED',
            metadata            jsonb
        )
        """
    )

    # CHECK constraints — every one DROP-then-ADD so re-runs are safe.
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_blocking_constraint_chk"
    )
    valid_blocking = ", ".join(
        f"'{v}'::text" for v in _VALID_BLOCKING_CONSTRAINTS
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_blocking_constraint_chk "
        f"CHECK (blocking_constraint = ANY(ARRAY[{valid_blocking}]))"
    )

    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_status_chk"
    )
    valid_status = ", ".join(f"'{v}'::text" for v in _VALID_STATUSES)
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_status_chk "
        f"CHECK (status = ANY(ARRAY[{valid_status}]))"
    )

    # Numeric range checks.
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_n_trials_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_n_trials_chk "
        "CHECK (n_trials >= 0)"
    )

    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_n_trades_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_n_trades_chk "
        "CHECK (n_trades IS NULL OR n_trades >= 0)"
    )

    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_parameter_count_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_parameter_count_chk "
        "CHECK (parameter_count IS NULL OR parameter_count >= 0)"
    )

    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_credibility_score_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_credibility_score_chk "
        "CHECK (credibility_score IS NULL OR "
        "       (credibility_score BETWEEN 0 AND 100))"
    )

    # "Every row explains itself" — failure_summary must be non-empty.
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_failure_summary_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "ADD CONSTRAINT failed_alpha_ledger_failure_summary_chk "
        "CHECK (length(trim(failure_summary)) > 0)"
    )

    # Idempotency UNIQUE — re-running the backfill MUST NOT duplicate.
    op.execute(
        "DROP INDEX IF EXISTS platform.ux_failed_alpha_ledger_engine_sweep"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_failed_alpha_ledger_engine_sweep "
        "ON platform.failed_alpha_ledger (engine, sweep_id)"
    )

    # Operator-facing query indexes.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_alpha_ledger_engine "
        "ON platform.failed_alpha_ledger (engine)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_alpha_ledger_blocking_constraint "
        "ON platform.failed_alpha_ledger (blocking_constraint)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_alpha_ledger_status "
        "ON platform.failed_alpha_ledger (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_alpha_ledger_recorded_at "
        "ON platform.failed_alpha_ledger (recorded_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_failed_alpha_ledger_engine_blocking "
        "ON platform.failed_alpha_ledger (engine, blocking_constraint)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_failed_alpha_ledger_engine_blocking"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.ix_failed_alpha_ledger_recorded_at"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.ix_failed_alpha_ledger_status"
    )
    op.execute(
        "DROP INDEX IF EXISTS "
        "platform.ix_failed_alpha_ledger_blocking_constraint"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.ix_failed_alpha_ledger_engine"
    )
    op.execute(
        "DROP INDEX IF EXISTS platform.ux_failed_alpha_ledger_engine_sweep"
    )
    # Constraint drops are implicit via DROP TABLE, but keep explicit
    # for symmetry with the upgrade idempotency pattern.
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_failure_summary_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_credibility_score_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_parameter_count_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_n_trades_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_n_trials_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_status_chk"
    )
    op.execute(
        "ALTER TABLE platform.failed_alpha_ledger "
        "DROP CONSTRAINT IF EXISTS failed_alpha_ledger_blocking_constraint_chk"
    )
    op.execute("DROP TABLE IF EXISTS platform.failed_alpha_ledger")
