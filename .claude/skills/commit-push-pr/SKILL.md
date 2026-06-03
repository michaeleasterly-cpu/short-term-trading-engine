---
name: commit-push-pr
description: "Slash-only wrapper that commits, pushes, and opens a PR in STE's conventional style. HEREDOC commit + PR body, Co-Authored-By footer, Test plan section, `gh pr checks <n>` for CI gate. Vendored 2026-06-04 from anthropics/claude-code plugins/commit-commands."
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(gh pr create:*), Bash(gh pr view:*), Bash(gh pr checks:*)
---

# /commit-push-pr

Authoritative external: <https://code.claude.com/docs/en/skills>.
Vendored 2026-06-04 from `anthropics/claude-code` `plugins/commit-commands/commands/commit-push-pr.md`, adapted to STE conventions per `docs/audits/2026-06-03-vendor-vs-handrolled.md` §7 + operator decision §9 #7.

## Context to gather (run in parallel)

- `git status`
- `git diff HEAD`
- `git branch --show-current`
- `git log --oneline -10`

## What this skill does

1. Stage relevant changes (by explicit path — not `-A`).
2. Create **one** commit using `/commit`'s conventions (conventional-commit prefix, HEREDOC body, Co-Authored-By footer).
3. Push the branch with `-u origin HEAD`.
4. Open a PR with `gh pr create --base main` using HEREDOC body.
5. Output the PR URL.

## Branch policy

- **NEVER push to `main` directly** — if `git branch --show-current` returns `main`, surface the error and stop. The operator must create a branch first.
- **Match the existing branch's prefix** — `worktree-<name>`, `feature/<slug>`, `fix/<slug>`, `chore/<slug>`. If the branch was created via `EnterWorktree` or `git worktree add -b`, the name is already set.
- **Branch base** — the operator's `.claude/settings.json` is set to `worktree.baseRef: "fresh"` (2026-06-04, controls-audit #4), so EnterWorktree branches start at `origin/main`. The `.github/workflows/branch-base-sentinel.yml` will red the PR if HEAD isn't descended from `origin/main`.

## PR title

- Same conventional-commit shape as the commit subject: `feat(scope): …`, `fix(scope): …`, `docs(audits): …`, `chore(claude): …`.
- ≤ 70 chars.

## PR body template

```bash
gh pr create --base main --title "<conventional-commit title>" --body "$(cat <<'EOF'
## Summary

<1–3 sentences. What changed and why.>

## Test plan

- [x] Whatever you actually ran (cite the command + result)
- [x] Specific sentinel suites green if applicable
- [ ] `gh pr checks <n>` — gate on `statusCheckRollup` conclusion==SUCCESS

## Authoritative external

<links to Anthropic docs / Postgres docs / etc. when relevant>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## CI discipline (the standing rules)

- **`gh pr checks <n>`**, NEVER `gh run watch` (the latter's exit code is a documented misreport — `.claude/rules/tests-and-ci.md`).
- Gate on `statusCheckRollup` conclusion==SUCCESS, NOT `mergeStateStatus`==CLEAN (docs-path PRs flip CLEAN before pytest finishes — 2026-05-19 memo).
- If the CI fails on a known pre-existing flake (e.g. the `tpcore/tests/test_upsert_bars_provenance_guard.py` DATABASE_URL leak documented in `MEMORY.md`), note it in the PR body and don't retry blindly.

## What this skill does NOT do

- Never merges the PR — operator authorizes merges manually.
- Never enables auto-merge — operator-only per the standing memory.
- Never skips hooks (`--no-verify` would defeat the manifest checker + pre-commit ruff).
- Never uses bare force-push — `--force-with-lease` only, and only after a rebase with operator confirmation.
- Never opens multiple PRs in one call — one commit, one PR.

## Adjacent SoT

- `.claude/skills/commit/SKILL.md` — commit-only flow.
- `.claude/skills/clean-gone/SKILL.md` — post-merge cleanup.
- `.claude/rules/tests-and-ci.md` — CI-gate discipline.
- `.claude/rules/heavy-lane.md` — for heavy-lane paths, this skill is NOT enough; spec → plan → subagent split-review → operator gate is required first.
- `.github/workflows/branch-base-sentinel.yml` — PR-time check that base is ancestor of HEAD.
- `docs/DEV_PIPELINE_STANDARD.md` — full lane + pipeline standard.
