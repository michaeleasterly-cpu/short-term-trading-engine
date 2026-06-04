---
name: canary-is-end-to-end-heartbeat
description: "Canary engine is the non-graduating end-to-end test heartbeat — never receives finder hypotheses, never gets engine surface enrichment, never calls write_credibility_score; excluded from the eligible target_engine list."
metadata: 
  node_type: memory
  type: project
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

**Canary's purpose (operator clarification 2026-05-22):** canary is intentionally a non-graduating end-to-end smoke check. It's NOT a real engine for hosting trading hypotheses.

**Why:** spec §4b establishes canary as the documented heartbeat engine. Concretely:
- `tpcore/engine_profile.py::_PROFILE['canary'].graduating=False`
- Canary's backtest NEVER calls `write_credibility_score` (engine-build rule explicitly carves canary out as the only exception)
- Canary trades a known, predictable signal at low size purely to prove the platform end-to-end pipeline still works (engine_service → broker → trade_monitor → AAR → dashboard)

**How to apply:**

1. **Engine-surface-enrichment epic excludes canary.** Don't add LAB_TARGET knobs for finder hypotheses. The 5-engine roster for enrichment is: reversion, vector, momentum, sentinel, catalyst (canary skipped).

2. **The autonomous finder (Task #25) should NOT emit ProposedSpecs with `target_engine='canary'`.** The v2.0 gate pilot produced a `canary_range_reversion_5d_earnings_conditional` candidate — that was a misemission. Next persona iteration should explicitly list canary as ineligible (separate from the snapshot.roster). Persona §3 or §7 should say: "canary is a platform heartbeat — never propose hypotheses for it; pick from {reversion, vector, momentum, sentinel, catalyst}."

3. **If the snapshot's `roster_target` list currently includes canary**, that's a snapshot-assembler bug — should be filtered out at assembly time.

**Related memory:** [[project_finder_first_edge_signal]], [[project_master_remaining_program]].
