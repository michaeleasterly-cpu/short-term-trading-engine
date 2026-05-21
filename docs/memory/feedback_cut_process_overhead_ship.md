---
name: cut-process-overhead-ship
description: "Operator 2026-05-20, emphatic: too much scanning/review vs coding (~10% coding); collapse the per-task review spiral, implement directly, trust good tests, ship — overrides the heavy uniform DEV_PIPELINE cadence"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9c826d9e-2d98-48e5-a010-5bcee9667a4d
---

Operator (2026-05-20, frustrated, valid): *"you do more scanning than
coding ... 10% coding now"*, *"the tests are supposed to test so you
know it works ... your tests must be shit"*, *"you keep finding bugs
and it's your code that took 5 steps before you wrote a character of
code"*, *"just spinning and not getting shit done"*, *"T5 blocked,
stopped, unblocked, t fucking 5"*.

**Why:** the implementer→spec-review→code-review→fix→re-review→fix→
re-review spiral (5–7 subagent hops/task) + brainstorm→spec-PR→plan-PR
ceremony BEFORE coding produced defect-laden plan/test artifacts whose
bugs the expensive review loop then "caught" — process overhead, not
rigor. Ratio of shipped engine value to ceremony was terrible.

**How to apply (standing, overrides the uniform heavy
[[always-subagent-driven]]/DEV_PIPELINE cadence per instruction
priority — user > skills):**
1. Implement directly. Brainstorm→spec→plan gated-PR chain ONLY for a
   genuinely net-new epic, NOT per task/sub-task.
2. ONE consolidated review per task max — not split spec+code-quality
   THEN re-review every fix. Reviewer Minors: fold silently or skip,
   never round-trip.
3. Tests are the validation. Write them genuinely good once; if green,
   that is the proof — don't stack 3 review passes on top. Stop
   polishing test comments/provenance beyond proving behavior.
4. Reserve deep multi-pass review for truly safety-critical live-money
   surface only (real money path), not test scaffolding/docs/refactors.
5. Bias to DONE + merged over perfect. Don't spin.
Still real: don't ship broken; CI gate stays; live-money safety stays.
But default to lean. Relates to [[feedback_stop_over_asking_use_expert]],
[[feedback_visible_progress_not_opaque_subagents]],
[[feedback_no_shortcuts_100_pct]] (verify ≠ ceremony).
