---
name: deterministic-cascade-architecture
description: The deterministic-first self-heal architecture that emerged from PRs
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Architectural concept (emerged 2026-05-21, formalized in spec `docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md`; UPDATED 2026-05-22 per operator directive "we aren't going to use the llm triage... take it out"):** the data-lane + engine-lane self-heal is **deterministic-only**. The deterministic cascade catalog (Waves 1-4 + sentinel — D1-D14 / E1-E11) is the COMPLETE self-heal layer. On cascade exhaustion the daemon emits `INGESTION_AUTO_RECOVERY_FAILED` and STOPS — operator reviews. **There is no LLM backstop.**

## The cascade pattern (Wave 1 example, generalizable)

Each failure shape lives in `scripts/ops.py` as four coordinated pieces:

1. **A cascade decision-point map** — e.g. `_VALIDATION_CASCADE_MAP: dict[str, Callable]` keyed by the failed-check name, mapping to a recovery function. New failure shapes add an entry.
2. **A cascade function** — `_auto_cascade_<failure>` — sibling to `_auto_cascade_coverage_collapse` (PR #227 prototype). Called from `cmd_update` AFTER stage failure, BEFORE `_self_heal_failed_stages`. Returns whether the cascade ran + the recovery outcome.
3. **An event name** — `INGESTION_AUTO_RECOVERED_<shape>` for success, `<SHAPE>_ESCALATED` for the long-tail-to-operator path. Each cascade emits ONE terminal event so application_log shows the operator what happened.
4. **A regression test** — fail-on-main, pass-on-branch. Asserts the cascade's decision-point fires on the expected failure shape AND does NOT fire on unrelated failures.

## Failure-mode catalog (current state)

Per the spec § 1+2 catalog:

**Data-lane:**
- D1 coverage_collapse → `_auto_cascade_coverage_collapse` (PR #227 + PR #231 smart-feed)
- D4 SIP 403 transient → handled inside D1's cascade (probe → IEX fallback, PR #231)
- D6 validation suite partial → `_auto_cascade_validation_failures` via `_VALIDATION_CASCADE_MAP` (PR #261)
- D7 monotonicity → `_MONOTONE_CASCADE_MAP` (PR #261)
- D8 macro per-indicator → `_MACRO_COMPLETENESS_CHECK` route (PR #261)
- D9 tier-completeness → `liquidity_tiers_completeness` route (PR #261)
- D10 ticker-classifications → `ticker_classifications_coverage` route (PR #261)
- D12 CSV substrate Railway-fragile → backend abstraction (PR #235, not a cascade — env-pluggable)

**Engine-lane:** none yet — Wave 3 dispatched 2026-05-22 (in flight at time of this entry).

## How to apply (extending the catalog)

When you find a new deterministic-recoverable failure shape:

1. **Read the spec catalog (`docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md`).** Add a row to the table. Status PROPOSED.
2. **Add a cascade function** in `scripts/ops.py` (data-lane) or `ops/engine_service.py` (engine-lane). Match the Wave 1 pattern: detect-shape, run-recovery, emit-event.
3. **Register the decision-point** in the appropriate map (or define a new map if the failure family is new).
4. **Wire the call** from `cmd_update` (data) or `engine_service` main loop (engine). After the stage failure, before the fallback `_self_heal_failed_stages` retry.
5. **Add a regression test** with the fail-on-main / pass-on-branch contract.
6. **Mark the row DONE** in the spec.
7. **There is no LLM persona to touch.** 2026-05-22 — the LLM-triage stack is REMOVED. New deterministic-recoverable shapes get a deterministic recovery; new unrecoverable shapes emit `INGESTION_AUTO_RECOVERY_FAILED` for operator review.

## Standing rules

- **Deterministic only.** Per operator directive 2026-05-22: "we aren't going to use the llm triage... take it out". The deterministic cascade catalog is the COMPLETE self-heal layer. No LLM backstop.
- **One-shot per failure.** Cascades retry ONCE. If the recovery itself fails, escalate via the terminal `_FAILED` event. Don't loop.
- **Whitelist-bounded.** Cascades only invoke whitelisted stages with whitelisted params. (The previous `_AUTONOMOUS_DATA_ACTIONS` whitelist from the deleted LLM stack is gone; the cascade-internal whitelist is what bounds the action surface now.)
- **Failure-shape exclusive.** Each cascade fires ONLY on its specific failure shape — a generic catch-all defeats the design.

## Standing memory cross-refs

- [[self-heal-autonomous-no-operator-task]] — the operator directive that birthed this architecture
- [[autonomous-lab-criteria-replaces-absolute-gate]] — sibling pattern at the engine-roster layer
- [[llm-triage-runs-local-on-max]] — DEPRECATED 2026-05-22; the LLM-triage stack was deleted entirely
- [[lab-heavy-probe-needs-chunking]] — the chunking pattern (PR #222, PR #236) is the same architectural idea applied to the LAB
- [[push-when-tangible-batch-prs]] — wave-bundling discipline (each wave = one tangible PR, not per-row PRs)

## Predecessor PR record (the architecture's emergence)

- PR #227 — orchestrator cascade trigger (`_auto_cascade_coverage_collapse` first instance)
- PR #231 — smart-feed cascade (SIP probe + IEX fallback)
- PR #233 — LLM autonomous action — REVERTED by deletion 2026-05-22
- PR #235 — CSV archive R3 substrate (env-pluggable backend)
- PR #236 — chunked force_refresh + lane-service daemon consolidation
- PR #239 — LLM data_recovery_v2 — REVERTED by deletion 2026-05-22
- PR #260 — 9-failure validation audit + recovery (4 healed, 5 surface real defects)
- PR #261 — Wave 1 deterministic recovery (D6-D10) + spec auto-update to APPROVED

Future cross-refs (Wave 2-5 PRs as they land):
- Wave 2 (D2/D3/D5/D13 — data robustness) — IN FLIGHT 2026-05-22 (subagent `af8925db0997cd683`)
- Wave 3 (E1/E2/E3/E9 — engine lane) — IN FLIGHT 2026-05-22 (subagent `a08af2f89fd6de11b`)
- Wave 4 (E4/E7/E10/E11 — engine behavioral) — pending operator authorization
- Wave 5 (sentinel test pinning the catalog) — pending operator authorization
