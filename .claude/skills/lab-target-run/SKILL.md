---
name: lab-target-run
description: "Slash-only wrapper for the canonical Lab on-demand search command — python -m ops.lab --candidate <name> --target-engine <engine> --intent {promote_new|fold_existing}. Emits a docs/lab/<dossier>.md + byte-frozen .json sidecar."
disable-model-invocation: true
---

# Lab target run

Canonical CLI: `python -m ops.lab --candidate <NAME> --target-engine <ENGINE> --intent {promote_new|fold_existing}`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs the on-demand edge-hunt **Lab** (recommendation-only; never daemon-wired; per CLAUDE.md). Produces:

- `docs/lab/<NAME>-<verdict>-seed<N>.md` — the human-readable dossier
- `docs/lab/<NAME>-<verdict>-seed<N>.json` — byte-frozen sidecar the ECR planner re-verifies

Both files are inputs to the **ECR ADD/MODIFY** flow (see `/ecr` skill).

## Usage

```bash
python -m ops.lab \
    --candidate <name>             # one pre-registered hypothesis
    --target-engine <reversion|vector|momentum|sentinel|catalyst|...> \
    --intent {promote_new|fold_existing}
```

Pre-conditions (`docs/superpowers/checklists/lab_candidate_readiness.md` — read it before invoking):

- Single pre-registered primary hypothesis; no post-hoc metric shopping.
- Feature-flag-variant pattern: off-by-default backtest path, one Lab param toggle, byte-identical live path.
- n_trials-ledger acknowledgement: every run is a cumulative trial against the target.
- Roster-targeting prerequisite (post-SP-B): target engine in `tpcore.engine_profile.lab_targetable_engines()` with a declared `LAB_TARGET`.

## After the run

If verdict is SURVIVED + DSR ≥ 0.95 ∧ credibility ≥ 60: route the dossier to the ECR (`/ecr` skill) for the appropriate `intent`. Otherwise stop — the gate is sacred; never re-tune past it without a fresh single-hypothesis run.

## Adjacent SoT

- `docs/superpowers/checklists/lab_candidate_readiness.md`
- `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`
- `.claude/skills/ecr/SKILL.md`
