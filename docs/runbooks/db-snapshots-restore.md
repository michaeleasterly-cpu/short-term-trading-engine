# db_snapshots/ restore runbook

Per Phase 0.5 of the v2.1 referential-integrity rollout. The `scripts/db_snapshots.py` producer is ON-DEMAND (re-scoped 2026-05-23): invoked manually right before a Phase 4 cleanup PR for the specific table(s) being cleaned up. It writes per-table CSV.gz + a manifest. This runbook is the restore protocol.

## Recovery decision tree

When the live `platform.*` schema is degraded:

1. **Recent table-level data loss (a few hours ago, single table accidentally TRUNCATEd)**: Supabase Pro 7-day PITR is the fastest path. Restore-to-timestamp inside the Supabase project. Minutes to recover.
2. **Pre-cleanup rollback during Phase 4** (the primary use case for this substrate): the operator-controlled rollback path. \COPY from the most recent snapshot back into the live table after a bad cleanup.
3. **Tenant loss / Supabase project deleted / region outage**: Supabase Pro's daily backups are the right path — operator-restorable via the Supabase project dashboard. THIS substrate is for in-tenant pre-cleanup rollback only. (A self-managed `pg_dump` regimen was considered as Phase 0.6 but DROPPED 2026-05-23 since Supabase Pro already provides this coverage.)

## Per-table restore protocol

```bash
# Pick the snapshot you want to restore from. List recent manifests:
ls -t data/db_snapshots/*_manifest.json | head -5

# Pick a stamp, e.g. 20260523T230000Z:
STAMP=20260523T230000Z
TABLE=prices_daily

# Sanity-check the manifest BEFORE you touch the live table:
jq ".tables.\"$TABLE\"" data/db_snapshots/${STAMP}_manifest.json
# Should show {rows, sha256, size_bytes, path, duration_s}

# Verify the file's sha256 matches the manifest:
GZ=$(jq -r ".tables.\"$TABLE\".path" data/db_snapshots/${STAMP}_manifest.json)
EXPECTED=$(jq -r ".tables.\"$TABLE\".sha256" data/db_snapshots/${STAMP}_manifest.json)
ACTUAL=$(shasum -a 256 "$GZ" | awk '{print $1}')
[[ "$EXPECTED" == "$ACTUAL" ]] && echo "sha256 OK" || echo "MISMATCH — DO NOT RESTORE"

# Load into a temp table FIRST so you can compare row counts before
# touching live:
psql "$DATABASE_URL" -c "CREATE TEMP TABLE ${TABLE}_restored (LIKE platform.${TABLE} INCLUDING ALL)"
gunzip -c "$GZ" | psql "$DATABASE_URL" -c "\COPY ${TABLE}_restored FROM STDIN WITH (FORMAT csv, HEADER true)"
psql "$DATABASE_URL" -c "SELECT count(*) FROM ${TABLE}_restored"
# Compare against manifest.tables.<TABLE>.rows — should match.

# If the live table is the issue, REPLACE with a transaction:
psql "$DATABASE_URL" <<SQL
BEGIN;
-- For Phase 4 cleanup rollback: SET session_replication_role = replica;
-- to bypass FK checks during the swap, ONLY if you're confident the
-- snapshot was taken when the FK invariants held.
TRUNCATE platform.${TABLE};
INSERT INTO platform.${TABLE} SELECT * FROM ${TABLE}_restored;
-- Verify before commit:
SELECT count(*) FROM platform.${TABLE};
-- If correct:
COMMIT;
-- If wrong, ROLLBACK.
SQL
```

## Full-schema restore is NOT in scope for this substrate

This substrate's per-table CSV.gz files **assume the schema already exists**. They don't replay DDL. Use this substrate only when:
- The schema (tables, FKs, indexes) is intact
- Specific table data needs replacement

For a full schema-and-data restore in a fresh database, use **Supabase Pro's daily backups** (restorable via the Supabase project dashboard). The earlier-considered Phase 0.6 self-managed `pg_dump` regimen was dropped 2026-05-23 since Supabase Pro covers this case.

## Snapshot order on restore

If multiple tables need restoring, follow the FK dependency order (parents first):

1. `ticker_classifications` (FK parent)
2. The 14 FK-protected children in any order (FKs check parent existence; with parent restored, all children can restore)
3. `application_log`, `data_quality_log` (no FK; any order)

## When to trust which snapshot

The manifest's `alembic_revision` field identifies which schema version the snapshot was taken under. If the current live schema's alembic head is DIFFERENT from the snapshot's recorded revision:
- The snapshot may not be schema-compatible
- Either `alembic downgrade` to the snapshot's revision FIRST, OR
- Use a more recent snapshot taken under the current schema

## Rollback drill (quarterly)

To verify the restore protocol stays sharp, every quarter:

```bash
# Restore the latest prices_daily snapshot into a throwaway local Postgres:
docker run --rm -d -p 5434:5432 -e POSTGRES_PASSWORD=test --name pg-drill postgres:15
sleep 5
docker exec pg-drill psql -U postgres -c "CREATE SCHEMA platform"
docker exec pg-drill psql -U postgres -c "CREATE TABLE platform.prices_daily (ticker text, date date, open numeric, high numeric, low numeric, close numeric, volume bigint, adjusted_close numeric, delisted boolean, delisting_date date, source text)"
# Copy the latest .csv.gz in and verify row count:
LATEST=$(ls -t data/db_snapshots/prices_daily/*.csv.gz | head -1)
gunzip -c "$LATEST" | docker exec -i pg-drill psql -U postgres -c "\COPY platform.prices_daily FROM STDIN WITH (FORMAT csv, HEADER true)"
docker exec pg-drill psql -U postgres -c "SELECT count(*) FROM platform.prices_daily"
# Should match manifest.tables.prices_daily.rows
docker stop pg-drill
```

## Related

- `scripts/db_snapshots.py` — the producer
- `scripts/run_db_snapshots.sh` — wrapper
- `docs/runbooks/csv-archive-retention.md` — sibling retention policy for vendor-shape archives
- `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.1.md` §2.5 — Phase 0.5 spec
