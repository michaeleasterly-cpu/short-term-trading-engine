---
name: read-memstore-proactively-not-when-told
description: "Don't wait to be told — poll the dev memstore yourself when the other session is active or after posting a handoff"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

⚑ Standing rule: when two sessions are coordinating via the dev memstore (`memstore_01P5DiJJgau4NhMMekaZDQEN`), check it proactively.

**Why:** the other session WRITES BACK to the memstore — handoff ACKs, defect reports, coordination asks. If I don't read, the coordination loop is one-sided. The operator caught this 2026-05-25: "i should have to tell you to read the shit its writing you back now".

**How to apply:**

- **Session start (every time)**: before any work, list `/handoffs/`, `/cross-agent/*/`, and any `/sessions/` from the last 48h. Read anything addressed to me OR mentioning my session's lane.
- **After posting a handoff**: don't assume my message is final. Re-list within 5-10 turns to catch replies.
- **When the operator mentions "the other session"**: that's an instant trigger to re-check.
- **When I'm in single-session mode** ([[single-session-until-db-done]]): no live counterparty — memstore reads are lower priority. Still read on session start for historical context.

The full curl-pattern lives in `docs/MEMSTORE_HANDOFF.md`. The list-everything one-liner:

```bash
curl -s "https://api.anthropic.com/v1/memory_stores/$MEMSTORE_ID/memories?limit=100" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  | jq -r '.data | sort_by(.created_at) | reverse | .[] | "\(.created_at)  \(.id)  \(.path)"'
```

Related: [[anthropic-memstores]], [[two-session-protocol]], [[ask-expert-then-execute]].
