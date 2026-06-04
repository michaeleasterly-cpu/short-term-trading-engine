---
name: llm-lab-finder-monitor-retired-2026-05-25
description: "Operator-local LLM lab/finder/monitor stack DELETED 2026-05-25 (\"it is out\", Railway-readiness sweep). LAB-EMITTER + EDGE-FINDER + OUTCOME-MONITOR + their tpcore companions + the 2 lab slash skills are gone. AAR critic preserved."
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

⚑ **Operator directive 2026-05-25** ("it is out"): the operator-local LLM lab/finder/monitor stack is retired entirely. This is the second LLM retirement — the first (2026-05-22) deleted the data-side LLM-triage modules; this one closes the operator-local lab agents.

**What was deleted (Railway-readiness retirement):**

| file | role |
|---|---|
| `ops/llm_triage_service.py` | Operator-local orchestrator (multiplexed the 3 lanes below) |
| `ops/llm_lab_emitter.py` | SP-G LAB-EMITTER (`/lab-spec-emit`) |
| `ops/llm_edge_finder.py` + `ops/llm_edge_finder_sdk.py` | Task #25 EDGE-FINDER (`/lab-edge-find`) |
| `ops/llm_finder_outcome_monitor.py` | Task #25 OUTCOME-MONITOR |
| `tpcore/lab/llm_emitter/` (whole package) | SP-G engine-free contract layer |
| `tpcore/lab/llm_finder/` (whole package) | Task #25 engine-free contract layer |
| `.claude/skills/lab-spec-emit/` + `.claude/skills/lab-edge-find/` | Slash-skill wrappers |
| `tests/test_llm_lab_emitter.py` + `ops/tests/test_llm_edge_finder*.py` + `ops/tests/test_llm_finder_outcome_monitor.py` | Coverage for the above |
| `docs/runbooks/lab-spec-emit-orphaned-spend.md` + `docs/runbooks/llm_edge_finder_operator_runbook.md` | Operator-facing runbooks |

**What was PRESERVED (NOT in scope of "it is out"):**

* `ops/llm_aar_critic*.py` + `ops/llm_aar_anthropic_ids.py` + `tpcore/lab/llm_aar/` — the AAR critic. Operator-local (Claude Max session), never deployed. Distinct epic.
* `tpcore/lab/regime_tuple.py` (NEW) — extracted `compute_regime_tuple_id` SHA12 primitive from the retired `tpcore.lab.llm_finder.models`. `reversion/regime_filter.py` consumes it; preserves byte-identical regime IDs for Lab probes that were registered against the finder-era tuples.

**Sentinel coverage updated:**

* `tests/test_lane_service_no_anthropic.py::_DELETED_LLM_TRIAGE_MODULES` extended with the 2026-05-25 deletions (7 ops modules + 2 tpcore packages). The deleted-modules sentinel reds CI if any are re-introduced.
* `tests/test_claude_skills_present.py::_SKILLS` no longer requires `lab-spec-emit` / `lab-edge-find`.
* `vulture_allowlist.py` pruned (69 dead-code entries removed for deleted symbols).
* `CLAUDE.md` `ops/` line + `.claude/rules/daemons.md` text updated.
* `TODO.md` epic L515 (LOCAL-LLM-BRIDGE) + L598 (Task #25) marked CLOSED 2026-05-25 with historical scope retained.

**Why retired:** Railway-readiness sweep. The 3 lanes were Mac-local-only by design (they needed the operator's Claude Max session, not an API key). Operator decision: keep the architecture small and Railway-only for the deployed side; LLM work that needs Max stays as the AAR critic only. The autonomous-finder thesis (first real edge-signal 2026-05-22, see [[finder-first-edge-signal]]) remains valid as research substrate but the LLM-driven discovery loop is shut down.

**Sibling work (same session):**

* Cutover runbook at `docs/runbooks/2026-05-25-railway-cutover.md` (operator-driven sequence, no deploy this session)
* `scripts/upload_archives_to_s3.py` — one-shot bulk-upload of `data/*_archive/` to bucket before flipping `CSV_ARCHIVE_BACKEND=s3`
* Memstore handoff posted to dev memstore (`memstore_01P5DiJJgau4NhMMekaZDQEN`) at `/handoffs/2026-05-25-railway-readiness-for-engine-session.md` (mem_01WTo2t2JDDig5yygbnVvtGV) instructing the engine session that whatever they build must run on Railway.

Related: [[finder-first-edge-signal]], [[canary-is-end-to-end-heartbeat]], [[db-is-substrate-not-engine-inputs]] (the philosophy that justifies keeping the AAR critic + the regime_tuple primitive as substrate).
