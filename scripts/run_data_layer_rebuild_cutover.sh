#!/usr/bin/env bash
# Canonical runner for the Plan 2 data-layer clean-schema cutover.
# Plan: docs/superpowers/plans/2026-06-04-data-layer-rebuild-2-clean-schema-cutover.md
#
# Wraps the cutover steps in one auditable sequence. The destructive TRUNCATE is
# gated TWICE: by REBUILD_WIPE_CONFIRM (the truncate script's own guard) AND by
# requiring the operator to pass STAGE=wipe explicitly — running this with no STAGE
# only does the SAFE prep (snapshots), never the wipe.
#
# Usage:
#   scripts/run_data_layer_rebuild_cutover.sh snapshot   # phase-1 snapshots only (safe)
#   scripts/run_data_layer_rebuild_cutover.sh migrate    # alembic upgrade to 20260604_0500 (drops + dql redesign)
#   REBUILD_WIPE_CONFIRM=I_HAVE_THE_SNAPSHOT_AND_OPERATOR_GO \
#     scripts/run_data_layer_rebuild_cutover.sh wipe     # the irreversible TRUNCATE + tighten (20260604_0600)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
STAGE="${1:-snapshot}"

case "$STAGE" in
  snapshot)
    echo ">> Phase-1 snapshots (PRESERVE-class + full ticker graph)"
    "$PY" scripts/rebuild_snapshot_preserve_tables.py
    "$PY" scripts/rebuild_snapshot_ticker_graph.py
    ;;
  migrate)
    echo ">> Apply migrations through 20260604_0500 (0300 drops + 0500 data_quality_log redesign)"
    bash scripts/run_alembic_upgrade.sh 20260604_0500
    ;;
  wipe)
    echo ">> IRREVERSIBLE: TRUNCATE the ticker graph, then tighten schema (20260604_0600)"
    "$PY" scripts/rebuild_truncate_ticker_graph.py
    bash scripts/run_alembic_upgrade.sh 20260604_0600
    ;;
  *)
    echo "unknown STAGE '$STAGE' (use: snapshot | migrate | wipe)" >&2
    exit 2
    ;;
esac
echo ">> cutover stage '$STAGE' complete."
