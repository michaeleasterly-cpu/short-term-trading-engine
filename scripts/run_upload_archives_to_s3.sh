#!/usr/bin/env bash
# Wrapper for the Railway-cutover pre-seed archive bulk-upload.
#
# See docs/runbooks/2026-05-25-railway-cutover.md for the full
# cutover sequence. This script is the operator-helper that runs
# scripts/upload_archives_to_s3.py with the .env environment loaded.
#
# Usage:
#   bash scripts/run_upload_archives_to_s3.sh --dry-run
#   bash scripts/run_upload_archives_to_s3.sh --commit
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

exec .venv/bin/python scripts/upload_archives_to_s3.py "$@"
