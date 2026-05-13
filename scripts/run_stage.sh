#!/usr/bin/env bash
# Run a single stage of scripts/ops.py --update by name. Used by the
# dashboard's per-stage Fix buttons so each red stage can be re-run in
# isolation without paying the full 30-45min daily-update cost.
#
# Usage:
#   scripts/run_stage.sh daily_bars
#   scripts/run_stage.sh corporate_actions
#   scripts/run_stage.sh fundamentals_refresh
#   scripts/run_stage.sh data_validation
#   scripts/run_stage.sh universe_prescreener
#   scripts/run_stage.sh universe_simulation
#
# Each stage emits the same INGESTION_START/COMPLETE/FAILED events to
# platform.application_log as it would inside a full --update — the
# dashboard's Platform-health panel auto-picks up the new run_id.
#
# A Postgres advisory lock keyed on the stage name prevents concurrent
# runs of the same stage from racing each other or a future Railway cron.
set -uo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 1 ]; then
    echo "usage: $0 <stage_name>" >&2
    echo "  stages: daily_bars corporate_actions fundamentals_refresh data_validation universe_prescreener universe_simulation" >&2
    exit 2
fi

STAGE_NAME="$1"

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/ops.py --stage "$STAGE_NAME"
