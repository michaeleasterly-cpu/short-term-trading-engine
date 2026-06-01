---
name: heavy-lane
paths:
  - "tpcore/risk/**"
  - "tpcore/selfheal/**"
  - "tpcore/auditheal/**"
  - "tpcore/quality/validation/**"
  - "ops/engine_service.py"
  - "ops/engine_sdlc.py"
  - "ops/engine_sdlc/**"
  - "ops/data_feed_sdlc/**"
  - "ops/cutover_agent.py"
  - "scripts/ops.py"
  - "platform/migrations/**"
  - "tpcore/engine_profile.py"
  - "tpcore/providers.py"
description: "Path-scoped rule: heavy lane triggers — the full §1 pipeline of docs/DEV_PIPELINE_STANDARD.md is mandatory when any of these paths is touched."
---

# Heavy lane (full §1 pipeline mandatory)

Canonical path SoT: `.claude/path_registry.yaml` (H0 hardening, 2026-06-01) — the frontmatter `paths:` above mirrors `groups.heavy_lane.paths` and is verified by `scripts/check_manifests.py::check_heavy_lane_rule_frontmatter_equals_registry` (sentinel: `tests/test_path_registry_present.py`).
Process SoT: `docs/DEV_PIPELINE_STANDARD.md` §0 + §1.
Authoritative external: <https://code.claude.com/docs/en/extend> (the extension layers; rules/skills/agents/hooks).

If the change touches any path listed in this rule's `paths:` frontmatter, the lean default does NOT apply. The change goes through the full §1 pipeline:

1. Brainstorm
2. Expert-subagent harden
3. Spec = gated docs-only PR
4. Operator spec-read gate
5. Plan = gated docs-only PR
6. Subagent-driven execution
7. **Split-review** (spec-compliance then, on PASS, a separate fresh-context code-quality reviewer)
8. Implementer folds findings
9. Gated PR
10. CI via `gh pr checks <n>` — gate on `statusCheckRollup` conclusion==SUCCESS, NOT `mergeStateStatus`
11. Whole single-process pytest + bidirectional order-flip is the authoritative gate
12. Squash-merge `--delete-branch`
13. `git switch main && git pull` sync

Triggers (mirrors `.claude/path_registry.yaml` `groups.heavy_lane`; per-path `why` lives in the registry):
- `tpcore/risk/**` — platform-wide RiskGovernor / capital-gate (live-money trade path)
- `tpcore/selfheal/**` — autonomous self-heal (100%-green-or-don't-trade invariant)
- `tpcore/auditheal/**` — cross-table audit + bounded `cross_ref_cleanup`
- `tpcore/quality/validation/**` — data-acceptance gate (`DATA_OPERATIONS_COMPLETE` predicate)
- `ops/engine_service.py` — consolidated engine dispatch daemon
- `ops/engine_sdlc.py` / `ops/engine_sdlc/**` — ECR mutator entrypoint + package
- `ops/data_feed_sdlc/**` — DFCR mutator + data-feed-lifecycle planner
- `ops/cutover_agent.py` — automated provider-CUTOVER agent (parity-gated swap)
- `scripts/ops.py` — operator-on-demand stage registry. New stages adjacent to the DFCR / cutover path are heavy-lane-by-discipline.
- `platform/migrations/**` — Alembic (schema is the durable substrate; rollback discipline)
- `tpcore/engine_profile.py` — engine roster SoT
- `tpcore/providers.py` — data-feed ProviderBinding SoT
- New engine (5-plug `<engine>/` scaffold) and new data adapter — covered by `engine-build` / `data-adapter` rules

Default and fast lanes are explicitly NOT permitted for these paths.

## Automated first-pass reviewer (advisory)

When a PR touches any of these paths, the workflow
`.github/workflows/claude-review-heavy-lane.yml` (Anthropic
``claude-code-action`` v1) posts a first-pass review comment with a
verdict of `PASS` / `REQUEST_CHANGES` / `NEEDS_OPERATOR_REVIEW`. This
is **advisory / review-only** — the workflow has `contents: read` and
`pull-requests: write` permissions ONLY, never `contents: write`. It
cannot commit, push, auto-fix, or auto-merge.

The operator remains the final gate. A `VERDICT: PASS` is necessary
but not sufficient for merge; the §1 pipeline above (spec → plan →
subagent execution → split-review → operator authorization) is
unchanged.

The workflow's `paths:` filter equals exactly `groups.heavy_lane ∪ groups.claude_system` from `.claude/path_registry.yaml`; drift in either direction reds CI via `scripts/check_manifests.py` and the sentinel tests.
