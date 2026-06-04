---
name: i-do-that-too-not-operator-action
description: "STANDING RULE (operator 2026-05-23): for tasks I keep framing as 'operator action' or 'operator-side', if I have credentials/access to do them myself, I do them. DFCR filing, alembic migrations, live-DB runs, env-var checks — all mine. The operator pays for them; that does NOT mean they execute them."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23, perpetual):** *"DFCR is operator-side? you do that too, i pay for it"* + *"run the migration, you have the supabase credentials"* + *"remember that you do that shit"*.

For any task I'm tempted to defer as "operator-side" or "blocks on operator action", the test is simple: **do I have the credentials / access / tooling to execute it?** If yes, I execute. The operator paying for the resource (API key, Supabase compute, GitHub Actions minutes) does NOT mean they execute it. Their part is paying + directing scope; mine is execution.

## What this overrides

The instinct to write status reports like:
- "Operator action items: 1. Apply P2 schema to live DB ..."
- "DFCR ADD requires operator filing ..."
- "Pending operator: Supabase statement_timeout raise ..."

When in fact I have:
- `DATABASE_URL_IPV4` in `.env` → I run `alembic upgrade head`
- `OPEN_FIGI_API_KEY` in `.env` → I call OpenFIGI live
- `ANTHROPIC_API_KEY` in `.env` → I read/write the dev memstore
- `FMP_API_KEY` in `.env` → I call FMP profile live
- DFCR templating + ops.py pipeline → I file the DFCR

## What stays operator-action (genuinely)

- Domain decisions only the operator can make (which engine to ADD, which feed to RETIRE, which research direction to pursue)
- Scope authorization (start a new epic, change a multi-week direction)
- Manual UI actions in third-party systems I have no credentials for (e.g., if Supabase had a setting only reachable via their web UI with no CLI/API equivalent — rare; usually there's a CLI)
- Approvals on prepared diffs at structural-scope checkpoints (the DFCR `APPROVE? (y/n)` at the END of the system-prepared diff)

## How to apply

Before writing any "operator action item" line in a status report:

1. Grep `.env` for the credential the task needs. Got it? → I do it.
2. Check if there's a CLI / Python module / shell script I can invoke. Got it? → I do it.
3. Check if the operation is reversible or has a snapshot/rollback path. If yes + the above are true → I do it.
4. Only if ALL of those fail OR the operation is genuinely operator-only (domain decision, scope authorization, manual UI action) → I surface it.

## Related

- `[[ask-expert-then-execute]]` — sibling: ask EXPERT (not operator) for tech decisions
- `[[authorization-via-expert-keep-moving]]` — sibling: routine auth gates → expert, not operator
- `[[keep-building-dont-pause-for-breaks]]` — sibling: don't pause at "milestones" — and don't pause for "operator action" when I can act
- `[[cut-process-overhead-ship]]` — direct support
