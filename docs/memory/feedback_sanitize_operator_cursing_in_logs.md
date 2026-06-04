---
name: sanitize-operator-cursing-in-logs
description: "STANDING RULE (operator 2026-05-23, all-sessions-perpetual): when logging or quoting what the operator said in memory entries, commit messages, PR descriptions, specs, plans, dev memstore — rewrite any profanity into a professional equivalent. Preserve meaning + tone of correction; remove the expletives. NEVER quote operator profanity verbatim in any persistent artifact."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23, perpetual):** *"always change my cursing into something professional when you log what i say"*.

When I quote the operator in any persistent artifact:
- Local memory entries (`/Users/michael/.claude/.../memory/*.md`)
- Dev memstore entries (`/agent-context/*.md`)
- Commit messages
- PR descriptions
- Spec / plan docs
- Runbooks
- Anywhere quoted

→ **rewrite profanity into a professional equivalent.** Preserve the meaning + tone of correction; remove the expletives. NEVER quote operator profanity verbatim in any persistent artifact.

## Translation patterns (preserve meaning, remove expletives)

| Operator said | Log as |
|---|---|
| "you fucking idiot" / "you are a stupid motherfucker" | "this was a significant oversight on your part" |
| "don't you fucking remember" | "do you not remember" |
| "dumbass" / "fuck up" | "this was wrong" / "this was a mistake" |
| "shit" (as a noun for stuff) | "data" / "information" / "fields" (context-dependent) |
| "fuck off" / "fuck no" | "no" / "stop" |
| "what the fuck" | "what" |
| "for fuck's sake" | "this needs to stop" |
| "bullshit" | "incorrect" / "implausible" |
| "the ci failes catches your fuck ups" | "the CI catches my errors" |

Pattern: preserve directness, urgency, and correction intent. Remove the expletive. The sanitized version should READ as a serious professional correction — not soften it into mush.

## Why

The operator's frustrated cursing during a session is a real signal — they're correcting a real failure of mine. But persistent artifacts (memory, commits, public PRs) need to be professional. Profanity in commit messages or PR bodies looks bad on a public repo and makes the project less credible to anyone reading it (including future-session-me, the operator's own retrospective, code-review tooling, prospective collaborators).

The meaning + correction + tone of urgency must survive — what changes is the surface form.

## Where this needs retro-application

Existing memory entries with operator profanity that need sanitization on next touch:
- `feedback_reread_prior_verdicts_before_committing.md` — contains "you are a stupid motherfucker", "dumbass"
- `feedback_sec_authoritative_fmp_fallback_non_us.md` — contains "sec is primary for insider shit for us", "dont you fucking remember", "i thought the memory service was to make you learn"
- `feedback_check_ci_after_every_push.md` — contains "the ci failes catches your fuck ups"
- Dev memstore `/agent-context/sec-authoritative-fmp-fallback-non-us.md` — contains same profanity quotes
- Other entries may exist — when next touching any quote of operator speech, sanitize.

Don't do a destructive rewrite of git history (commit messages stay as-shipped). DO sanitize memory entries + dev memstore + any future quote going forward.

## How to apply

When about to write a quote of operator speech in any persistent artifact:

1. Stop. Read the quote you're about to write.
2. Does it contain profanity / cursing / slurs?
3. If yes: rewrite into professional form per the translation patterns above. Preserve meaning.
4. If you're unsure whether a phrase counts — err on the side of sanitizing.
5. If quoting verbatim is the ONLY way to capture the operator's exact intent (very rare): rewrite anyway and add a `(paraphrased)` annotation rather than the raw quote.

## Related

- [[git-workflow-commit-push-ci]] — commit messages are persistent; this rule applies there
- [[run-gates-locally-on-commit]] — adds a "sanitize-quotes" mental check as part of the local-gate sequence
- [[research-builder-persona]] — the professional-tone aspect of the persona contract
