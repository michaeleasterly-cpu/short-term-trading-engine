---
name: local-llm-bridge-no-api
description: "Operator binding 2026-05-21 post-gate-pilot — ALL 4 LLM lanes route through the operator's local Claude Max Pro session via ops/llm_local_bridge.py. NO Anthropic API credit top-up. Same code that calls AsyncAnthropic must use the bridge instead."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

**Operator binding 2026-05-21 post-gate-pilot (PR #254 dossier):** ALL 4 LLM lanes that call `AsyncAnthropic.messages.create` via the API key path must route through the **operator's local Claude Max Pro session** via a new `ops/llm_local_bridge.py` module. **No Anthropic API credit top-up is going to happen** — the Max subscription already covers model access.

**The 4 affected lanes:**
1. `ops/llm_edge_finder_sdk.py` (Task #25 T9) — `make_sdk_llm_callable`
2. `ops/llm_lab_emitter.py` (SP-G PR #152)
3. `ops/llm_data_triage.py` (Epic E Phase 3, data lane)
4. `ops/engine_llm_triage.py` (Epic E Phase 3, engine lane)

**Why this is the right call:** API credits at autonomous-loop scale ($0.01-0.05/turn × 10 turns × 3 specs × N runs/day × 4 lanes) compounds to real money the operator already pays for via the Max subscription. Routing the lanes through the same session avoids double-billing.

**Why:** API path is structurally wrong-for-this-operator. Don't propose API-credit solutions, fallbacks, or hybrid postures. Bridge is the production posture.

**How to apply:** Any new ops/*.py that needs LLM completion uses `ops.llm_local_bridge.make_local_callable()` instead of the SDK. Same callable contract (`(system_prompt, user_prompt, transcript) -> dict`). Fallback if operator offline: lane co-task sleeps + emits `LAB_FINDER_BRIDGE_OFFLINE` event for §12 dashboard. The bridge SINGLE-SOURCES the contract so all 4 lanes share one implementation.

**Hosting posture:** edge finder + 3 LLM lanes = LOCAL-ONLY on operator's Mac. Railway can't reach the operator's Claude session, so those lanes can't migrate. Rest of the platform (data ops + engines + daemons) = Railway per the [[project_railway_archive_substrate_migration]] roadmap.

**Build status (2026-05-21):** module not yet written. Backlog at TODO.md L499 ("⚠ LOCAL-LLM-BRIDGE"). Order of work: edge finder bridge first (autonomous-loop-critical), then SP-G + data-triage + engine-triage in parallel (share `default_pr_runner`).

**Related decisions:**
- [[project_research_llm_edge_discovery]] — Path B autonomous loop reversed HARD CONSTRAINT (a) but assumed API path; this decision supersedes that on the deployment vehicle.
- [[project_railway_archive_substrate_migration]] — Railway hosting plan; LLM lanes stay off-Railway per this decision.
