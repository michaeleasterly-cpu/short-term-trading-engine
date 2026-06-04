---
name: anthropic-529-self-heal
description: "Anthropic API 529 \"Overloaded\" platform incidents — known transient error; retry-with-long-backoff (60/120/300s) at SDK call sites; check status.claude.com before assuming code defect"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

Anthropic API returns HTTP 529 ("Overloaded") during platform-wide capacity incidents. These are SERVER-SIDE, transient, and typically last minutes-to-hours per status.claude.com.

**Why:** 2026-05-22 — two consecutive subagent dispatches failed with `API Error: 529 Overloaded` during pilot work; status.claude.com confirmed an active "Elevated error rate on multiple models" incident at 04:16 UTC. Operator framed as: "you have a known error to self-heal from."

**How to apply:**

1. **Before assuming code defect**, fetch status.claude.com and look for active incidents. 529 ≠ bug in our code.
2. **For SDK call sites** (`ops/llm_edge_finder_sdk.py`, `ops/llm_data_triage.py`, `ops/llm_data_recovery.py`, `ops/llm_lab_emitter.py`): retry-on-5xx with LONG backoff (15/30/60s minimum; up to 300s cap). Short backoff (the prior 2s base / 30s cap → 14s total budget) burns the retry quota faster than the platform recovers.
3. **In `ops/llm_edge_finder_sdk.py`** specifically: catch `(InternalServerError, APIStatusError)` with backoff `(60, 120, 300)` seconds — see the inline retry loop. The other 3 LLM SDK sites use `with_retry(retry_on=(RateLimitError, APIError))` with `backoff_base_sec=15.0, backoff_cap_sec=300.0`.
4. **Lab probes (`python -m ops.lab`) are NOT affected** — they're deterministic Python code with no API calls. If 529 blocks subagent dispatches, run the deterministic work inline.
5. **Subagents that 529 should be re-dispatched** with the same brief; they retry with fresh context.

**Bright line:** 529 is not a coding defect. Don't redesign the system in response to it. Add backoff + propagate.
