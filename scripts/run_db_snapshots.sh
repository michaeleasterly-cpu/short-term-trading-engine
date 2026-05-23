#!/usr/bin/env bash
# Wrapper for scripts/db_snapshots.py — ON-DEMAND pre-cleanup snapshot.
#
# **Not a backup.** Supabase Pro provides daily backups + 7-day PITR;
# that is the durable backup story. This wrapper captures a one-shot
# `COPY (SELECT *) FROM platform.<table>` for the table(s) named on
# the command line, into `data/db_snapshots/<table>/<utc_stamp>.csv.gz`
# with a per-snapshot manifest.json (row counts + sha256 + alembic rev).
#
# Re-scoped 2026-05-23 per v2.1 spec corrections (was daily-scheduled
# with 30d retention; Phase 0.6 pg_dump regimen DROPPED).
#
# Usage:
#   bash scripts/run_db_snapshots.sh prices_daily
#   bash scripts/run_db_snapshots.sh corporate_actions fundamentals_quarterly
#
# Delete the snapshot files after the cleanup PR is verified.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/db_snapshots.py "$@"
