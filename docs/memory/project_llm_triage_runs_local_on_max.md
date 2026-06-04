---
name: llm-triage-runs-local-on-max
description: "DEPRECATED / REMOVED 2026-05-22. Operator directive REMOVED the LLM-triage stack entirely — the deterministic cascade catalog (Waves 1-4 + sentinel) is the COMPLETE self-heal layer. No LLM backstop, no operator-local triage. The SP-G lab-emitter and Task #25 finder REMAIN (different purpose — Lab spec generation / edge discovery, NOT triage) and stay operator-local."
metadata:
  node_type: memory
  type: project
  status: DEPRECATED
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

# ARCHITECTURE DECISION 2026-05-22 — LLM TRIAGE REMOVED ENTIRELY

**Operator verbatim (2026-05-22):** "we aren't going to use the llm triage... take it out"

The deterministic cascade catalog shipped under PR #261 / #262 / #267 / #271 / #272 (Waves 1-4 + sentinel — D1-D14 / E1-E11) is the COMPLETE self-heal layer. There is NO LLM backstop.

## What was DELETED

- `ops/llm_data_recovery.py` (the autonomous data-recovery agent — PRs #233 + #239)
- `ops/llm_data_triage.py` + `tpcore/llm_data_triage/` (data-lane triage)
- `ops/engine_llm_triage.py` + `tpcore/engine_llm_triage/` (engine-lane triage)
- `docs/llm_triage_personas/` (data_recovery_v1 + v2 personas)
- `docs/llm_data_triage_persona.md`, `docs/engine_llm_triage_persona.md`
- `docs/llm_data_triage_operator_runbook.md`
- `.claude/skills/triage-data-failures/` + `.claude/skills/triage-engine-failures/`
- `.claude/rules/llm-triage.md`
- `scripts/llm_triage_pr_check.py` (CI fence script)
- `scripts/agent_pr_label_guard.py` (fail-closed label guard)
- `scripts/run_llm_triage_service.sh` (deployment wrapper)
- All triage-targeted tests (`tests/test_engine_llm_triage_agent.py`, `tests/test_llm_data_triage_agent.py`, `tests/test_llm_triage_autonomous_data_recovery.py`, `tests/test_llm_triage_service.py`, the `tpcore/tests/test_*_triage_*.py` set, `scripts/tests/test_agent_pr_label_guard.py`, `scripts/tests/test_llm_triage_pr_check_cleanup.py`)
- The `llm-data-triage-fence` + `engine-llm-triage-fence` + `agent-pr-label-guard` CI jobs

## What was KEPT (NOT triage)

- `ops/llm_lab_emitter.py` + `tpcore/lab/llm_emitter/` — SP-G Lab spec emitter (Lab-side, not triage)
- `ops/llm_edge_finder*.py` + `tpcore/lab/llm_finder/` — Task #25 autonomous edge discovery
- `ops/llm_finder_outcome_monitor.py` — Task #25 outcome monitoring
- `.claude/skills/lab-spec-emit/` + `.claude/skills/lab-edge-find/`
- `ops/llm_triage_service.py` — REFACTORED to host only the KEEP co-tasks (lab_emitter + edge_finder + outcome_monitor). NOT a deployed daemon — operator-local only.

## The new self-heal chain

```
1. daily_bars / data_validation / engine_service / etc. fails
2. Deterministic cascade fires (one of D1-D14 / E1-E11)
3. Recovery succeeds → INGESTION_AUTO_RECOVERED_* event
4. Recovery fails    → INGESTION_AUTO_RECOVERY_FAILED + STOP
                       (no LLM fallback; operator reviews the event)
```

The `_self_heal_failed_stages` (PR #200) generic retry layer stays.

## Sentinels

- `tests/test_lane_service_no_anthropic.py` — strengthened to also assert NONE of the DELETED LLM-triage modules is importable (subprocess parametrize over the deleted set).
- `tests/test_deterministic_cascade_catalog.py` — pins the complete catalog (Waves 1-4 + sentinel).

## Related

- [[deterministic-cascade-architecture]] — the catalog
- [[self-heal-autonomous-no-operator-task]] — recovery is deterministic-only end-to-end; no LLM step
- [[research-llm-edge-discovery]] — Task #25 (Lab edge discovery — NOT triage; survives this directive)

## Reproducibility note

If a future session encounters references in old PRs / specs / memory to `ops.llm_data_recovery` / `ops.llm_data_triage` / `ops.engine_llm_triage` / `tpcore.llm_data_triage` / `tpcore.engine_llm_triage`, those are HISTORICAL — the modules no longer exist. Do not re-create them.
