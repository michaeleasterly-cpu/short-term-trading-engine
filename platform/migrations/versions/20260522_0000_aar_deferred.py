"""aar_deferred — deferred After-Action Reports for the E4 self-heal row.

Wave 4 of the deterministic self-heal coverage expansion (see
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` row E4 + §4 answer #4):

    Detection: ``aar_logging`` plug raises during a cycle (DB write
    failure, schema validation error). Recovery: defer the AAR record
    to next cycle's queue. The engine cycle continues; no AAR is lost.

Per §4 answer #4 the substrate is **a new typed table**, not folded
into ``platform.application_log``. AAR has structured fields
(engine_name, trade_id, ticker, aar_data, recorded_at) that don't fit
the JSONB-blob ``application_log`` schema; a typed table also lets the
next-cycle replay query ``WHERE replayed_at IS NULL`` cheaply with an
index — ``application_log`` would need a JSONB GIN scan.

Pattern reference: this mirrors the per-table-discipline used by
``earnings_events_count_snapshot``, ``sec_insider_row_counts_snapshot``,
``ticker_classifications_source_count``, ``ingestion_metrics``.

Shape:

* ``id UUID`` PK (gen_random_uuid()) — the deferred-row id; needed
  because (engine, trade_id) is NOT unique here (a single trade could
  hit the defer path on multiple AAR write attempts if the schema-
  validation error persists; the replay path is idempotent against
  duplicates via the downstream ``platform.aar_events`` write).
* ``engine TEXT NOT NULL`` — engine_name from the AfterActionReport.
* ``trade_id TEXT NOT NULL`` — AAR.trade_id; the natural correlation
  key the operator triages against in application_log.
* ``ticker TEXT NOT NULL`` — AAR.ticker for fast operator triage.
* ``aar_data JSONB NOT NULL`` — full AAR model_dump_json output; the
  replay path rehydrates via ``AfterActionReport.model_validate_json``.
* ``defer_reason TEXT NOT NULL`` — the exception class + truncated
  message so the operator can scan for systemic substrates problems.
* ``recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()`` — when the defer
  happened (NOT when the AAR was originally produced; that's inside
  ``aar_data``).
* ``replayed_at TIMESTAMPTZ NULL`` — null until the replay path
  successfully wrote the AAR to ``platform.aar_events``.

Indexed on ``(replayed_at, recorded_at)`` (partial WHERE replayed_at
IS NULL) — the replay query is "give me the pending defers oldest-
first"; the partial index keeps it cheap as the table grows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260522_0000"
down_revision: str | None = "20260521_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aar_deferred",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("trade_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column(
            "aar_data",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("defer_reason", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "replayed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema="platform",
    )
    op.create_index(
        "ix_aar_deferred_pending",
        "aar_deferred",
        ["recorded_at"],
        unique=False,
        schema="platform",
        postgresql_where=sa.text("replayed_at IS NULL"),
    )
    op.execute(
        "COMMENT ON TABLE platform.aar_deferred IS "
        "'Deferred After-Action Reports — the Wave-4 E4 self-heal "
        "substrate. ``aar_logging`` plug raised during the engine "
        "cycle; the AAR is queued here so the cycle continues and a "
        "later ops.py --stage aar_replay (or the next engine cycle) "
        "rehydrates the row into platform.aar_events. Partial index on "
        "replayed_at IS NULL keeps the replay query cheap.'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_aar_deferred_pending",
        table_name="aar_deferred",
        schema="platform",
    )
    op.drop_table("aar_deferred", schema="platform")
