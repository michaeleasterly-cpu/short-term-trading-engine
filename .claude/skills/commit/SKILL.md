---
name: commit
description: "Slash-only wrapper that drafts and creates a single git commit in STE's conventional-commit style. HEREDOC-formatted message, Co-Authored-By footer, no .env / secret staging. Vendored 2026-06-04 from anthropics/claude-code plugins/commit-commands."
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git add:*), Bash(git commit:*)
---

# /commit

Authoritative external: <https://code.claude.com/docs/en/skills>.
Vendored 2026-06-04 from `anthropics/claude-code` `plugins/commit-commands/commands/commit.md`, adapted to STE conventions per `docs/audits/2026-06-03-vendor-vs-handrolled.md` §7 + operator decision §9 #7.

## Context to gather (run all four in parallel)

- `git status`
- `git diff HEAD`
- `git branch --show-current`
- `git log --oneline -10`  (so the message style matches recent commits)

## What this skill does

Drafts and creates a **single** git commit. NEVER pushes, NEVER opens a PR — that's `/commit-push-pr`.

## STE conventions to follow

1. **Conventional-commit prefix** matching recent style: `feat(<scope>):`, `fix(<scope>):`, `docs(<scope>):`, `chore(<scope>):`, `refactor(<scope>):`, `test(<scope>):`. Pick the scope from the changed files (e.g. `feat(claude):`, `fix(reversion):`, `docs(audits):`).
2. **Subject** ≤ 72 chars, imperative mood, no trailing period.
3. **Body** (optional, separated by blank line) explains *why*, not *what*. The diff explains the what.
4. **HEREDOC formatting** — pass the full message via `git commit -m "$(cat <<'EOF' ... EOF)"` so newlines + special chars survive.
5. **Co-Authored-By footer** — last line of every commit message:
   ```
   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```
6. **Never stage sensitive files** — `.env`, `.env.*`, `*.pem`, `credentials*`, `secrets/**`. If `git status` shows one of these untracked, list it and ask before staging. (`.env` is in `.gitignore`; this is a belt-and-suspenders check.)
7. **Stage by name** — avoid `git add -A` / `git add .` which can sweep in untracked files the operator didn't expect. Prefer explicit `git add <path1> <path2> …`.
8. **One commit per call** — don't batch multiple logical changes into one commit. If the diff spans two concerns, surface the split and ask which to commit first.

## Canonical invocation pattern

```bash
git add <paths>
git commit -m "$(cat <<'EOF'
feat(scope): short imperative subject

Optional body paragraph explaining the why. Wraps at ~72 chars.
Reference issues or audits if relevant.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

## What this skill does NOT do

- Does NOT push (`/commit-push-pr` does).
- Does NOT open a PR (`/commit-push-pr` does).
- Does NOT amend (`--amend` belongs in operator-explicit calls, not in this auto-flow).
- Does NOT skip hooks (`--no-verify` would defeat the manifest checker + pre-commit ruff; never silently bypass).
- Does NOT sign with GPG unless operator explicitly enables it.

## Adjacent SoT

- `.claude/skills/commit-push-pr/SKILL.md` — for the commit + push + PR flow.
- `.claude/skills/clean-gone/SKILL.md` — for post-merge worktree + branch cleanup.
- `.claude/rules/tests-and-ci.md` — the gate discipline ("`gh pr checks <n>`, NEVER `gh run watch`"; gate on `statusCheckRollup` conclusion==SUCCESS).
- `docs/DEV_PIPELINE_STANDARD.md` §0 §1 — lane decision + heavy-lane pipeline (this skill is the fast/default-lane commit step; heavy-lane goes through subagent split-review first).
