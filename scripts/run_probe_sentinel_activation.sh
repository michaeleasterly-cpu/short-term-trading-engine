#!/usr/bin/env bash
# Wrapper for the offline Sentinel activation-score distribution probe
# (`scripts/probe_sentinel_activation.py`). The probe is a read-only
# diagnostic for the FAILED `sentinel_bear_score` Lab dossier
# (`docs/lab/2026-05-21-sentinel_bear_score-FAILED-seed0.md`) —
# checks whether the graduated activation gate is structurally
# dormant or merely threshold-clipped. No Lab spend, no n_trials
# increment, no dossier.
#
# Usage:
#   scripts/run_probe_sentinel_activation.sh
#
# Outputs a JSON sidecar to data/sentinel_activation_probe/<date>.json.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

.venv/bin/python scripts/probe_sentinel_activation.py "$@"
