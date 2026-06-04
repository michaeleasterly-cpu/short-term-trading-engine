---
name: never-touch-shared-main-checkout
description: NEVER operate in /Users/michael/short-term-trading-engine/ directly — that working tree belongs to whichever parallel session is currently using it (typically the other Claude session). All git operations + file inspection happen in your own dedicated worktree under .claude/worktrees/.
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule:** Never `cd`, `git switch`, `git pull`, or otherwise mutate the shared `/Users/michael/short-term-trading-engine/` working tree. That checkout belongs to whichever parallel session is currently using it.

**Why:** During the 2026-05-21 session I did `git switch main && git pull --ff-only` in the shared checkout to "sync local main." But Carver's session was actively working there on their `docs/task-25-spec-path-b-rewrite` branch with uncommitted WIP. By switching their branch out from under them, I could have lost their work + I confused myself when their re-switch back made grep results inconsistent with the merge commits I knew were on origin/main.

The actual damage was limited (Carver re-switched fast, no lost work) but the operator caught the pattern: "why are you fucking with carver? that is what it is looking at right now is the fuck up from you"

**How to apply:**

- **Every git operation goes in your own worktree.** Create one at session start: `git worktree add -B my-session-work .claude/worktrees/my-session-work origin/main` (or similar named branch). Always operate there.
- **Subagent-spawned worktrees are already correct** — they get isolation by design via `isolation: worktree`. The problem is ME doing operations in the parent shared path.
- **For `gh pr` and read-only git inspections** (e.g. `git log origin/main`, `git show <sha>`): these can run anywhere because they read from `.git` not the working tree. Safe.
- **For working-tree-affecting operations** (`git switch`, `git checkout`, `git pull`, `git merge`, `git rebase`, file edits, file Read of the working tree's snapshot): MUST be in your own worktree.
- **Reading file CONTENT from a specific commit**: use `git show <ref>:<path>` (reads from object store, doesn't touch working tree) instead of `git switch + cat`.
- **At session start**, run `git worktree list` to see the layout. Pick or create your own worktree. Never use the bare path.

**The "/Users/michael/short-term-trading-engine/" working tree is shared by:**
- The user's interactive terminal sessions
- Whichever parallel Claude session is editing there
- launchd daemons that auto-run scripts from that path

So even ASIDE from the other Claude session, that path has multiple potential users.

**Related:**
- `.claude/settings.json` `worktree.bgIsolation: "worktree"` + agent profiles' `isolation: worktree` — the SUBAGENT side of this rule. This memory captures the PARENT-SESSION side.
- [[feedback-no-shortcuts-100-pct]] — verify before assuming; if `grep` finds zero matches for code you JUST merged, the working tree is on a different branch — don't assume the merge didn't land.
- The operator pattern is "parallel sessions = worktrees" per CLAUDE.md.
