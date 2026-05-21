---
name: always-subagent-driven
description: "Standing operator preference: ALWAYS execute implementation plans via superpowers:subagent-driven-development — never ask 'which execution approach', never inline-execute. CADENCE cut 2026-05-20 by [[cut-process-overhead-ship]] (one consolidated review/task, no per-fix re-review)."
metadata:
  node_type: memory
  type: feedback
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

**⚠ CADENCE OVERRIDE 2026-05-20 — see [[cut-process-overhead-ship]]:**
the *execution mechanism* (subagent-driven) still stands; the *review
cadence* is now ONE consolidated review per task max (no split
spec+code-quality then re-review-per-fix). Tests + CI are the proof,
not ceremony. Reserve deep multi-pass review for safety-critical
live-money surface only. Below = the historical full cadence; apply the
lean version unless a task is genuinely live-money safety-critical.

Operator directive (2026-05-17): *"always subagent drive."*

**Why:** the writing-plans skill ends by offering "1. Subagent-Driven
vs 2. Inline Execution". The operator has chosen Subagent-Driven every
time (Sub-project C, DA-1, DA-2) and now made it standing policy. It
is the flow that delivered C/DA-1 cleanly and caught the DA-1-T7
vacuous-test regression via the two-stage review.

**How to apply:** after writing-plans saves a plan, do NOT ask the
execution-choice question — go straight to
`superpowers:subagent-driven-development`: fresh implementer subagent
per task (TDD), then ONE consolidated review (per the 2026-05-20
override), then finishing-a-development-branch (push → PR → merge when
CI green → clean worktree). Still surface the plan path so the
operator can object, but default to executing.

**Pipeline shape (historical full form — read with the 2026-05-20
cadence override applied):** brainstorm → expert-harden → spec (own
gated docs PR) → operator spec-read gate → writing-plans (own gated
docs PR) → subagent-driven exec → consolidated review (was split spec
+ code-quality per [[split-review-dispatches]] — kept ONLY for
safety-critical live-money work post-2026-05-20) → reviewer findings
folded by the implementer → gated PR → CI verified via `gh pr checks`,
never `gh run watch`'s exit code → whole-suite single-process +
order-flip gate ([[ops-package-shadow-full-suite-gate]]) → squash-merge.
Pairs with [[stop-over-asking-use-expert]], [[workflow-style]],
[[cut-process-overhead-ship]].
