#!/usr/bin/env bash
# Phase-1 snapshot of the PRESERVE-class ops tables before the Plan 2 cutover.
# These are EXCLUDED from the TRUNCATE, but a verbatim off-DB copy is the
# belt-and-suspenders rollback (the SACRED-carve-out analog for ops state).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
OUT="data/rebuild_2026-06-04/preserve"
mkdir -p "$OUT"
# Session-mode psql via the IPv4 URL; \copy runs client-side (no server FS needed).
PSQL_URL="${DATABASE_URL_IPV4%%\?*}"   # strip any ?params for psql
for t in ingest_manifest allocations risk_close_ledger; do
  psql "$PSQL_URL" -c "\copy (SELECT * FROM platform.$t) TO '$OUT/$t.csv' WITH CSV HEADER"
  echo "snapshot: $t -> $OUT/$t.csv ($(wc -l < "$OUT/$t.csv") lines incl header)"
done
echo "Phase-1 PRESERVE snapshot complete: $OUT"
