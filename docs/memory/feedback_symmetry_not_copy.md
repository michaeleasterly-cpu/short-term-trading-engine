---
name: symmetry-not-copy
description: "Cross-lane (engine↔data) work applies symmetry of pattern/contract, NOT a code copy; within-lane structural mirroring is fine"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

When building a data-lane counterpart of an engine-lane mechanism (e.g. the
Data Supervisor as the counterpart of `ops/engine_supervisor.py` / DA-1; the
"Escalation & Hardening Ladder" as the data analog of the engine Deterministic
Agents epic), **apply symmetry of approach — do NOT copy the engine session's
work**.

Still applies in the single-session world (engine session permanently ended 2026-05-19, [[cross-session-coordination]]) — engine vs data is now a SUBSYSTEM distinction, not a lane/session distinction. Symmetry-of-approach when building the data-side analog of an engine-side mechanism (or vice-versa) still binds; nothing about consolidating to one session relaxes the don't-copy rule.

**Why:** operator stated this explicitly (2026-05-17): *"you are not to copy
the engines work but to apply symmetry"* and earlier *"use the same approach
... i like symmetry"*. The lanes have genuinely different realities; a literal
port force-fits the wrong abstractions.

**How to apply:**
- Reuse the *pattern / contract shape / decision framework*: event-sourced
  hold/clear (no new table), bounded detect→remediate→verify→escalate→
  auto-clear, Approach-A (module inside the existing daemon, no new daemon),
  `schema:1` inter-lane events, conservative safe-by-construction auto-clear
  (no operator ack), crash-isolated.
- Design **data-native** components, not engine clones: per-**source/feed**
  (not per-engine); stand-down via the **capital-gate / `DATA_OPERATIONS_
  COMPLETE` emit gate** (not `should_fire`); detectors are the validation
  suite / cross-table audit / `tpcore.auditheal` / the contract-population
  sentinel (not scheduler liveness/`crashed_startup`).
- Within-lane structural mirroring IS fine and good: `tpcore/auditheal`
  mirroring `tpcore/selfheal` 1:1 was correct (same lane, real reuse, drift
  symmetry). The "not a copy" rule is specifically **cross-lane**.
- Avoid "1:1 / structural twin / clone of the engine X" framing for
  cross-lane work; say "symmetric to" and justify each data-native divergence.

Related: [[project_three_service_architecture]] (data/engine/aar symmetry is
the operator's canonical topology).
