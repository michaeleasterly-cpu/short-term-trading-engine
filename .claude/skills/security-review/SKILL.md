---
name: security-review
description: "Run a security-review pass on a security-sensitive diff that did not trigger the heavy-lane Claude review workflow (or where the workflow hit the validation safeguard). Walks the 3-layer cascade (static → Claude review → operator gate) and produces a verdict comment. Review-only — never modifies code, never auto-merges, never deploys."
---

# Security review (manual / model-invocable)

Canonical policy: `docs/SECURITY_GUIDANCE.md`.
Path-loaded rule: `.claude/rules/security-guidance.md`.

## What this skill does

Walks the 3-layer security cascade against the current diff and produces a single verdict comment string in the same shape the heavy-lane Claude review action would post. Suitable for:

- Security-sensitive diffs (per `docs/SECURITY_GUIDANCE.md` §2) that landed in default lane.
- Heavy-lane PRs where the Claude review hit the `Workflow validation failed` 401 safeguard (action couldn't authenticate; no verdict was posted).
- A second look before authorizing `--admin` merge on any security-touching PR.

## What this skill MUST NOT do

This skill is **review-only**. Per-action prohibitions follow; each bullet carries its own explicit ban so layer-scanning sentinels classify the skill as enforcing — not authorizing — the forbidden pattern.

- Never modify code, never write files, never commit, never push, never rebase.
- Never invoke `gh pr merge`; never auto-merge; never auto-fix; never auto-rebase; never force-push.
- Never run `docker`, never `railway up`, never any deployment command (deployment is operator-controlled and out of scope for security review).
- Never write to Anthropic API memstores (`memstore_01P5DiJJgau4NhMMekaZDQEN` / `memstore_01MzLun3AfRf2viPmDqJvsWi`); never modify local memory (`MEMORY.md` / per-fact files under `~/.claude/projects/.../memory/`).
- Never add or reconfigure MCP servers.
- Never print secret values to chat, PR comments, logs, or any persistent surface. Pattern matches must be **redacted** before surfacing — never quote the raw value.

## Inputs

The invoker provides:

- The PR number (or base + head SHAs).
- A short scene-setting paragraph: what the change is, what lane it landed in, why a manual security review is being run.

## Procedure

### Step 1 — confirm the layer-1 static gate ran clean

Verify on the PR (read-only `gh pr checks <n>` + `gh pr view <n>` allowed):

- `pytest + ruff + check_imports` — SUCCESS
- `gitleaks (worktree + SARIF)` — SUCCESS
- `gitleaks` — SUCCESS
- `lab-isolation-db (Postgres-gated suites)` — SUCCESS
- If `Claude review (heavy-lane paths)` was expected but is missing / cancelled / failed on the workflow-validation safeguard, note that explicitly — this skill is the substitute, not an addition.

Any failed layer-1 check that isn't a known pre-existing failure (e.g., `test_upsert_bars_provenance_guard` env-leak) is a **BLOCKING** finding by itself.

### Step 2 — classify the diff against the §2 sensitive-class catalogue

Read `gh pr diff <n> --name-only` and check each file path against `docs/SECURITY_GUIDANCE.md` §2. Note every match.

### Step 3 — review the diff against the per-class rubric

For each matched class, work through the checklist below. Cite file:line for each finding.

#### Workflow / Actions changes (§2.1, §2.2)
- Any new `permissions:` block grants broadened? (`contents: read` must NOT become `contents: write` on any review/comment action.)
- Any third-party action pinned to `@main` instead of `@vX` or a SHA?
- Any new secret reference (`${{ secrets.* }}`) that doesn't exist or whose purpose isn't documented?
- Any `--allowedTools` list on a Claude Code Action invocation that improperly includes mutating tools? These must never appear in `--allowedTools`: `Bash(gh pr merge`, `Bash(git push`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash(docker`, `Bash(railway`, `Bash(gh api --method PATCH|POST|PUT|DELETE`.

#### Secret-scan + pre-commit changes (§2.3, §2.4)
- Any `[[allowlists]]` widened to a broad path-only suppression? (Narrow path + regex pairing is required.)
- Any gitleaks rule disabled without an audit-doc rationale?
- Any pre-commit pin moved off the gitleaks repo or off the pinned `v8.30.1` rev without updating the CI workflow pin in the same PR? (`tests/test_secret_scan_gate.py` should catch drift, but flag for the operator.)

#### Auth / session / middleware changes (§2.5)
- Are public-route exclusions widened? (Per the existing operator memory: a new dashboard slug not added to the negative-lookahead matcher is a regression class.)
- Is session-token handling reshaped? Are tokens written to logs, env vars, or persistent surfaces they weren't before?
- Is CORS / CSRF / cookie policy weakened?

#### DB credential / connection handling (§2.6)
- Does `tpcore/db.py` normalization change? Does the change preserve the credential-redaction guarantees the normalizer provides?
- Are connection strings logged or returned anywhere?

#### Broker / API credential handling (§2.7)
- Are credentials read from env into log lines, error strings, or HTTP request bodies that aren't TLS-encrypted to the vendor's documented endpoint?
- Does an adapter change shift the credential source (env → file, etc.)?

#### Claude settings / hooks / agents / skills (§2.8)
- The C0.3 sentinels (`tests/test_claude_surface_contract.py`) cover the mechanical invariants. Verify those tests ran and passed. Additional manual review: does any new agent or skill body fail to clearly forbid auto-merge / auto-fix / auto-rebase semantics? A long instruction where the explicit ban sits far from the forbidden pattern can escape the static scan and must never authorize the behavior in any reading.

#### MCP changes (§2.9)
- Any new MCP server added or reconfigured? MCP changes are categorically **NEEDS_OPERATOR_REVIEW** at minimum — review with the operator before classifying as ADVISORY.

#### Deployment config (§2.10)
- `railway.json` / `railpack.toml` / `Procfile` / `Dockerfile` touched? Classify based on whether the change is operator-side (e.g., a documented prod env var addition) or a code-shaped reshape (the latter is **NEEDS_OPERATOR_REVIEW** at minimum).

#### Dependency changes (§2.11)
- New auth / crypto / networking / subprocess / shell-execution dependency? Check pinning, upstream source, and whether the project already has an equivalent (avoid duplication of attack surface).
- Bump on an existing dependency? Note the version delta and whether security-relevant CVEs are addressed.

#### Memory / memstore access (§2.12)
- Any code that opens `/v1/memory_stores/...` endpoints, edits `~/.claude/projects/.../memory/`, or modifies `MEMORY.md` discipline?

### Step 4 — classify each finding

For every finding from step 3, assign a class per `docs/SECURITY_GUIDANCE.md` §3:

- **BLOCKING** — security invariant violated; PR cannot merge.
- **NEEDS_OPERATOR_REVIEW** — change with security implications that the operator must adjudicate; PR may merge only after explicit operator decision.
- **ADVISORY** — note worth surfacing but not blocking.

### Step 5 — produce the verdict comment

Output a single block in this shape (do NOT auto-post; the operator pastes it):

```
SECURITY REVIEW — manual /security-review on PR #<n>

Layer 1 (static): <PASS|FAIL with specifics>
Layer 2 (heavy-lane Claude review): <PASS|FAIL|CANCELLED|N/A — default lane>

Findings:
1. [<CLASS>] <file:line> — <one-line description>. Rationale: <why this class>.
2. [<CLASS>] ...

VERDICT: <PASS | REQUEST_CHANGES | NEEDS_OPERATOR_REVIEW>
```

Aggregate verdict rule (per `docs/SECURITY_GUIDANCE.md` §3):

- Any **BLOCKING** present ⇒ `REQUEST_CHANGES`.
- Else any **NEEDS_OPERATOR_REVIEW** present ⇒ `NEEDS_OPERATOR_REVIEW`.
- Else (all **ADVISORY** or none) ⇒ `PASS`.

## When to suggest invoking this skill

You (Claude) should suggest invoking `/security-review` in chat when:

- The current diff touches any path in the `.claude/rules/security-guidance.md` `paths:` frontmatter glob AND the heavy-lane review either did not fire (default lane) or hit the `Workflow validation failed` safeguard.
- An operator authorizes `--admin` merge on a security-touching PR without an explicit prior review pass.
- A layer-1 finding looks like a false positive and needs human classification (e.g., a gitleaks hit on a known test fixture not yet in the allowlist).

## Acceptance of authority

This skill is a **manual / fresh-context review pass**. It does NOT:

- Replace the operator gate (§1 layer 3 in the policy doc).
- Authorize bypass of the base-branch policy.
- Authorize admin-merge on a finding-flagged PR.

A `VERDICT: PASS` from this skill is necessary-but-not-sufficient for merge, exactly like the heavy-lane Claude review verdict. The operator remains dispositive.
