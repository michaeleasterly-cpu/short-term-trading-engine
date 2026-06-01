# Security Guidance

C0.4 canonical security policy for short-term-trading-engine.
Defines the 3-layer review cascade Claude and operator follow when a
diff touches a security-sensitive surface. Companion artifacts:

- `.claude/rules/security-guidance.md` — path-loaded rule that
  auto-injects this guidance into Claude's context on a
  security-sensitive diff.
- `.claude/skills/security-review/SKILL.md` — model-invocable
  manual review checklist tied to the cascade below.
- `tests/test_security_guidance_present.py` — presence sentinels
  that red CI if any of the above goes missing or drifts.

> **Master rule**: security review is *review-only*. No layer in
> this cascade is permitted to auto-fix, auto-merge, commit, push,
> change deployment config, modify Claude memory, install MCP
> servers, or print secrets. The operator is the final gate.

## §1 — Three-layer cascade

Every security-sensitive diff (per §2) must clear all three layers
before merge.

### Layer 1 — static checks (deterministic, mechanical)

Runs in CI on every push and on every pre-commit hook locally.

- **gitleaks** v8.30.1 (pinned) — `.gitleaks.toml` config +
  `.gitleaksignore` pins. Workflow:
  `.github/workflows/secret-scan.yml`. Sentinels:
  `tests/test_secret_scan_gate.py`. Audit baseline:
  `docs/audits/2026-05-21-public-repo-secret-audit.md`.
- **`scripts/check_manifests.py`** — H0 path-registry checks (PR
  #411) + C0.3 Claude-surface contract checks (PR #413). Reds CI
  on hook/agent/skill/workflow drift.
- **`tests/test_claude_surface_contract.py`** (C0.3, PR #413) —
  10 sentinels pinning hook shebangs / executable bits, hook
  forbidden commands (`gh pr merge`, `git push --force`,
  Anthropic API curl, memstore mutations), agent + skill
  frontmatter, workflow `contents: read` + `--allowedTools`
  read-only invariants.
- **`tests/test_path_registry_present.py`** (H0, PR #411) — 11
  sentinels pinning `.claude/path_registry.yaml` schema +
  consumer presence.
- **`tests/test_memory_boundary_present.py`** +
  **`tests/test_memory_index_size.py`** (C0.1, PR #412) — memory
  boundary + size sentinels.
- **ruff**, **vulture**, **forbidden-imports check** — code-level
  hygiene.

Layer-1 findings are **BLOCKING** by default (CI reds, PR cannot
merge through normal gates).

### Layer 2 — Claude review (advisory, fresh-context)

Two sub-paths depending on whether the diff triggers the
heavy-lane workflow.

#### Layer 2a — automatic heavy-lane Claude review

Triggers on the `claude_system ∪ heavy_lane` path filter from
`.claude/path_registry.yaml` (per `.github/workflows/claude-review-heavy-lane.yml`).
The Claude Code Action posts a single verdict comment:

- `VERDICT: PASS`
- `VERDICT: REQUEST_CHANGES`
- `VERDICT: NEEDS_OPERATOR_REVIEW`

The action runs with `contents: read` + `pull-requests: write`
permissions only. It must never commit, must never push, must
never auto-fix, must never auto-merge, and must never deploy.
C0.3 sentinels pin those invariants.

#### Layer 2b — manual `/security-review` skill

For diffs that are security-sensitive (per §2) but do NOT hit a
heavy-lane path glob, the operator (or Claude on the operator's
behalf) invokes the **model-invocable** `/security-review` skill
defined in `.claude/skills/security-review/SKILL.md`. It runs the
same 3-layer rubric as 2a but locally in the current session
instead of in CI. The skill is review-only and produces a verdict
comment string the operator can paste into the PR.

Layer-2 findings classify as **BLOCKING** /
**NEEDS_OPERATOR_REVIEW** / **ADVISORY** per §3.

### Layer 3 — operator gate (dispositive)

The branch policy blocks normal merge on `main`. Even with all CI
checks green and a Claude `VERDICT: PASS`, the operator (never
the reviewer, never an automated step) must explicitly authorize
the merge — for example `gh pr merge --admin` under the current
policy, OR a future policy that requires reviewer approval. No CI
gate and no Claude verdict can override the operator gate.

A **BLOCKING** finding from layer 1 or layer 2 means: do not
admin-override. Either fix the finding or reject the PR.

## §2 — Security-sensitive diff classes

A diff is security-sensitive if it touches any of:

1. Workflow permission changes — anything under
   `.github/workflows/**` (especially `permissions:` blocks).
2. GitHub Actions changes — workflow steps, allowedTools, action
   pins, secret references.
3. Secret scanning config changes — `.gitleaks.toml`,
   `.gitleaksignore`, `.github/workflows/secret-scan.yml`.
4. Pre-commit gate changes — `.pre-commit-config.yaml`.
5. Auth / session code changes — `console-api/src/auth/**`,
   `console/src/auth/**`, `console-api/src/middleware*`,
   `console/src/middleware*`, `console/src/proxy*`.
6. Database URL / credential handling changes — `tpcore/db.py`
   normalization, `.env` schema changes, connection-pool config.
7. Broker / API credential handling changes —
   `tpcore/order_management/alpaca_*.py`, FMP / Tradier / SEC /
   Finnhub adapter credential paths.
8. Claude settings / hooks / agents / skills changes —
   `.claude/settings.json`, `.claude/hooks/**`, `.claude/agents/**`,
   `.claude/skills/**`. (Subset overlaps with heavy-lane workflow
   filter via the `claude_system` registry group.)
9. MCP config changes — anything that adds, removes, or
   reconfigures MCP servers.
10. Deployment config changes — `railway.json`, `railpack.toml`,
    `Procfile`, `Dockerfile`, any new deploy workflow under
    `.github/workflows/**`.
11. Dependency changes touching auth / crypto / networking /
    subprocess / shell execution — `pyproject.toml`,
    `requirements*.txt`, `Pipfile*`, `.github/dependabot.yml`.
12. Anthropic memstore / memory access — code that opens
    `/v1/memory_stores/...` endpoints, edits
    `~/.claude/projects/.../memory/`, or modifies `MEMORY.md`
    discipline.

If unsure whether a diff is security-sensitive, treat it as
security-sensitive and run layer 2 (skill or workflow).

## §3 — Finding classification

| Class | Meaning | Consequence |
|---|---|---|
| **BLOCKING** | A security invariant is violated. Forbidden examples: secret committed; workflow must never grant `contents: write` to a review action; hook must never invoke `gh pr merge`; agent body must never authorize auto-merge. | Do not merge. Fix the finding (preferred) or reject the PR. Admin-override would be a security-policy violation. |
| **NEEDS_OPERATOR_REVIEW** | A real change with security implications that automated checks cannot adjudicate. Examples: new third-party action pinned to a SHA the operator hasn't audited; new dependency on a crypto library; auth-middleware refactor that preserves behavior but reshapes surface. | Operator reviews + decides. PR may merge only after explicit operator-recorded decision. |
| **ADVISORY** | A note worth surfacing but not blocking. Examples: dependency bump to a vetted upstream version; rule body wording tightened; cosmetic change to security doc. | Log in PR thread. May merge without further action. |

A single PR can carry multiple findings of different classes.
The aggregate verdict equals the most-severe class present (any
**BLOCKING** ⇒ `REQUEST_CHANGES`; any **NEEDS_OPERATOR_REVIEW**
⇒ `NEEDS_OPERATOR_REVIEW`; all **ADVISORY** or none ⇒ `PASS`).

## §4 — When to invoke `/security-review`

Invoke the `/security-review` skill in the current Claude session
when:

- A PR touches a §2 diff class but the heavy-lane workflow did
  not fire (default lane).
- A PR touched the workflow file itself and the heavy-lane review
  hit the `Workflow validation failed` 401 safeguard — manual
  review is the substitute.
- The operator wants a second look on a security-adjacent change
  before authorizing `--admin` merge.
- An automated layer-1 finding looks like a false positive and
  needs human classification (e.g., a gitleaks hit on a known
  fixture that isn't yet allowlisted).

The skill produces a verdict string identical in shape to the
heavy-lane Claude review (`VERDICT: PASS` /
`REQUEST_CHANGES` / `NEEDS_OPERATOR_REVIEW`) plus a numbered
findings list. The operator pastes it into the PR thread.

## §5 — Forbidden actions across every layer

Every layer of the cascade is review-only. Per-action prohibitions
follow; each bullet carries its own explicit ban so the negation
discipline survives layer-scanning sentinels.

- Never auto-fix code, never write to files, never commit on the
  operator's behalf.
- Never auto-merge, never auto-rebase, never invoke `gh pr merge`.
- Never `git push --force` / `--force-with-lease`.
- Never run `docker`, never `railway up`, never any deployment
  command. (Deploys are operator-controlled and out of scope for
  the security-review skill. A future dedicated deploy workflow
  would need its own contract test — see C0.3 scope note for the
  `Bash(docker` / `Bash(railway` allowedTools restriction on the
  review workflow.)
- Never write to Anthropic API memstores (the dev memstore
  `memstore_01P5DiJJgau4NhMMekaZDQEN` or the finder
  `memstore_01MzLun3AfRf2viPmDqJvsWi`).
- Never add or reconfigure MCP servers.
- Never print secret values to chat, PR comments, logs, or any
  persistent surface. Pattern hits must be **redacted** before
  surfacing.
- Never store secrets, API keys, broker credentials, Postgres
  URLs with embedded credentials, private financial balances, raw
  logs, or raw backtest dumps in any memory tier (`CLAUDE.md`,
  `MEMORY.md`, Anthropic memstores, repo docs).

Per `docs/MEMSTORE_HANDOFF.md` (C0.1): memory is context, not
source of truth — code, tests, schemas, migrations, and `docs/**`
override memory. The security cascade is no exception.

## §6 — Operator quick-reference

- A security-sensitive diff arrived → check that
  `.github/workflows/secret-scan.yml` and
  `.github/workflows/claude-review-heavy-lane.yml` both ran (or
  hit the workflow-validation safeguard, in which case manually
  invoke the skill).
- Reviewing the PR → run `/security-review` if the heavy-lane
  Claude action did not produce a verdict.
- About to admin-override → no **BLOCKING** finding may remain;
  every **NEEDS_OPERATOR_REVIEW** must have a recorded decision.
- Storing follow-up context → `TODO.md` (tracked) or the per-fact
  `.claude/projects/.../memory/<name>.md` (local memstore tier
  per C0.1). Never paste credentials or raw logs.
