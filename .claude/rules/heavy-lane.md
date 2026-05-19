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
  - "ops/llm_data_triage.py"
  - "ops/engine_llm_triage.py"
  - "platform/migrations/**"
  - "tpcore/engine_profile.py"
  - "tpcore/providers.py"
description: "Path-scoped rule: heavy lane triggers — the full §1 pipeline of docs/DEV_PIPELINE_STANDARD.md is mandatory when any of these paths is touched."
---

# Heavy lane (full §1 pipeline mandatory)

Canonical SoT: `docs/DEV_PIPELINE_STANDARD.md` §0 + §1.
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

Triggers (this rule's `paths:`):
- `tpcore/risk/**` — platform-wide RiskGovernor / capital-gate (live-money trade path)
- `tpcore/selfheal/**`, `tpcore/auditheal/**` — autonomous repair / cross-table audit (100%-green-or-don't-trade invariant)
- `tpcore/quality/validation/**` — data-acceptance gate (`DATA_OPERATIONS_COMPLETE` predicate)
- `ops/engine_service.py`, `ops/engine_sdlc.py`/`ops/engine_sdlc/**` — engine dispatch + SDLC ECR mutator
- `ops/llm_data_triage.py`, `ops/engine_llm_triage.py` — advisory LLM lanes (advisory-only is a safety contract; spec change ≠ implementation change)
- `platform/migrations/**` — Alembic (schema is the durable substrate; rollback discipline)
- `tpcore/engine_profile.py` — the engine roster SoT
- `tpcore/providers.py` — the data-feed ProviderBinding SoT
- New engine (5-plug `<engine>/` scaffold) and new data adapter — covered by `engine-build` / `data-adapter` rules

Default and fast lanes are explicitly NOT permitted for these paths.
