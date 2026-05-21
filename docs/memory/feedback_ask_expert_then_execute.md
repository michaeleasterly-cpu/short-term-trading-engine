---
name: ask-expert-then-execute
description: "Operator 2026-05-20 standing rule: on every technical decision point, ask an expert subagent (claude-code-guide for tooling/SDK, general-purpose for codebase research) FIRST, then proceed with the recommendation. Don't ask the operator the technical question — ask the expert. This is the next-step contract every time, not just when stuck."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

Operator 2026-05-20 (during the vector_composite Lab probe retry-loop, after the 5th probe failed on Postgres statement_timeout and I asked the operator to choose between 4 options): **"ask expert, go with recommendation, do this each time"**.

The standing contract:

- **Rule:** every technical decision point that has more than one defensible answer goes through an expert-subagent first. NOT the operator.
- **Default expert:** `claude-code-guide` for Claude Code / SDK / Anthropic API / tooling-specific questions; `general-purpose` (or `Plan`) for codebase research and architecture decisions.
- **Then execute:** take the expert's recommendation and proceed without asking the operator for sign-off. The operator delegated approval authority for technical choices.
- **What still goes to operator:** scope changes, priorities, sequencing, true blockers, and anything where the consequences span beyond the current task.

**Why:** every "what should I do" question to the operator costs them attention. The expert subagent IS the authority for technical correctness. The operator is the authority for what to spend tokens on, not how to spend them.

**How to apply:** when I would have written "Options forward (operator decides): 1. ... 2. ... 3. ..." or "What's the call?", instead spawn an expert subagent with the question, get the recommendation, execute. Report the expert's reasoning + my action in the response — but don't pause for sign-off.

Pairs with [[feedback_stop_over_asking_use_expert]] (the same idea, expanded into a standing every-time rule).
