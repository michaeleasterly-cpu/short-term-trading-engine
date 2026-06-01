---
name: security-guidance
paths:
  - ".github/workflows/secret-scan.yml"
  - ".github/workflows/**"
  - ".gitleaks.toml"
  - ".gitleaksignore"
  - ".pre-commit-config.yaml"
  - "console-api/src/middleware*"
  - "console-api/src/auth/**"
  - "console/src/middleware*"
  - "console/src/auth/**"
  - "console/src/proxy*"
  - "tpcore/db.py"
  - "tpcore/order_management/alpaca_*.py"
  - "pyproject.toml"
  - "requirements*.txt"
  - "Pipfile*"
  - ".github/dependabot.yml"
description: "Path-scoped rule: load the security-guidance cascade when a diff touches a security-sensitive surface — secret-scan config, auth/middleware, DB credentials, broker credentials, dependency declarations, or workflow files."
---

# Security guidance (auto-loaded on security-sensitive diffs)

Canonical policy: `docs/SECURITY_GUIDANCE.md`.
Manual review entrypoint: `.claude/skills/security-review/SKILL.md` (model-invocable; suggest invoking when one of the path globs above matches).

## Why this rule loads

This rule is auto-loaded when a diff touches any path in the frontmatter `paths:` glob (per Claude Code's path-scoped rule mechanism). The path list covers the security-sensitive diff classes enumerated in `docs/SECURITY_GUIDANCE.md` §2 — secret-scan configuration, auth / middleware / session code, database + broker credential handling, dependency declarations, and the workflow surface.

## What to do when this rule fires

1. **Read `docs/SECURITY_GUIDANCE.md` §1 (the 3-layer cascade).** Layer 1 is mechanical (gitleaks + `scripts/check_manifests.py` + the C0.3 + H0 + C0.1 sentinels). Layer 2 is Claude review (automatic via `.github/workflows/claude-review-heavy-lane.yml` for `heavy_lane ∪ claude_system` paths, manual via `/security-review` skill otherwise). Layer 3 is the operator gate.
2. **Suggest invoking `/security-review` if the heavy-lane workflow did not fire** OR if it hit the `Workflow validation failed` safeguard and produced no verdict. The skill is model-invocable; you can recommend it in chat. Do not invoke deployment commands, do not modify code, do not auto-merge.
3. **Classify findings using the §3 taxonomy**: `BLOCKING` / `NEEDS_OPERATOR_REVIEW` / `ADVISORY`. The aggregate PR verdict equals the most-severe class present.

## What this rule does NOT authorize

This rule is advisory. Per-action prohibitions follow; each bullet carries its own explicit ban so layer-scanning sentinels classify the rule as enforcing — not authorizing — the forbidden pattern.

- Never auto-fix the security-sensitive diff (Layer 2 is review-only — see `docs/SECURITY_GUIDANCE.md` §5).
- Never auto-merge the PR, never force-push, never invoke `gh pr merge` (operator-only per the §1 layer-3 gate).
- Never run `docker`, never `railway up`, never any deploy command (operator-controlled; out of scope for the security-review path).
- Never write to Anthropic API memstores; never modify local memory (`MEMORY.md` / per-fact files). Memory writes during a security review are forbidden per `docs/SECURITY_GUIDANCE.md` §5.
- Never add or reconfigure MCP servers (out of scope).
- Never print secret values to chat, PR comments, or any persistent surface — redact before surfacing.

The operator is the dispositive gate. Even a `VERDICT: PASS` from the heavy-lane Claude action or from the `/security-review` skill is necessary but not sufficient for merge — the operator authorizes admin-override or merge through the GitHub UI.

## Cross-links

- Policy: `docs/SECURITY_GUIDANCE.md`
- Static checks: gitleaks (`.gitleaks.toml`, `.github/workflows/secret-scan.yml`), `scripts/check_manifests.py`, `tests/test_secret_scan_gate.py`, `tests/test_claude_surface_contract.py`, `tests/test_path_registry_present.py`
- Auto Claude review: `.github/workflows/claude-review-heavy-lane.yml`
- Manual review: `.claude/skills/security-review/SKILL.md`
- Memory boundary (no memory writes during review): `docs/MEMSTORE_HANDOFF.md`
- Presence sentinel: `tests/test_security_guidance_present.py`
