---
name: feedback_never_git_add_all_sweeps_secrets
description: "NEVER `git add -A` / `git add .` — it sweeps untracked secret files (the .env.bak incident). Stage explicit paths."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

**NEVER use `git add -A` or `git add .`** — stage explicit paths only (`git add <file1> <file2>`).

**Why:** 2026-06-05, a `git add -A` in a Plan 2 commit swept in the operator's untracked `.env.bak` (a plaintext backup of `.env` with real Anthropic/Vercel/Alpaca/DB secrets). It rode 27 local commits; **GitHub push-protection (GH013) rejected the push** ("Push cannot contain secrets") — the secrets never reached the remote, but I had to `git filter-branch` `.env.bak` out of the local history before re-pushing, which also deleted the operator's on-disk `.env.bak`. Root causes: (1) `git add -A` stages EVERYTHING untracked, including secret files; (2) `.gitignore` had `.env`/`.env.local` but NOT `.env.bak`/`.env.*` (now fixed: `.gitignore` has `.env.*` + `!.env.example`).

**How to apply:** stage by explicit path every time. If a broad add is unavoidable, `git status` first and confirm no untracked secret/`.env*`/dump files are about to be staged. The pre-commit gitleaks hook + GitHub push-protection are backstops, NOT the primary defense — explicit staging is. Relates to [[feedback_run_gates_locally_on_commit]] and the public-repo secret discipline (CLAUDE.md universal invariants: secrets only in `.env`, gitignored).
