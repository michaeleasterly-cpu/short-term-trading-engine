---
name: daemons
paths:
  - "ops/engine_service.py"
  - "ops/data_repair_service.py"
  - "ops/lane_service.py"
  - "ops/llm_triage_service.py"
  - "scripts/install_all_daemons.sh"
description: "Path-scoped rule: 'exactly N daemons' invariant; deployed daemon is deterministic-only; LLM triage is operator-local (Max account); mkdir-atomic locks."
---

# Daemon topology

Canonical SoT: `ops/engine_service.py`, `ops/lane_service.py` (deployed deterministic data-repair only), `ops/data_repair_service.py` (library source), `scripts/install_all_daemons.sh`. Heavy-lane rule applies for `engine_service.py` (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Invariants:

- **"Exactly two daemons"** = one long-lived daemon per lane + the data-ops cron. Enforced by `scripts/tests/test_two_daemon_invariant.py` + the `consolidated_daemon_topology --check` probe (DA-3, 2026-05-18).
- **Engine lane**: `engine_service` (consolidated — data-ops-triggered sweep + co-hosted trade-monitor stream + day-rollover weekly-digest trigger; DA-3).
- **Data lane**: `lane_service` (deployed, DETERMINISTIC-ONLY — single `data_repair` co-task) + `data_operations` (cron). The standalone `data_repair_service` was folded into `lane_service` on 2026-05-21; the LLM-invoking co-tasks were removed from `lane_service` on 2026-05-22 (operator directive — see `llm-triage-runs-local-on-max` memory + `docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md`).
- **NO LLM in the deployed daemon set.** Per operator directive 2026-05-21 ("we wont be deploying the llm data triage it will run locally with my max account") the LLM-side runs OPERATOR-LOCALLY via the slash skills `/triage-data-failures` (data lane autonomous recovery), `/triage-engine-failures` (engine lane advisory + draft PR), `/lab-spec-emit` (SP-G). Sentinel test: `tests/test_lane_service_no_anthropic.py` (subprocess-import sentinel — reds if `ops.lane_service` ever transitively pulls in `anthropic`).
- **Event-driven on `platform.application_log`**, NOT scheduled/linear (operator directive 2026-05-18). New component invocation = sibling daemon mirroring the pattern; never a new cron-only addition. The deployed deterministic cascade STILL emits escalation events; the operator-local LLM side observes them.
- **mkdir-atomic self-exclusion lock** for the data-ops loop prevents the scheduled-cycle overlap. It does NOT guard ad-hoc concurrent `ops.py --stage` from a separate process — concurrent `daily_bars` contend on the Supabase pooler.
- **Daemons installed via `scripts/install_all_daemons.sh`** (3-installer launchd label whitelist; the topology invariant test guards against new daemon labels being added without an explicit consolidation case).

LLM-triage is operator-local — the deployed daemon must NEVER call Anthropic at runtime. See `llm-triage` rule for the advisory bright lines that apply to the operator-local invocations.
