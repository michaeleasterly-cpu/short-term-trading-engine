---
name: push-when-tangible-batch-prs
description: "Push when there's something tangible — not after every commit. Batch related work into single PRs. Reserve subagent dispatch for genuinely independent / large work; small things go in-thread. Per-task-PR pattern burned the operator's GitHub Actions quota and forced them to make the repo public 2026-05-21."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule:** Push when there's something tangible. Don't push after every commit. Batch related work into single PRs.

**Why:** During the 2026-05-21 session I dispatched ~12 subagents in sequence, each opening its own PR with 4 CI checks per push (pytest + ruff + check_imports / lab-isolation-db / fail-closed agent-PR label guard / [LLM-triage fence skips]). Combined with rebase-on-red re-pushes when other PRs broke main, the cumulative CI consumption exhausted the operator's GitHub Actions quota. **The operator had to make the repo public** to keep CI running. That has IP / strategy disclosure implications for a private quant trading platform.

**How to apply:**

- **Default to in-thread execution** for small changes. The bar for dispatching a subagent: scope ≥ ~150 LOC, OR independent file surfaces, OR fresh-context-review needed. Below that, do it in-thread.
- **One PR per tangible milestone, not per task.** If three related fixes can ship together (e.g. cascade fix + cascade-test + docs update), bundle them in one branch and push once. The smart-feed cascade + autonomous-triage + CSV-archive-backend were three separate subagent dispatches — they could have been one if related work landed together.
- **No reflexive rebase + re-run on CI red.** Fix forward in the same push window when possible. Only rebase if a rebase is genuinely required (main moved through your file surface).
- **Verify locally before pushing.** Run the four heavy-lane gates (`pytest -p no:xdist`, `pytest -p no:randomly`, `ruff`, `check_imports`) BEFORE the first push. The "push and watch CI tell me what's red" pattern multiplies cycles.
- **Subagent dispatches batch their internal work too.** When dispatching, instruct the subagent to push ONCE per tangible milestone, not after every commit. Same standing rule applies to delegated work.

**Operator's verbatim phrasing (2026-05-21):**
- "you dont have to push every time, you can push after you get something tangible"
- "you used up all my shit in github and i had to make the repo public"

**Related:**
- [[feedback-cut-process-overhead-ship]] — operator's prior signal that the dispatch cadence was excessive (per-gate review spiral). This is the CI side of the same pattern.
- [[feedback-visible-progress-not-opaque-subagents]] — small mechanical fixes in-thread, reserve subagents for large/independent work. Same principle: subagents are not free.
- [[feedback-always-subagent-driven]] — earlier standing rule that subagent-driven IS the execution mechanism. NOT contradicted — subagents stay the mechanism for *substantial* work; small fixes still go in-thread. Reconciliation: subagent for the substance, in-thread for the polish.
