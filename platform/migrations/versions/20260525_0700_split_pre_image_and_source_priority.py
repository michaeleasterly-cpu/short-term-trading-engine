"""P4 destructive-write protection — pre-image audit + source-priority guard.

The 2026-05-25 trust-audit P4 remediation. Two related artifacts:

A. ``platform.split_pre_image_log`` — every ``apply_split`` call writes
   one row here BEFORE the destructive ``UPDATE platform.prices_daily``
   runs. Captures the affected row count, observed close-price
   ratio, sample pre-image rows, and the planned ratio. If the
   actual UPDATE row count or close-price diff exceeds bounded
   sanity thresholds, the apply is REJECTED and the row stays at
   ``applied=false`` with a ``rejected_reason``. This gives the
   operator a forensic audit trail of every split that touched
   ``prices_daily`` (and every one that was rejected).

B. ``platform._source_priority(source text) -> smallint`` — IMMUTABLE
   SQL function returning a per-source rank. Used as the
   provenance-downgrade guard on the
   ``prices_daily ON CONFLICT DO UPDATE`` clause: a lower-priority
   source (e.g. legacy ``alpaca``) re-running over a row already
   tagged ``fmp`` would silently overwrite the FMP-primary
   provenance per the audit finding. Now the UPDATE only fires
   when the incoming source's priority is ≥ the existing row's.

Source priority rationale (per operator memory
``project_fmp_primary_daily_bars_2026_05_22``):

    fmp     4   primary daily-bars feed since 2026-05-22 (FULL CTA)
    sip     3   Alpaca SIP — paid-tier fallback (richer than iex)
    alpaca  3   Alpaca IEX-default, treated as peer to sip
    iex     2   IEX free-tier (limited coverage)
    tradier 1   legacy / frozen (per P0_5)
    *       0   unknown / NULL (lowest)

Same priority is allowed (a fresh fmp run can re-pull an existing
fmp row — that's a legitimate refresh, not a downgrade).

Revision ID: 20260525_0700
Revises: 20260525_0500
Create Date: 2026-05-25
"""
from alembic import op

revision: str = "20260525_0700"
down_revision: str | None = "20260525_0500"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.split_pre_image_log (
            pre_image_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticker              TEXT NOT NULL,
            action_date         DATE NOT NULL,
            ratio               NUMERIC NOT NULL,
            captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            n_rows_to_update    BIGINT NOT NULL,
            close_before        NUMERIC,
            close_after         NUMERIC,
            observed_ratio      NUMERIC,
            pre_image_sample    JSONB,
            applied             BOOLEAN NOT NULL DEFAULT false,
            applied_at          TIMESTAMPTZ,
            n_rows_actually_updated BIGINT,
            rejected_reason     TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_split_pre_image_log_ticker_date "
        "ON platform.split_pre_image_log (ticker, action_date DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_split_pre_image_log_rejected "
        "ON platform.split_pre_image_log (rejected_reason) "
        "WHERE rejected_reason IS NOT NULL"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION platform._source_priority(s text)
        RETURNS smallint AS $$
            SELECT CASE s
                WHEN 'fmp'     THEN 4::smallint
                WHEN 'sip'     THEN 3::smallint
                WHEN 'alpaca'  THEN 3::smallint
                WHEN 'iex'     THEN 2::smallint
                WHEN 'tradier' THEN 1::smallint
                ELSE 0::smallint
            END;
        $$ LANGUAGE SQL IMMUTABLE;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS platform._source_priority(text)")
    op.execute("DROP TABLE IF EXISTS platform.split_pre_image_log")
