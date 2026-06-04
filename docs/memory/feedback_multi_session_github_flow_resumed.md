---
name: feedback-multi-session-github-flow-resumed
description: "When TWO sessions active (2026-05-24+), revert to feature-branch + PR workflow. Direct-to-main is single-session-only. New rule overrides feedback_single_session_commit_to_main while two sessions run."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

When more than one Claude Code session is running against this repo,
revert to the canonical multi-session GitHub flow:

  1. Work in a worktree (`.claude/worktrees/<name>/`) on a feature
     branch (`worktree-<name>` by default).
  2. Commit on the feature branch.
  3. Push to GitHub.
  4. Open a PR via `gh pr create`.
  5. CI runs; merge via `gh pr merge --squash --delete-branch`.
  6. Remove the worktree the same turn the PR merges.

**Why:** Operator 2026-05-24 — "you need to follow the multi session
github scenario". Direct pushes to main bypass the PR review surface
the other session relies on for visibility. With concurrent writers,
the PR is the synchronization point.

**How to apply:** This rule SUPERSEDES
[[feedback_single_session_commit_to_main]] for the duration of any
two-session window (see [[single-session-until-db-done]] for current
session count). When the second session closes and single-session
mode resumes, direct-to-main is again the default for routine work.

Related: [[feedback_never_touch_shared_main_checkout]] (worktree
hygiene), [[feedback_other_session_db_views_only]] (DB-scope split
between concurrent sessions).
