#!/usr/bin/env bash
# Emit the weekly data-layer state-comprehension digest.
#
# NOT a long-running daemon — a periodic one-shot. emit_digest is
# idempotent per ISO week (it dedups on the WEEKLY_DIGEST row), so it
# is safe (and intentionally) scheduled DAILY: it no-ops until a new
# ISO week, which makes it resilient to a single missed day far better
# than a precise weekly cron. Pushes a one-page digest to
# platform.application_log (+ best-effort local notification); the
# operator acknowledges with `python -m ops.weekly_digest ack`. Two
# consecutive unacked weeks auto-de-escalates live trading
# (ops.weekly_digest.live_clearance).
#
# Usage: scheduled via scripts/install_launchd_weekly_digest.sh, or
# run by hand. Logs to weekly-digest.log under launchd; stdout else.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

# IPv4 pooler — Supabase direct DSN is IPv6-only and not reachable
# from launchd's network namespace on macOS.
exec env DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" \
    .venv/bin/python -m ops.weekly_digest emit
