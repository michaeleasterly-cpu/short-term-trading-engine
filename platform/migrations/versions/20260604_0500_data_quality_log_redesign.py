"""Plan 2 — redesign data_quality_log into the consolidation substrate (spec §3.3).

LIVE (20260509_0000) is a single-purpose freshness-metric log: id bigint,
source, timestamp, latency_ms/missing_bars/stale/confidence (all NOT NULL),
notes text, UNIQUE(source, timestamp), ~6.5K rows. Those rows are REPRODUCED on
the next validation pass, so this drops + recreates rather than migrating data.

Target: uuid id, ``kind`` discriminator, the typed metric columns become
VALIDATION-ONLY + NULLABLE (CHECK ties them to kind='validation'), notes->jsonb,
per-kind partial indexes. Fold sources: fundamentals_period_source_evidence +
parity_drift_log + forensics_triggers (all dropped empty in 0300) become ``kind``
values here. failed_alpha_ledger + ingest_quarantine stay STANDALONE (v1.4).

⚠️ WRITER/READER blast radius (surfaced to the coordinator — see agent report):
  * The canonical validation writer (tpcore/quality/data_quality.DataQualityWriter)
    is updated in this PR to emit the new shape (kind='validation' + jsonb notes).
  * The new schema drops UNIQUE(source, timestamp); the writer's ON CONFLICT
    idempotency is therefore changed to a plain INSERT (uuid PK makes every row
    unique). Idempotency-by-(source,timestamp) is intentionally NOT preserved
    in this minimal shim.
  * The credibility path (tpcore/backtest/statistical_validation.write_credibility_score)
    flows through the SAME DataQualityWriter and populates ``confidence``/``stale``
    (typed cols). Under dql_typed_cols_validation_only that forces kind='validation'
    for those rows too — semantically they are 'backtest_credibility'. The minimal
    shim tags EVERY DataQualityWriter row kind='validation' (the only CHECK-compliant
    minimal path that keeps both suites green); the per-kind writer split is deferred
    to Plan 3/4 (plan Task 5 Step 2 caveat).
  * ALL validation writers now emit the new shape (kind discriminator + jsonb
    notes via the shared tpcore.quality.data_quality.write_row path). Phase 0
    rewired every raw data_quality_log writer — the validation suite, credibility,
    cross-table audit, the folded sidecars (forensics_triggers → kind=
    'forensics_trigger'; parity_drift_log → 'parity_drift'; fundamentals_period_
    source_evidence → 'confirmed_data_gap_evidence'), scripts/audit_data_pipeline.py,
    and the scripts/ops.py stage sites. There are no remaining raw OLD-shape
    (7-col + ON CONFLICT (source, timestamp)) writers; nothing will error at
    runtime against the redesigned table after 0500.
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0500"
# Re-chained 0400→0300 (Plan 2 Phase 0): migration 0400 (count_snapshot→VIEW)
# was DROPPED — earnings_events_count_snapshot is a STATEFUL monotone baseline
# (earnings_events_monotone SELECT … FOR UPDATE + upsert), not a cache, so it
# STAYS a mutable table. The chain is now 0200→0300→0500→0600.
down_revision = "20260604_0300"
branch_labels = None
depends_on = None

KINDS = (
    "validation",
    "confirmed_data_gap_evidence",
    "parity_drift",
    "forensics_trigger",
    "backtest_credibility",
)


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.data_quality_log CASCADE")
    kinds_sql = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(
        f"""
        CREATE TABLE platform.data_quality_log (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind         text NOT NULL CHECK (kind IN ({kinds_sql})),
            source       text NOT NULL,
            "timestamp"  timestamptz NOT NULL,
            latency_ms   integer,
            missing_bars integer,
            stale        boolean,
            confidence   numeric,
            notes        jsonb,
            recorded_at  timestamptz NOT NULL DEFAULT now(),
            -- typed metric columns are VALIDATION-ONLY: populated iff kind='validation'
            CONSTRAINT dql_typed_cols_validation_only CHECK (
                kind = 'validation'
                OR (latency_ms IS NULL AND missing_bars IS NULL
                    AND stale IS NULL AND confidence IS NULL)
            )
        )
        """
    )
    # Partial indexes per hot kind (the live hot path is overwhelmingly validation).
    op.execute(
        'CREATE INDEX ix_dql_validation ON platform.data_quality_log '
        '("timestamp", source) WHERE kind=\'validation\''
    )
    op.execute(
        'CREATE INDEX ix_dql_parity_drift ON platform.data_quality_log '
        '("timestamp") WHERE kind=\'parity_drift\''
    )
    op.execute(
        'CREATE INDEX ix_dql_forensics ON platform.data_quality_log '
        '("timestamp") WHERE kind=\'forensics_trigger\''
    )
    op.execute(
        "CREATE INDEX ix_dql_notes_gin ON platform.data_quality_log USING gin (notes)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.data_quality_log CASCADE")
    op.execute(
        """
        CREATE TABLE platform.data_quality_log (
            id           bigserial PRIMARY KEY,
            source       text NOT NULL,
            "timestamp"  timestamptz NOT NULL,
            latency_ms   integer NOT NULL,
            missing_bars integer NOT NULL DEFAULT 0,
            stale        boolean NOT NULL DEFAULT false,
            confidence   numeric NOT NULL,
            notes        text,
            UNIQUE (source, "timestamp")
        )
        """
    )
