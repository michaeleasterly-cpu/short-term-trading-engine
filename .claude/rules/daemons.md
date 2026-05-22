---
name: daemons
paths:
  - "ops/engine_service.py"
  - "ops/data_repair_service.py"
  - "ops/lane_service.py"
  - "ops/llm_triage_service.py"
  - "scripts/install_all_daemons.sh"
description: "Path-scoped rule: 'exactly N daemons' invariant; deployed daemon is PERMANENTLY deterministic-only (no LLM triage in repo); mkdir-atomic locks."
---

# Daemon topology

Canonical SoT: `ops/engine_service.py`, `ops/lane_service.py` (deployed deterministic data-repair only), `ops/data_repair_service.py` (library source), `scripts/install_all_daemons.sh`. Heavy-lane rule applies for `engine_service.py` (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Invariants:

- **"Exactly two daemons"** = one long-lived daemon per lane + the data-ops cron. Enforced by `scripts/tests/test_two_daemon_invariant.py` + the `consolidated_daemon_topology --check` probe (DA-3, 2026-05-18).
- **Engine lane**: `engine_service` (consolidated — data-ops-triggered sweep + co-hosted trade-monitor stream + day-rollover weekly-digest trigger; DA-3).
- **Data lane**: `lane_service` (deployed, DETERMINISTIC-ONLY — single `data_repair` co-task) + `data_operations` (cron). The standalone `data_repair_service` was folded into `lane_service` on 2026-05-21; the LLM-invoking co-tasks were removed from `lane_service` on 2026-05-22.
- **PERMANENT deterministic-only invariant — NO LLM triage in the repo.** Operator directive 2026-05-22 ("we aren't going to use the llm triage... take it out") DELETED `ops.llm_data_recovery`, `ops.llm_data_triage`, `ops.engine_llm_triage`, `tpcore.llm_data_triage`, `tpcore.engine_llm_triage`, the two triage slash skills, the CI fence script + label-guard script, and the docs personas. The deterministic cascade catalog (Waves 1-4 + sentinel) is the COMPLETE self-heal layer. NO LLM backstop, NO autonomous fallback — recovery succeeds (emits `INGESTION_AUTO_RECOVERED_*`) or fails (emits `INGESTION_AUTO_RECOVERY_FAILED` and STOPS for operator review).
- **Sentinel tests**: `tests/test_lane_service_no_anthropic.py` reds if (a) the deployed `lane_service` ever transitively pulls `anthropic`, or (b) any of the DELETED LLM-triage modules becomes importable again.
- **Event-driven on `platform.application_log`**, NOT scheduled/linear (operator directive 2026-05-18). New component invocation = sibling daemon mirroring the pattern; never a new cron-only addition.
- **mkdir-atomic self-exclusion lock** for the data-ops loop prevents the scheduled-cycle overlap. It does NOT guard ad-hoc concurrent `ops.py --stage` from a separate process — concurrent `daily_bars` contend on the Supabase pooler.
- **Daemons installed via `scripts/install_all_daemons.sh`** (3-installer launchd label whitelist; the topology invariant test guards against new daemon labels being added without an explicit consolidation case).

What still uses LLM (operator-local only, NOT triage): the SP-G Lab spec-emitter (`ops.llm_lab_emitter`, slash skill `/lab-spec-emit`) and the Task #25 edge-finder (`ops.llm_edge_finder`, slash skill `/lab-edge-find`). Both are Lab-side spec generators, not triage — and neither is deployed. The `ops/llm_triage_service.py` file remains as the local-only orchestrator for those three KEEP co-tasks (lab_emitter + edge_finder + outcome_monitor) but is NOT in `install_all_daemons.sh`.
