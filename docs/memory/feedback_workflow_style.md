---
name: workflow-style
description: "How the operator wants me to handle multi-step work and mid-flow pivots: don't push A/B/C options unprompted; track pending decisions; summarize the previous prompt's outcome before doing the new one"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 9e7d8205-de59-4346-8cfb-beb7d9a26640
---
Two related rules:

1. **Don't jump to conclusions / don't propose A/B/C options unless asked.** Surface trade-offs only when relevant; otherwise just execute the literal request. Reserve AskUserQuestion for genuine forks I cannot resolve myself.
2. **Keep an explicit running list of pending work across prompts.** The user often pivots mid-flow. When they send a new prompt, my response should:
   - Briefly state the results / outcome of the *previous* prompt (including anything left pending — e.g. an unmade decision).
   - Then do the new prompt.
   - The pending items remain on the list until explicitly resolved.

**Why:** The user explicitly corrected me when I offered a 3-option recommendation around DB hosting (Railway/Supabase) instead of just tracking the open decision and continuing with the next instruction.

**How to apply:** Treat this as load-bearing for *every* multi-step session. Use the TaskList as the persistent pending-work tracker. Open decisions = pending tasks. At the start of each turn, glance at the task list and surface anything still open in 1–2 lines before doing the new request.
