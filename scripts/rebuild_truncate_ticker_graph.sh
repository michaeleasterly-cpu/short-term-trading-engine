#!/usr/bin/env bash
# Plan 2 Task 7 — the irreversible ticker-graph wipe. One TRUNCATE statement so
# the mutual classification_id FKs are satisfiable. EXCLUDES macro_data + the
# PRESERVE-class ops tables. Session-mode :5432 (DDL/TRUNCATE; not the pooler).
#
# The TRUNCATE list below is FK-COMPLETE (Task 6 verification, live FK map
# 2026-06-04): every child of a truncated parent is itself in the statement.
# Parents truncated: ticker_classifications + issuers and all their children.
# options_max_pain (a ticker_classifications child) is EXCLUDED because it is
# DROPPED by migration 20260604_0300, which applies BEFORE this wipe. By contrast
# ticker_lifecycle_events is KEPT (its corporate_events fold is deferred to Plan 3)
# so it IS in the TRUNCATE (it FKs ticker_classifications). ingest_quarantine ->
# ingest_manifest is NOT forced in: ingest_manifest is PRESERVE (not truncated).
# Re-verify FK-completeness at execution (Task 6) before running.
#
# PRECONDITION (operator-gated, Task 7 Step 1): PRESERVE snapshot + Supabase
# on-demand snapshot + PITR anchor recorded; writers paused; migrations
# 0300->0500 applied (alembic current == 20260604_0500); operator GO given.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
[[ "${REBUILD_WIPE_CONFIRM:-}" == "I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO" ]] || {
  echo "Refusing: set REBUILD_WIPE_CONFIRM=I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO" >&2; exit 1; }
PSQL_URL="${DATABASE_URL_IPV4%%\?*}"
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -c "
TRUNCATE TABLE
  platform.prices_daily, platform.prices_daily_staging, platform.fundamentals_quarterly,
  platform.ticker_classifications, platform.ticker_history, platform.issuers,
  platform.issuer_securities, platform.issuer_history, platform.corporate_events,
  platform.corporate_actions, platform.earnings_events, platform.short_interest,
  platform.borrow_rates, platform.insider_transactions, platform.insider_sentiment,
  platform.social_sentiment, platform.sec_material_events, platform.spread_observations,
  platform.liquidity_tiers, platform.universe_candidates, platform.aar_events,
  platform.ticker_lifecycle_events
  RESTART IDENTITY;"
echo "WIPE complete."
