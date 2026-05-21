---
name: momentum-aar-plug-finding
description: "TRACKED (not yet fixed): momentum's AAR + lifecycle plugs are defined but NOT instantiated in momentum/scheduler.py — possible CLAUDE.md AAR-compliance gap; surfaced by Lean P3a vulture spot-read 2026-05-19, pre-existing since the momentum build"
metadata:
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Finding (Lean P3a vulture allowlist spot-read, 2026-05-19; spec/intent
re-review independently confirmed):** `MomentumAARLogging` /
`MomentumLifecycleAnalysis` / `write_rebalance_close`
(`momentum/plugs/aar_logging.py:32/52`,
`momentum/plugs/lifecycle_analysis.py:32`) are **never instantiated** —
they appear only in a `momentum/scheduler.py:39` docstring reference.
`momentum/scheduler.py` reconciles AARs via the **trade_monitor path**
(scheduler.py:444), not via these plugs. **Pre-existing since the
original momentum build (sole commit `a340d64`)** — NOT introduced by
Lean P3a; the plug docstring self-documents "exists for parity".

**Status:** allowlisted in `vulture_allowlist.py` as
intentional-parity-scaffold (with an inline note so a future reader
does NOT delete it as plain dead code) AND **flagged here for separate
assessment — deliberately NOT fixed opportunistically** (out of P3a
scope; engine-lane-origin code, now owned by this single session per
[[cross-session-coordination]]).

**Why it matters / how to apply:** CLAUDE.md "Engine-build compliance
shortlist" mandates *every AAR plug uses `tpcore.aar.classify_exit_reason`*
and the engine_readiness §10 compliance set. Momentum may satisfy the
*intent* (AARs DO get reconciled, via trade_monitor) while not
satisfying the *letter* (the declared AAR/lifecycle plugs are dead
scaffolding). Open question for a future dedicated assessment: is
momentum's trade_monitor-path AAR reconciliation compliant-by-intent
(then the plugs should be deleted or the docstring corrected), or is
this a real gap (then the plugs must be wired into the scheduler like
the per-trade engines)? Requires reading `momentum/scheduler.py`,
the two plugs, `tpcore.aar`, and how reversion/vector wire their AAR
plugs for parity. Pairs with [[feedback_tpcore_reuse]] /
[[lab-front-half-epic]] (momentum is a Lab target engine). Do NOT
auto-start; surface when momentum/AAR-compliance or the Lab front-half
is prioritized.
