#!/usr/bin/env bash
# Corpus-fitness audit (4 sections) — operator-on-demand only.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

# Prefer the project .venv (canonical); fall back to /private/tmp/ste-venv
# when the worktree's .venv link is broken (HEAD-tracked .venv self-symlink
# pre-existing defect — does NOT block read-only audits).
if [[ -x .venv/bin/python ]]; then
    PY=.venv/bin/python
elif [[ -x /private/tmp/ste-venv/bin/python ]]; then
    PY=/private/tmp/ste-venv/bin/python
else
    echo "FAILED — no working python venv (.venv or /private/tmp/ste-venv)" >&2
    exit 1
fi

DATABASE_URL="$DATABASE_URL_IPV4" "$PY" scripts/audit_corpus_fitness.py
