---
name: anthropic-memstores
description: "Two server-side Anthropic Memory Stores are in active use for this project — read this at session start so you don't re-discover them by grepping prior session transcripts. Includes the curl pattern + handoff path convention."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**At session start, BOTH of these memory stores exist and should be consulted before doing anything significant.** They are server-side (persist across session boundaries) and survived the lesson that file-based handoffs go stale faster than state changes.

## The two stores

| Memstore ID | Name | Purpose |
|---|---|---|
| `memstore_01P5DiJJgau4NhMMekaZDQEN` | `trading-engine-finder-context` | **Dev / cross-session coordination.** Standing rules, identity, two-session protocol, per-session ledgers, cross-session handoffs. Both Claude Code dev sessions (this one + Carver's) read+write here. |
| `memstore_01MzLun3AfRf2viPmDqJvsWi` | `finder-llm-edge-discovery` | **Autonomous LLM edge-finder (Task #25).** Prior emissions, outcomes, lessons learned for the finder agent. Dev sessions can READ to understand finder state; the finder itself writes via its Sessions API wiring. |

## Why both exist (don't conflate)

- The dev store is **operator-facing coordination** — what the human + parallel sessions need to know to not step on each other.
- The finder store is **agent-internal learning** — the finder writes its own emission/outcome ledgers; dev sessions mostly READ it for context.

Writing operator-facing handoffs into the finder store, or finder emissions into the dev store, breaks the separation. Keep them sorted.

## How to read at session start

Repo doc: `docs/MEMSTORE_HANDOFF.md` (curl one-liners + namespace conventions). Don't re-derive — start there.

Minimum session-start reads from the dev store:
- `/agent-context/identity.md`
- `/agent-context/hard-rules.md`
- `/agent-context/two-session-protocol.md`
- `/agent-context/working-state-pointers.md`
- The most recent `/handoffs/*.md` (cross-session coordination notes)
- The most recent `/sessions/*.md` (per-session ledgers)

List entries (the response key is `data`, not `memories`):

```bash
set -a && source .env && set +a
curl -s "https://api.anthropic.com/v1/memory_stores/memstore_01P5DiJJgau4NhMMekaZDQEN/memories?limit=100" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" | jq -r '.data[] | "\(.id)  \(.path)"' | sort
```

`limit` cap is 100. Fetch a specific entry by `mem_id` (the `id` field, not the path):

```bash
curl -s "https://api.anthropic.com/v1/memory_stores/memstore_01P5DiJJgau4NhMMekaZDQEN/memories/<mem_id>" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" | jq -r .content
```

## Write convention

- Latest-write-wins per path; 30-day version history server-side.
- Path regex: `^(/[^/ ]+)+$`. `.md` extension by convention.
- Namespace prefixes: `/agent-context/`, `/sessions/YYYY-MM-DD-<topic>.md`, `/handoffs/`, `/finder/`, `/probes/`, `/cross-agent/`.
- Cost: ~$0.001/call against `ANTHROPIC_API_KEY` credits (NOT Claude Max).

## Related

- `docs/MEMSTORE_HANDOFF.md` — in-repo entrypoint with the full curl set
- `feedback_complete_concern_map_first.md` — the dev-store coordination patterns reinforce concern-map discipline
- `feedback_never_touch_shared_main_checkout` — the protocol that motivated the dev store
