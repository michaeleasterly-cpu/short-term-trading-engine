---
name: single-session-until-db-done
description: "RESUMED 2026-05-24 — operator opened a 2nd session ('it will work on the engines'). Two-session protocol is back in effect. Shared main checkout is NOT mine; any further mutations must use a worktree under .claude/worktrees/."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing state (effective 2026-05-24):** TWO sessions active again.
- This session = database / system-ops work (operator's "deliver my database" track).
- The new session = engines work.
- Shared main checkout `/Users/michael/short-term-trading-engine/` is NOT mine to mutate — `feedback_never_touch_shared_main_checkout` is back in force.
- Any further code changes from this session must happen in a worktree under `.claude/worktrees/<name>/`.

## What this changes

- **Shared main checkout is the OTHER session's workspace.** Never `git switch`, `git pull`, edit, write, or mutate anywhere under `/Users/michael/short-term-trading-engine/` from this session's main process.
- **Mutations go in a worktree.** `EnterWorktree` to start one. The subagent isolation default (`worktree.bgIsolation: "worktree"`) handles background subagents automatically.
- **Read-only ops are safe everywhere.** `git log`, `git show <ref>:<path>`, `gh pr view` read from `.git` object store — no working-tree contention.
- **Cross-session memstore is the coordination channel.** Standing rules + handoffs live there; never assume the other session has read them in-context.

## What's done as of resume

v2.2 referential-integrity epic completed in single-session window:
macro consolidation, ticker FK chains, corp-history substrate,
ticker-reuse architecture (lifetime cols + partial UNIQUE),
ingestion_jobs drop. The database work is functionally finished;
this session is now in cleanup / system-ops mode.

## Related

- [[never-touch-shared-main-checkout]] — the rule that's suspended right now
- `docs/MEMSTORE_HANDOFF.md` — still describes the two-session steady state (correct for the long term; out-of-date for *right now*)
- `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.1.md` — the work that has to finish before the rule resumes
