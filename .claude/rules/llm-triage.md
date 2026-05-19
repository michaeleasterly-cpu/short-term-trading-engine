---
name: llm-triage
paths:
  - "ops/llm_data_triage.py"
  - "ops/engine_llm_triage.py"
description: "Path-scoped rule: LLM triage is advisory-only. Credential-starved worktree, no `tools` param, draft-PR-only, fence-label CI gating, never the detector."
---

# LLM triage — advisory only

Canonical SoT: `ops/llm_data_triage.py` (Ladder rung 5, data lane) + `ops/engine_llm_triage.py` (engine lane R5, symmetric mirror). Heavy-lane rule applies (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Bright lines (non-negotiable; the advisory contract):

- **Data restoration NEVER goes through the LLM.** Only the existing deterministic bounded path keeps data 100%. The LLM is never the detector.
- **Event-driven, NOT scheduled.** Invoked by the sibling `llm_triage_service` daemon polling `platform.application_log` for `DATA_REPAIR_ESCALATED` / `DATA_SOURCE_ESCALATED` (data lane) and `ENGINE_ESCALATED` (engine lane). Mirrors `data_repair_service`/`engine_service`; NOT a `run_data_operations.sh` step.
- **Runs in a credential-starved ephemeral `git worktree`** — no production credentials reachable.
- **Calls official Anthropic `messages.create` with NO `tools` param.** No tool execution; advisory text only.
- **Emits a non-authoritative `DATA_LLM_TRIAGE_PROPOSAL` / `ENGINE_LLM_TRIAGE_PROPOSAL`** event.
- **Produces only a DRAFT, HUMAN-MERGE-ONLY PR** (additive mechanism-free HealSpec / RemediationSpec binding + dossier). NO mechanism change; only a binding to an *existing* verb.
- **Gated by the deterministic provenance + hard-denied label-gated `llm-triage-fence` / `engine-llm-triage-fence` CI check** (credential-starved fence; reuses the one #187 pure fence/canary verbatim for the engine lane — no twin).
- **Two-human review + post-merge canary/shadow only** until a human promotes it.
- **Engine lane: never feeds the Ladder, never mutates/disposes/trades, never edits ladder/supervisor mechanism.** It only triages what the deterministic Ladder already escalated. Selects only `hold_id`s in `engine_ladder.list_undispositioned()` (open + undispositioned + past grace; corrected §7 predicate — never an "unknown class") minus prior-proposal dedup.

The fence is the safety boundary. The LLM is not.

Personas + runbooks: `docs/llm_data_triage_persona.md`, `docs/engine_llm_triage_persona.md`, `docs/llm_data_triage_operator_runbook.md`.
