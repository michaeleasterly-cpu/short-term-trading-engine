---
name: feedback-visible-progress-not-opaque-subagents
description: "Operator perceives long opaque subagent dispatches as 'stuck' — keep work visible: crisp status around every dispatch, do small fixes directly in-thread"
metadata:
  node_type: memory
  type: feedback
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

Operator signal (2026-05-19, repeated): "is it stuck" / "you got
stuck" — fired during 5–10 min subagent dispatches (SP-A T11
finish-branch ~10min, the SP-A2 expert-harden ~8min) that emit ZERO
intermediate output. I was NOT stuck; the dispatches are a black box
from the operator's side, which reads as hung.

**Why:** a long-running Agent call shows nothing until it returns. In a
long autonomous chain (brainstorm→spec→harden→plan→subagent-exec→
reviews) this is many consecutive opaque multi-minute waits — the
operator can't tell progress from a hang and rightly loses confidence.

**How to apply (standing):**
- Put a crisp one-line status BEFORE every dispatch (what + why + that
  it's a long-runner) and a concrete result line AFTER — never go
  silent across a dispatch.
- Match mechanism to size: small, mechanical, well-specified fixes
  (e.g. mirror a known canonical remedy across 3 call sites) → do them
  DIRECTLY in-thread with visible step-by-step tool calls, not a long
  subagent. Reserve subagents for genuinely large/independent work and
  for fresh-context reviews. [[feedback_always_subagent_driven]] is
  calibrated to epic PLAN execution, not every small standalone fix.
- Prefer shorter, more frequent checkpoints over one giant agent;
  tighten subagent scope so it returns faster.
- Pairs with [[feedback_workflow_style]] (pivot reporting, no opaque
  stretches) and [[feedback_stream_long_running_output]] (stream
  progress; a crash/interrupt mid-run must leave visible state).
