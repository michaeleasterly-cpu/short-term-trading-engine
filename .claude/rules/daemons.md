---
name: daemons
paths:
  - "ops/engine_service.py"
  - "ops/data_repair_service.py"
  - "ops/llm_triage_service.py"
  - "scripts/install_all_daemons.sh"
description: "Path-scoped rule: 'exactly N daemons' invariant; two-daemon consolidation; event-driven on application_log; mkdir-atomic locks."
---

# Daemon topology

Canonical SoT: `ops/engine_service.py`, `ops/data_repair_service.py`, `ops/llm_triage_service.py`, `scripts/install_all_daemons.sh`. Heavy-lane rule applies for `engine_service.py` / `llm_triage_service.py` (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Invariants:

- **"Exactly two daemons"** = one long-lived daemon per lane + the data-ops cron. Enforced by `scripts/tests/test_two_daemon_invariant.py` + the `consolidated_daemon_topology --check` probe (DA-3, 2026-05-18).
- **Engine lane**: `engine_service` (consolidated — data-ops-triggered sweep + co-hosted trade-monitor stream + day-rollover weekly-digest trigger; DA-3).
- **Data lane**: `data_repair_service` + `data_operations` (cron).
- **Advisory lane** (Epic E B1): `llm_triage_service` — two crash-isolated `_run_supervised` co-tasks (data-lane + engine-lane), event-driven off `application_log`. NOT a new daemon — folded into the existing one.
- **Event-driven on `platform.application_log`**, NOT scheduled/linear (operator directive 2026-05-18). New component invocation = sibling daemon mirroring the pattern; never a new cron-only addition.
- **mkdir-atomic self-exclusion lock** for the data-ops loop prevents the scheduled-cycle overlap. It does NOT guard ad-hoc concurrent `ops.py --stage` from a separate process — concurrent `daily_bars` contend on the Supabase pooler.
- **Daemons installed via `scripts/install_all_daemons.sh`** (4-token launchd label whitelist; the topology invariant test guards against new daemon labels being added without an explicit consolidation case).

LLM-triage daemons are advisory-only — see `llm-triage` rule for the bright lines (credential-starved worktree, no `tools` param, draft-PR-only, fence label gating).
