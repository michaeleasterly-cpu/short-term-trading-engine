---
name: git-workflow-commit-push-ci
description: "Git discipline operator 2026-05-23: commit often, push after a batch is ready, check CI after every push. The pattern I keep failing: accumulating uncommitted changes across multiple-hour sessions, then surfacing them all at once (or losing them entirely)."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule (operator 2026-05-23, reinforced):** *"commit often and after the major deliverable then push"* + *"all these pushes costed me to have to make the repo public"*. Operator burned GitHub Actions quota → repo forced public 2026-05-21 incident.

**A "batch" = a MAJOR DELIVERABLE, not a single-file change.** I shipped 12 PRs today (#305-316) when 4 would have done the same work. That's 8 CI-runs of waste. Examples of what should have been one PR:

- v1 design pair (PR #305 plan + PR #307 spec + #308 docs reorg + #314 db-architect agent) → should have been ONE PR for the entire design pair
- v2 design pair (PR #315 spec + PR #316 plan) → should have been ONE PR
- Source-fix (PR #306) + orphan migration (PR #309) → could have been ONE PR (both are data-integrity unfucking)
- Ruff fix-forward (#311) + wrapper-fix-forward (#312) — never should have existed; verify gates locally before push

**Commit cadence (cheap):** keep committing locally as work progresses — single concept per commit. Operator wants the commit log to show progression.

**Push cadence (expensive):** push only when a MAJOR DELIVERABLE is ready. Examples that constitute "major":
- A complete design pair (spec + plan + supporting docs)
- A complete bug fix WITH its tests + any cascading updates
- A complete feature with infrastructure + runbook + tests

**Push rate guideline:** aim for ≤3 pushes per work session. If you're about to push your 4th PR in a session, STOP and ask whether it should have been folded into an existing branch.

**Canonical references:** <https://git-scm.com/book/en/v2/Git-Basics-Recording-Changes-to-the-Repository> + <https://docs.github.com/en/get-started/using-github/github-flow>. The pattern is commit-often-locally / push-when-a-coherent-batch-is-ready. Pushes are the expensive operation (CI runs, GitHub Actions minutes); commits are free.

**Why:** my recurring pattern this session — make a code change, leave it uncommitted in the worktree, dispatch a subagent, switch focus, the worktree accumulates 6-8 uncommitted files, eventually I lose track of what's in flight vs what's on main. The plan doc was IN my worktree but NOT pushed → operator couldn't see it → had to fix forward.

**How to apply (every code-change-touching turn):**

1. **Commit often.** After each logical unit of work — a single bug fix, a single test addition, a single doc — `git add <specific files>` + `git commit`. Don't accumulate. Don't bundle unrelated changes into one commit.

2. **Branch per concern.** Each meaningful chunk gets its own branch off main (`git switch -c feat/<topic>` or `fix/<topic>` or `docs/<topic>`). NEVER pile multiple concerns on one branch unless they're the same task.

3. **Push after a batch is ready.** A "batch" = one focused PR's worth of work. After the batch lands as commits on a branch: `git push -u origin <branch>` + `gh pr create --title ... --body ...` immediately. Don't sit on a finished batch.

4. **Check CI after every push.** `gh pr checks <#>` immediately after the push. If a check fails, fix it in-thread NOW — don't move on to other work while a red PR sits in the queue.

5. **Worktree hygiene.** At any given moment in a session, my worktree's `git status` should have ZERO uncommitted changes UNLESS I'm actively editing files in this turn. If `git status` shows leftover files from earlier turns, they belong in their own branch+PR — split them out before doing anything new.

6. **Specific-file `git add`, never bulk.** `git add -A` or `git add .` accidentally include stale subagent leftovers, untracked archive directories, `.venv` symlinks. Always: `git add <specific path 1> <specific path 2>`. The PR-shipped fix for the `.venv` self-symlink trap traced to bulk-add behavior.

7. **gh pr checks not gh run watch.** Gate on `statusCheckRollup` SUCCESS, not on `mergeStateStatus` clean (per CLAUDE.md hard rule).

8. **Run pytest LOCALLY before push, not just ruff.** Operator 2026-05-23 after PRs #310 + #311 shipped red CI back-to-back: *"the ci failes catches your fuck ups, why dont you laern from that and stop fucking up"*. PRs #310 (ruff F401) and #311 (orphan-script test) both went red because I only ran `ruff check` locally — missed that the orphan-script test catches new `scripts/*.py` without pipeline wiring. The right local-gate sequence for ANY script-creating PR:
   ```
   .venv/bin/ruff check <file>              # ruff first
   .venv/bin/python -m pytest scripts/tests/test_no_orphan_scripts.py -p no:xdist -q
   .venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q -k "<targeted topic>"
   ```
   Targeted pytest is faster than full-suite but covers the test that gates THIS change. For changes touching feeds/engines: also run `python -m tpcore.scripts.check_imports` per CLAUDE.md heavy-lane gates.

9. **Don't chain `gh pr merge` after `gh pr checks --watch --fail-fast` in the same `&&` line.** `--watch --fail-fast` exits when ANY check fails OR completes — non-zero exit on a fail is supposed to stop the `&&` chain, but the merge can race in if the watch returns early or if not all checks have started. Run them as separate commands and verify `statusCheckRollup` is `["SUCCESS"]` before merging. Doing this back-to-back chain shipped 2 red PRs to main today.

## What to do when finding mid-session that I've accumulated debt

- STOP the new work
- `git status` — list every uncommitted file
- Decide which files belong together (which PR)
- For each cluster: `git switch -c <branch>` + `git add <specific files>` + commit + push + PR
- THEN return to the in-progress task

**Anti-pattern (what I keep doing wrong):**

- Edit file A for fix #1, then while waiting for subagent, edit file B for fix #2, then read file C for question — all in one branch, all uncommitted. By turn 20 I have 6+ unrelated changes piled up.

**The recurring 8-uncommitted-warning is the symptom.** When `git push` warns "Warning: N uncommitted changes" — that's the alarm that I've drifted from the discipline. Address it before doing the next push.

## Branch cleanup discipline (operator 2026-05-23: *"add to memory to clean up your branches after they are no longer needed. stop being so sloppy"*)

After every merge: delete the branch locally + on remote. `gh pr merge <#> --squash --delete-branch` handles the remote; `git branch -d <name>` (lowercase d — refuses unmerged) for local. The 120+ stale branches accumulated today are exactly the sloppiness — `--delete-branch` was passed but the LOCAL branch sometimes stayed because the worktree referenced it. Standing rule:

1. **Pass `--delete-branch` on every `gh pr merge`.** No exceptions.
2. **After merge, run `git branch -d <name>` locally.** Even if the merge succeeded server-side, the local branch may linger.
3. **If `git branch -d` refuses** (says "not fully merged"): the branch isn't really merged into your local main — `git fetch && git pull` first, then re-try the delete.
4. **Worktree branches:** when a subagent worktree completes + its PR is merged, run `git worktree remove -f -f <path>` THEN `git branch -d <name>`. The worktree owns the branch ref; removing the worktree first releases it.
5. **Periodic prune:** `git fetch origin --prune` removes refs to deleted remote branches. `git remote prune origin` is the same. Run it at session start AND after a batch of merges.
6. **No exceptions for "I might need this later".** The PR's squash-merged commit on main contains the history. If you NEED to reconstitute a branch later, `git checkout -b <name> <sha>` from the merge commit's parent works fine. Don't hoard.

**Anti-pattern to avoid:** "I merged 5 PRs today and now have 50 stale branches." Every merge has a corresponding cleanup. Run them as a pair.

## Related

- `feedback_push_when_tangible_batch_prs` — sister rule on PR-batching cadence
- `feedback_never_touch_shared_main_checkout` — separates per-session checkouts
