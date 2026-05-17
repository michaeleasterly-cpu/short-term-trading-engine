"""provider_binding_state — runtime overlay for the ProviderBinding SoT

Automated CUTOVER (Data Provider Lifecycle, spec §10: cutover is
automated, not operator-confirmed) requires a feed's ACTIVE provider to
be runtime-mutable. ``tpcore/providers.py`` ``_BINDINGS`` is the frozen
code SoT (declared defaults + which providers exist + parity-verified
fallbacks); this table is the thin live overlay the cutover agent
flips — symmetric to how ``ingestion_jobs`` overlays config. Resolution
is: overlay row if present, else the code-declared ACTIVE.

One row per feed (only the *active* selection is mutable at runtime;
status of the other bindings stays code-declared). Fully idempotent.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260517_0900"
down_revision: str | None = "20260516_0800"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.provider_binding_state (
            feed            text PRIMARY KEY,
            active_provider text        NOT NULL,
            reason          text        NOT NULL DEFAULT '',
            updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform.provider_binding_state")
