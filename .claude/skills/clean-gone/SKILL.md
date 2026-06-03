---
name: clean-gone
description: "Slash-only wrapper that cleans up local branches with [gone] remote (deleted on the remote but still on disk), including the worktrees attached to them. Safe-by-default: refuses to touch worktrees with uncommitted work. Vendored 2026-06-04 from anthropics/claude-code plugins/commit-commands."
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git branch:*), Bash(git worktree:*), Bash(git rev-parse:*)
---

# /clean-gone

Authoritative external: <https://code.claude.com/docs/en/skills>.
Vendored 2026-06-04 from `anthropics/claude-code` `plugins/commit-commands/commands/clean_gone.md`, hardened for STE's worktree-heavy workflow per `docs/audits/2026-06-03-vendor-vs-handrolled.md` §7 + operator decision §9 #7.

## What this skill does

Cleans up local branches whose remote has been deleted (the `[gone]` markers in `git branch -v`), including the worktrees attached to those branches. After a squash-merge with `--delete-branch`, the local branch + its worktree become orphaned — this skill reaps them.

## Procedure

Run in this order:

1. **List branches with their tracking state.**
   ```bash
   git branch -v
   ```
   Branches with `[gone]` in the tracking column have no remote; those are the candidates.

2. **List active worktrees so we know which branches have worktrees attached.**
   ```bash
   git worktree list
   ```

3. **For each `[gone]` branch:**
   - **Skip the main checkout's branch** — even if its remote disappeared, we don't delete the branch the operator's primary checkout is on. (`git rev-parse --show-toplevel` returns the worktree path; the branch on that path stays.)
   - **Skip worktrees with uncommitted work** — if `git status` in the worktree reports uncommitted changes, surface the worktree path + the dirty files and skip. Operator must commit / stash / discard first.
   - **Skip worktrees marked `locked`** — `git worktree list` shows `locked` next to manually-locked worktrees. Skip them.
   - **Remove the worktree** if one is attached: `git worktree remove -f <path>` (the `-f` covers the case where the worktree dir has untracked files the operator already accepted).
   - **Delete the branch:** `git branch -D <name>`.

4. **Report the result.** Show: (a) which branches were deleted, (b) which worktrees were removed, (c) which were skipped + why.

## Safety invariants (do NOT bypass)

- **NEVER delete the branch the main checkout (`/Users/michael/short-term-trading-engine`) is sitting on.** Even if its remote is gone. Operator's standing rule: don't touch the shared main checkout.
- **NEVER remove a worktree with uncommitted work**, even with `-f -f`. Surface the dirty paths and let the operator decide.
- **NEVER remove a `locked` worktree** — the lock means someone (or some session) explicitly marked it for preservation.
- **NEVER use `git branch -d` without `-D`** here — `-d` refuses to delete branches that aren't fully merged into upstream, but for `[gone]` branches there is no upstream. `-D` is the right call.
- **Don't delete `main`** — even if `git branch -v` somehow showed `main [gone]`.

## Why this skill matters for STE

STE's heavy-lane workflow uses worktrees per PR (`worktree-<name>`). After squash-merge with `--delete-branch`, the local branch + worktree dir both go orphan. Letting them accumulate:
- Wastes disk
- Confuses `EnterWorktree` with a path-clashes-with-an-existing-worktree error
- Pollutes `git worktree list`'s output, making it hard to see active work

The standing operator rule (CLAUDE.md §"Parallel sessions = worktrees"): *"Cleanup is mandatory, not optional. … On session close, Claude prompts keep/remove if the worktree has changes — don't accumulate stale worktrees."* This skill is the batched-cleanup version.

## What this skill does NOT do

- Does NOT push, commit, or open PRs.
- Does NOT touch `.claude/worktrees/<name>` directories that don't have a registered worktree — those are operator-managed loose dirs.
- Does NOT run any destructive command on the main checkout (`git rev-parse --show-toplevel` from this skill returns the path Claude is invoked from; the main checkout is excluded by the path comparison).

## Adjacent SoT

- `.claude/skills/commit-push-pr/SKILL.md` — the upstream flow that creates the branches this skill cleans up.
- CLAUDE.md §"Parallel sessions = worktrees" — the standing cleanup rule.
- `docs/audits/2026-06-03-claude-code-workflow-controls.md` §13 #4 — branch-base discipline (relevant when worktrees are created with the wrong base).
