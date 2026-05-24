"""Corporate-history enrichment epic — P1 schema (issuers + issuer_securities
+ issuer_history + corporate_events bitemporal).

Per spec docs/superpowers/specs/2026-05-24-corporate-history-enrichment.md
v0.2. Creates the four-table substrate that the seed stage (P2) loads
and that the SEC EDGAR extractor (P3, future) extends.

Schema details (full rationale in the spec):
  - issuers: stable point-in-time legal-entity identity. operator-minted
    issuer_id PK (matches v2.2 cross-vendor pattern); CIK + LEI as
    nullable UNIQUE columns.
  - issuer_securities: M:N issuer↔ticker_classifications mapping with
    SCD-2 timeline. Handles GOOG/GOOGL (one Alphabet issuer → two
    securities) and merger-driven security transfer between issuers.
  - issuer_history: SCD-2 legal-name / CIK changes per issuer.
  - corporate_events: bitemporal M&A graph. Bitemporal because M&A
    announcements get amended (deal sweeteners, revised ratios,
    terminations + re-announcements) — preserves audit trail.

Event-kind taxonomy is enforced via CHECK constraint (16 kinds; widened
from spec v0.1 9 per expert review).

Revision ID: 20260524_1600
Revises: 20260524_1500
Create Date: 2026-05-24
"""
from alembic import op

revision: str = "20260524_1600"
down_revision: str | None = "20260524_1500"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# Event-kind taxonomy (16 kinds per spec §3.5).
_EVENT_KINDS: tuple[str, ...] = (
    "merger", "acquisition", "spinoff", "reverse_merger",
    "rename", "name_only_change", "cik_change", "ticker_swap",
    "take_private", "going_private_transaction",
    "bankruptcy_reorg", "bankruptcy_liquidation",
    "delisting", "asset_sale", "asset_sale_partial",
    "recapitalization", "share_class_collapse", "going_concern_warning",
    "fdic_receivership",  # added 2026-05-24 per seed CSV operator suggestion
)
_EVENT_KIND_CHECK = "event_kind IN (" + ", ".join(
    f"'{k}'" for k in _EVENT_KINDS
) + ")"


def upgrade() -> None:
    # 1. issuers — operator-minted PK, nullable cross-vendor IDs.
    op.execute("""
        CREATE TABLE IF NOT EXISTS platform.issuers (
            issuer_id          text        NOT NULL,
            cik                text                NULL,
            lei                char(20)            NULL,
            legal_name         text        NOT NULL,
            country_of_incorp  char(2)             NULL,
            status             text        NOT NULL DEFAULT 'active',
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT issuers_pk PRIMARY KEY (issuer_id),
            CONSTRAINT issuers_cik_uniq UNIQUE (cik),
            CONSTRAINT issuers_lei_uniq UNIQUE (lei),
            CONSTRAINT issuers_status_chk CHECK (
                status IN ('active','dissolved','merged','private')
            )
        )
    """)

    # 2. issuer_securities — M:N issuer↔ticker_classifications.
    op.execute("""
        CREATE TABLE IF NOT EXISTS platform.issuer_securities (
            issuer_id          text NOT NULL,
            classification_id  text NOT NULL,
            share_class        text         NULL,
            valid_from         date NOT NULL,
            valid_to           date         NULL,
            notes              text         NULL,
            CONSTRAINT issuer_securities_pk
                PRIMARY KEY (issuer_id, classification_id, valid_from),
            CONSTRAINT issuer_securities_issuer_fk
                FOREIGN KEY (issuer_id) REFERENCES platform.issuers(issuer_id),
            CONSTRAINT issuer_securities_security_fk
                FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_issuer_securities_security
            ON platform.issuer_securities (classification_id, valid_from)
    """)

    # 3. issuer_history — SCD-2 legal-name / CIK changes.
    op.execute("""
        CREATE TABLE IF NOT EXISTS platform.issuer_history (
            issuer_id    text         NOT NULL,
            cik          text                 NULL,
            legal_name   text         NOT NULL,
            valid_from   date         NOT NULL,
            valid_to     date                 NULL,
            source       text         NOT NULL,
            recorded_at  timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT issuer_history_pk PRIMARY KEY (issuer_id, valid_from),
            CONSTRAINT issuer_history_issuer_fk
                FOREIGN KEY (issuer_id) REFERENCES platform.issuers(issuer_id)
        )
    """)

    # 4. corporate_events — bitemporal M&A graph.
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS platform.corporate_events (
            event_id              text        NOT NULL,
            event_kind            text        NOT NULL,
            event_date            date        NOT NULL,
            announced_date        date                NULL,
            predecessor_cls_id    text                NULL,
            successor_cls_id      text                NULL,
            predecessor_issuer_id text                NULL,
            successor_issuer_id   text                NULL,
            successor_external    text                NULL,
            ratio_num             numeric             NULL,
            ratio_den             numeric             NULL,
            cash_per_share        numeric             NULL,
            extra_terms           jsonb               NULL,
            source                text        NOT NULL,
            source_filing_url     text                NULL,
            notes                 text                NULL,
            realtime_start        timestamptz NOT NULL DEFAULT now(),
            realtime_end          timestamptz NOT NULL DEFAULT 'infinity',
            recorded_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT corporate_events_pk PRIMARY KEY (event_id, realtime_start),
            CONSTRAINT corporate_events_kind_chk CHECK ({_EVENT_KIND_CHECK}),
            CONSTRAINT corporate_events_predecessor_fk
                FOREIGN KEY (predecessor_cls_id)
                REFERENCES platform.ticker_classifications(id),
            CONSTRAINT corporate_events_successor_fk
                FOREIGN KEY (successor_cls_id)
                REFERENCES platform.ticker_classifications(id),
            CONSTRAINT corporate_events_predecessor_issuer_fk
                FOREIGN KEY (predecessor_issuer_id)
                REFERENCES platform.issuers(issuer_id),
            CONSTRAINT corporate_events_successor_issuer_fk
                FOREIGN KEY (successor_issuer_id)
                REFERENCES platform.issuers(issuer_id)
        )
    """)

    # Hot read paths.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_corp_events_predecessor
            ON platform.corporate_events (predecessor_cls_id, event_date)
            WHERE realtime_end = 'infinity'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_corp_events_successor
            ON platform.corporate_events (successor_cls_id, event_date)
            WHERE realtime_end = 'infinity'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_corp_events_pred_issuer
            ON platform.corporate_events (predecessor_issuer_id, event_date)
            WHERE realtime_end = 'infinity'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_corp_events_succ_issuer
            ON platform.corporate_events (successor_issuer_id, event_date)
            WHERE realtime_end = 'infinity'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_corp_events_date
            ON platform.corporate_events (event_date)
            WHERE realtime_end = 'infinity'
    """)


def downgrade() -> None:
    # Tables drop in FK-reverse order. corporate_events references issuers
    # + ticker_classifications; issuer_securities references both.
    op.execute("DROP TABLE IF EXISTS platform.corporate_events")
    op.execute("DROP TABLE IF EXISTS platform.issuer_history")
    op.execute("DROP TABLE IF EXISTS platform.issuer_securities")
    op.execute("DROP TABLE IF EXISTS platform.issuers")
