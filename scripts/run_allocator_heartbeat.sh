#!/usr/bin/env bash
# Allocator heartbeat — safety-net cron when ops/engine_service.py is down.
#
# The allocator is event-driven on DATA_OPERATIONS_COMPLETE via
# ops/engine_dispatch.py (Sub-project C, PR #17, 2026-05-17). This
# cron is a THIN heartbeat, NOT a primary trigger:
#   * if engine='allocator' already emitted a STARTUP row this cadence
#     cycle (WEEKLY_FIRST_TRADING_DAY window), exit clean — daemon ran.
#   * otherwise fire `python scripts/ops.py --allocate` inline once
#     (the SAME canonical command _invoke_allocator uses).
#
# should_fire + the (engine, allocation_date) unique constraint are the
# structural backstops; a race with the daemon can't double-allocate.
#
# Installed by scripts/install_launchd_allocator_heartbeat.sh outside
# the install_all_daemons.sh closed-whitelist for-loop (the two-daemon
# invariant test pins exactly the 4 long-lived/cron installers there;
# this heartbeat is a sibling installer call).
set -uo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a
DATABASE_URL="$DATABASE_URL_IPV4" exec .venv/bin/python -m ops.allocator_heartbeat "$@"
