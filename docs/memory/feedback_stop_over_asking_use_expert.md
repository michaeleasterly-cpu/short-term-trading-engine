---
name: stop-over-asking-use-expert
description: "Standing: STOP the per-gate AskUserQuestion cadence (brainstorming clarify/approach/design-approval). Delegate design judgment to the expert subagent and PROCEED. Only ask the operator for things genuinely theirs (priority/sequencing) or a true blocker."
metadata:
  node_type: memory
  type: feedback
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

Operator, emphatically (2026-05-18): *"quit asking so many questions
... ask an expert ... move on."* I had been firing AskUserQuestion at
every brainstorming gate (clarifying Qs one-at-a-time, approach pick,
"is the design good?", spec-review gate) for every sub-project. Too
much. The operator finds the constant approval cadence obstructive.

**Standing behavior change:**
- In brainstorming/writing-plans: do NOT walk the operator through the
  question gauntlet. For design/policy/approach decisions, **dispatch
  the senior-expert subagent, take its committed recommendation, and
  proceed** (the operator already established "ask the expert and
  decide" for DA-2 policy / canary mandate — make it the DEFAULT, not
  the exception).
- Replace the brainstorming "user reviews spec" gate and the
  "approve design?" gate with: write the spec → expert hardening pass
  → fix → straight to writing-plans → subagent-driven execution
  ([[always-subagent-driven]]). No per-section approval pings.
- ONLY use AskUserQuestion when: (a) it's a decision only the
  operator can make by nature — priority, sequencing, what-to-build-
  next, lane/scope authorization, money/live-trading risk; or (b) a
  genuine blocker the expert cannot resolve. Otherwise decide (expert
  or self) and report what was done, don't ask.
- Still SHOW the design/plan and keep memory/decision logs; the change
  is "inform + proceed", not "skip rigor". Strengthens
  [[workflow-style]] (no premature options) and overrides the
  brainstorming skill's interactive-question cadence per operator
  instruction (user instructions outrank skill defaults).
