#!/usr/bin/env bash
# Apply pending Alembic migrations to the local Supabase project.
# Always uses the IPv4 pooler URL — the IPv6 URL is for Railway only
# (see memory: project_supabase_dual_db_urls.md).
#
# Usage:
#   scripts/run_alembic_upgrade.sh           # upgrade head
#   scripts/run_alembic_upgrade.sh <rev>     # upgrade to a specific rev
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

TARGET="${1:-head}"

DATABASE_URL="postgresql+asyncpg://${DATABASE_URL_IPV4#postgresql://}" \
  .venv/bin/alembic -c platform/migrations/alembic.ini upgrade "$TARGET"
