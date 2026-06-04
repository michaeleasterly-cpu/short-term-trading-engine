---
name: keep-building-dont-pause-for-breaks
description: "STANDING RULE (operator 2026-05-23, perpetual): don't pause at 'natural break points' to report progress and await direction. Keep building. Operator directs the start; I keep going until the work is done or a real blocker is hit. Reporting happens when the deliverable is complete or genuinely stuck — not at every milestone."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23, perpetual):** *"build the openfigi adapter, dont ask next time... keep building"*.

When given a directive to build a thing, the work continues until:
- The deliverable is complete (all gates green, ready to ship)
- OR a genuine blocker is hit (operator action needed, design ambiguity that can't be resolved by expert, dependency that doesn't exist yet)
- NOT at "natural break points" (the temptation to stop after one major artifact and check in)

**Don't pause at milestones.** Don't ask "should I continue?" Don't ask "want me to do the next phase?" Don't report status at convenient stopping points hoping the operator will tell me to stop. Keep going.

## What this overrides

- The instinct to wrap up after a "good chunk of work" with a status summary + standing-by message
- The instinct to break multi-hour tasks into "one PR per session"
- The instinct to ask permission for each sub-deliverable inside a larger directive

## Composition with other standing rules

- `[[run-gates-locally-on-commit]]` — still applies; gate-then-commit-then-push per major-deliverable
- `[[push-when-tangible-batch-prs]]` — major-deliverable cadence still applies; "keep building" means I keep PRODUCING, not that I push more frequently
- `[[authorization-via-expert-keep-moving]]` — for routine authorization questions inside the work, dispatch expert + proceed; for genuine design ambiguity, dispatch expert + proceed
- `[[cut-process-overhead-ship]]` — direct support; this rule is the cadence-cut at the work-block level

## When DO I pause and report

1. **Deliverable complete** — all phases shipped, gates green, ready for operator review
2. **Genuine blocker** — operator action required (e.g., they need to approve a DFCR diff, set an env var, make a domain decision only they can make)
3. **Design ambiguity unresolvable by expert** — rare; usually the expert's verdict + my judgment can proceed
4. **Operator explicitly requests status** — they ask "where are you", I report; otherwise silent forward motion

## Related

- `[[cut-process-overhead-ship]]` — the parent cadence-cut rule
- `[[stop-over-asking-use-expert]]` — sibling for tech-choice questions
- `[[authorization-via-expert-keep-moving]]` — sibling for authorization gates
- `[[visible-progress-not-opaque-subagents]]` — visible PROGRESS, not visible STOPPING
