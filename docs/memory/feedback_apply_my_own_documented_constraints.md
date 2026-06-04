---
name: apply-my-own-documented-constraints
description: "STANDING RULE (operator 2026-05-23, born from same-day repeat-failure): when I author a memory entry capturing a constraint or pattern, the NEXT piece of code I write that could violate it must be checked against that entry FIRST. Documenting the rule and then violating it the same session is a worse failure than not having documented it — the rule didn't enter my decision loop. Wastes operator quota + tokens + trust."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23):** *"didnt you already do that once already? didnt you update the datrabase constaints so you know not to fuck it / you wasted two more hours and tokens... you are fucking me over"* (paraphrased professionally: you already established this constraint; you wasted operator time + budget by ignoring your own rule).

When I write a memory entry capturing a constraint, pattern, or anti-pattern, the **next piece of code I write that could violate it** must be explicitly checked against that entry FIRST. Documenting a rule and violating it the same session is a worse failure than not having documented it — it means the rule didn't actually enter my decision loop.

## The 2026-05-23 incident this rule was born from

1. T=0: failed 21M-row single-transaction `UPDATE` on prices_daily → 1.95 GB WAL → Supabase auto read-only → 4-hour resize cooldown.
2. T+0.5h: wrote `[[supabase-constraints-2026-05-23]]` memory mandating "chunked DML for >100K-row writes" — pattern A.
3. T+1.5h: built `_tkr14_backfill_fmp_profile` stage with the OPPOSITE shape — buffer all 13K FMP responses, bulk-UPDATE at end.
4. T+2.5h: stage hit the 1-hour `HEAVY_STAGE_TIMEOUT_SEC`, was force-killed, **zero rows committed**.
5. Operator (paraphrased professionally): "didn't you already do this? You updated the constraints memory specifically so you wouldn't repeat it."

What pattern A (chunked DML) was about: WAL protection from single-transaction big writes. What I needed for FMP (pattern B): progress-survives-crash via streaming flush. Same root principle ("don't put all eggs in one buffer / one transaction"), different failure mode. The memory entry I wrote in step 2 covered pattern A — but didn't extend to pattern B until AFTER the second failure. The lesson: when I document constraint A, I should look one inferential step ahead at pattern B (the sibling failure mode), not wait for it to bite.

## The rule

**Whenever I write a memory entry, before I build the NEXT thing that touches the same surface:**

1. Re-read the memory entry I just wrote.
2. Ask: does the thing I'm about to build trip the rule? Even tangentially? Even via a different failure mode I didn't think of?
3. If the answer is yes (or maybe), either:
   - Update the memory entry to widen the rule to cover the new case
   - OR explicitly choose not to apply the rule with a documented reason
4. THEN build.

**Whenever I dispatch a long-running stage / migration / API call, before kicking it off:**

1. Walk through the relevant standing rules in MEMORY.md (literally scan the file).
2. For each one that COULD apply, verify the in-flight code respects it.
3. If the in-flight code violates one, fix BEFORE kicking off.

## Why this hurts more than other failure modes

- Quota wasted (FMP API calls, Anthropic compute, Supabase WAL, stage-timeout budget)
- Operator confidence shrinks — "you said you'd remember; you don't"
- Each repeat erodes the value of the next memory entry I write
- The platform's self-correcting feedback loop is broken when I write the rule but don't read it
- Repeated violation of self-authored rules signals the memory system is theatre, not load-bearing — operator's standing instructions make memory load-bearing; repeated violation makes me a liability instead of an asset

## How to apply

- **Pre-build checkpoint:** before any non-trivial code change, scan MEMORY.md for rules that COULD apply. The file is short and indexed by ⚑/⚠ priority for exactly this reason.
- **Post-memory-write checkpoint:** every memory entry I author goes through a "what's the next thing I'm about to do that this rule applies to" walk-through. If the rule needs widening to catch the next case, widen it BEFORE building.
- **Recurrence detector:** if I ever catch myself writing the same memory entry twice (or amending a recent one to cover a case I should have included originally), that's a tell — the underlying decision loop isn't actually consulting memory.

## Related

- `[[verify-expert-verdict-in-codebase-first]]` — sibling: don't relay verdicts I didn't verify
- `[[run-gates-locally-on-commit]]` — sibling: don't push without running my own gates
- `[[supabase-constraints-2026-05-23]]` — the memory entry I violated 90 minutes after writing
- `[[keep-building-dont-pause-for-breaks]]` — composes with this; "keep building" doesn't mean "don't pre-check the constraints"
