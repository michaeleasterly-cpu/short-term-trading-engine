<!--
  STE PR template. The lane declaration + heavy-lane sections are
  read by humans AND by .github/workflows/claude-review-heavy-lane.yml.
  Keep them — the Claude review action and the manual reviewer both
  rely on this structure.

  Reference: docs/DEV_PIPELINE_STANDARD.md §0 (lane decision) + §1
  (heavy-lane pipeline).
-->

## Lane

<!--
  Pick exactly one. Lane definitions live in
  docs/DEV_PIPELINE_STANDARD.md §0.
-->

- [ ] **fast** — one-sentence diff, single file or docs-only, no
      migration, no new public API. Implement → verify → commit.
- [ ] **default** — Anthropic Explore → Plan → Implement → Commit
      with one fresh-context review.
- [ ] **heavy** — touches a heavy-lane path (see below). FULL §1
      pipeline mandatory: brainstorm → expert-harden → spec → plan →
      execution → split-review → operator gate.

## Touched risk paths

<!--
  Check ALL that apply. Any check here forces the heavy lane and
  triggers .github/workflows/claude-review-heavy-lane.yml. The
  canonical heavy-lane path list lives in
  .claude/path_registry.yaml (groups.heavy_lane); this checklist
  mirrors it and is verified by scripts/check_manifests.py.
-->

- [ ] `tpcore/risk/**`
- [ ] `tpcore/selfheal/**`
- [ ] `tpcore/auditheal/**`
- [ ] `tpcore/quality/validation/**`
- [ ] `ops/engine_service.py`
- [ ] `ops/engine_sdlc.py`
- [ ] `ops/engine_sdlc/**`
- [ ] `ops/data_feed_sdlc/**`
- [ ] `ops/cutover_agent.py`
- [ ] `scripts/ops.py`
- [ ] `platform/migrations/**`
- [ ] `tpcore/engine_profile.py` (the engine roster SoT — ECR-only)
- [ ] `tpcore/providers.py` (the data-feed ProviderBinding SoT — DFCR-only)
- [ ] `.claude/` extension surface (rules/skills/agents/hooks)
- [ ] `.github/workflows/**`
- [ ] None of the above

## Spec / rule reference (heavy lane only)

<!--
  Required for heavy lane. Format:
    Spec: docs/superpowers/specs/<slug>.md (PR #<n> if it was its own
          docs-only PR)
    Plan: docs/superpowers/plans/<slug>.md (PR #<n>)
    Rule(s): .claude/rules/<rule>.md
  Fast and default lanes may leave this blank.
-->

(leave blank for fast/default lane)

## Summary

<!--
  1–3 sentences. What changed and why.
-->

## Tests run

<!--
  Paste the verification commands you ran. Heavy lane requires the
  whole-suite serial gate + order-flip (ci.yml AUTHORITATIVE step).
-->

- [ ] `python -m pytest -p no:xdist -q` (whole-suite serial)
- [ ] `python -m ruff check`
- [ ] `python -m vulture --min-confidence 60 tpcore ops reversion vector momentum sentinel canary catalyst dashboard_components vulture_allowlist.py`
- [ ] `gitleaks detect --config .gitleaks.toml --no-banner --redact --source .`
- [ ] Other:

## Migration impact

- [ ] No schema change.
- [ ] Migration added; head: `<revision_id>`. Reversible: yes/no.
      Live DB applied: yes/no.

## Data-quality impact

- [ ] No change to validators (`tpcore/quality/validation/checks/`).
- [ ] Validator added/changed:
      `tpcore/quality/validation/checks/<name>.py`. HealSpec updated.

## Live/backtest parity impact

- [ ] No change to engine signal / execution / cost model.
- [ ] Change affects parity (`tpcore/parity/`) — parity harness rerun
      planned: yes/no.

## Broker / order / risk impact

- [ ] No change to `tpcore/risk/`, `tpcore/order_management/`, or
      Alpaca adapter (`tpcore/alpaca/`).
- [ ] Risk-path change (heavy lane required) — RiskGovernor
      `check_trade` order + invariants reviewed.

## Memory impact (Claude / API memstores)

- [ ] No memory writes.
- [ ] Local memory file added/updated:
      `.claude/projects/.../memory/<name>.md`
- [ ] CLAUDE.md / MEMORY.md change documented in PR body.

## Out of scope (explicitly)

<!--
  List anything reviewers might expect that this PR deliberately does
  NOT touch. Helps reviewers stop chasing imagined regressions.
-->

(leave blank if obvious)
