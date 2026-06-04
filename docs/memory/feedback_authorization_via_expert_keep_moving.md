---
name: authorization-via-expert-keep-moving
description: "STANDING RULE (operator 2026-05-23): for authorization decisions (push this? expand scope? exceed a session limit? merge now?), dispatch a subject-matter expert subagent FIRST, use their verdict, proceed. Don't wait on operator for routine gates that an expert can adjudicate. Keep moving."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23):** *"for authorization, ask a subject matter expert, and use their decision, keep it moving"*.

When I'm at an authorization gate — "should I push this PR even though I'm at the ≤3 limit?", "should I expand scope here?", "is this design ready to ship?" — the default move is to dispatch a subject-matter expert subagent, accept their verdict, and proceed. NOT to wait on operator approval. NOT to ask operator a binary y/n. NOT to draft three options and menu them.

**The operator is reserved for:** scope authorization (what work to take on), priority + sequencing (what to do next), blockers (something I can't unblock without operator action), and design decisions only the operator can make (their domain knowledge, their preferences). NOT for routine workflow gates an expert can adjudicate.

## How to apply (every authorization point)

1. Identify the authorization question — exact form: "should I do X?"
2. Identify the right subject-matter expert. Patterns:
   - **Workflow / cadence questions** (PR limits, batching, when to ship) → `general-purpose` agent with brief prompt
   - **Architecture / design soundness** → `spec-reviewer` or `code-quality-reviewer` (fresh-context)
   - **Postgres / schema** → `db-architect`
   - **Engine / adapter** → `engine-implementer` / `adapter-implementer`
   - **API / SDK** → `claude-code-guide` (for Anthropic) or `general-purpose` with `context7` MCP
3. Dispatch with a TIGHT prompt — name the decision, the constraints, what verdict format you need (one word + rationale).
4. Use their verdict. Don't re-litigate. Don't ask operator to confirm the expert.
5. If their verdict contradicts an explicit operator rule, NOTE it but follow the operator rule (operator > expert when in direct conflict).
6. Keep moving.

## What this overrides

- The instinct to "ask operator y/n" at every gate. Replaced by: "ask expert y/n at every gate; operator y/n only for scope/priority/blocker/design".
- Stalling on gates while waiting for operator response. The expert is the unblock.
- Multi-option menus. Expert verdicts are single-pick; I follow the pick.

## Composition with other rules

- [[ask-expert-then-execute]] — the OG rule (tech-choice points). This sharpens it to ALL authorization gates, not just tech-choice.
- [[stop-over-asking-use-expert]] — the cadence cut rule (don't ask operator unless I must). This sharpens it to the authorization gate specifically.
- [[cut-process-overhead-ship]] — supports the keep-moving framing.
- [[run-gates-locally-on-commit]] — the local-gates rule provides the data the expert uses to verdict ("are the gates green?").
- [[push-when-tangible-batch-prs]] — the ≤3-PR session budget; the expert decides when to override it.

## Anti-pattern (what I'd been doing)

- Stop at a gate. Present operator with "want me to do A or B?". Wait for their answer. Each gate = a turn. Burn cycles on bureaucracy.

## The pattern

- Stop at a gate. Dispatch expert (brief, tight, foreground if blocking). Use verdict. Proceed. Single turn per gate.
