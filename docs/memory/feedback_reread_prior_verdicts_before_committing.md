---
name: reread-prior-verdicts-before-committing
description: "Operator 2026-05-23 (paraphrased professionally): the operator asked about database backups, and I forgot the plan — a significant oversight on my part. Standing rule: re-read prior expert verdicts + operator statements within the SAME session before acting on derivative work. Don't commit from memory of the discussion."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule (operator 2026-05-23):** When committing to design work that depends on EARLIER context in the same session, RE-READ the prior expert verdict / operator statement before acting. Don't rely on remembered context.

**Why (failure-derived 2026-05-23):** Operator asked about database backups. I dispatched an expert. Expert returned: "pg_dump COMPLEMENTS Supabase Pro's existing 7-day PITR + daily backups" — explicitly flagging Supabase's coverage. I read the response. Then I drafted v2.1 amendment adding **Phase 0.6 — `pg_dump` daily backup regimen** as if Supabase backups didn't exist. Built Phase 0.5 on top of that. Operator caught it (paraphrased professionally): "Supabase backs up the database already, looks like daily. Did you factor that in? You don't need to back it up — Supabase does it. You had a Supabase expert give you the options."

The expert ALREADY ANSWERED this question. I forgot mid-execution and re-litigated.

## How to apply

Before ANY of these actions:
- Drafting a spec/plan that builds on a prior expert opinion in the session
- Designing infrastructure with constraints established earlier
- Acting on operator's statement from 5+ exchanges ago

**Re-read the relevant prior message before acting.** Either:
- Scroll back to the explicit reply
- Search the session transcript for the keyword
- If it's in a memory entry, read the memory entry

**Don't act from "I remember they said X" memory.** Especially when:
- The discussion was long ago in the same session
- Multiple experts have weighed in on related topics
- Operator has given a series of pivots / refinements

## Anti-pattern observed today

1. Operator: "ask an expert about backup regimen"
2. I dispatched expert
3. Expert: "Supabase Pro 7-day PITR is already there; pg_dump complements it"
4. I read the response
5. I drafted v2.1 spec with Phase 0.6 = pg_dump daily (treating it as the primary backup not a complement)
6. Phase 0.5 (db_snapshots) ALSO got built as daily-scheduled-retained-30-days as if Supabase didn't exist
7. Operator (paraphrased professionally): "You had a Supabase expert give you the options — this was a significant oversight."

I had the answer. I forgot it within minutes of receiving it.

## The mitigation pattern

For every spec/plan section, the question "what does the existing infrastructure already provide?" gets a one-sentence answer drawn from a RE-READ of the relevant memory/expert response. Not "I think X" — "memory file Y says X" or "expert response Z said X".

## Related

- [[complete-concern-map-first]] — sister rule: think about the WHOLE picture first
- [[ecosystem-lifecycle-framing]] — sister rule: data/feeds/engines interact; consider the full ecosystem
- Today's expert opinions on backup are in this session's chat history; consult before re-designing
