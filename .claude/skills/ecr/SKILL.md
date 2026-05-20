---
name: ecr
description: "Slash-only wrapper for the canonical Engine Change Request — python -m ops.engine_sdlc --ecr <file>. The single structured touchpoint for ADD / REMOVE / MODIFY of an engine; never hand-edit tpcore.engine_profile._PROFILE."
disable-model-invocation: true
---

# Engine Change Request (ECR)

Canonical CLI: `python -m ops.engine_sdlc --ecr <path-to-filled-file>`.
Structured touchpoint: `docs/superpowers/checklists/engine_change_request.md` (the SoT).
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Routes an engine roster/lifecycle change through the deterministic SDLC gates. The operator approves **only two operations** via binary `APPROVE? (y/n)` on a planner-validated diff: **ADD** an engine (new scaffold or Lab-graduated) and **REMOVE** one (retire/archive). MODIFY (re-tuned params past DSR ≥ 0.95 ∧ credibility ≥ 60) + LAB→PAPER promote are automated, deterministic, no approval.

## Usage

1. Fill the ECR block from `docs/superpowers/checklists/engine_change_request.md` into a file (e.g. `ecr_<engine>.txt`):
   - `action: ADD | REMOVE | MODIFY` (exactly one)
   - `engine: <name>` (matches `_PROFILE` vocabulary)
   - For ADD: `source: new_scaffold | lab_candidate | existing_code`, `lab_dossier:` (required iff `lab_candidate`), `cadence`, `allocator`, `dispatch_order` (unique), `gate_dsr` / `gate_cred` (ONLY for `lab_candidate`, FORBIDDEN otherwise), `need:`
   - For REMOVE: `reason`, `eulogy_notes`
   - For MODIFY: `lab_dossier`, `param_change`, `gate_dsr`, `gate_cred`
2. Run: `python -m ops.engine_sdlc --ecr <path-to-file>`
3. Approve (`y`) the planner-validated diff for ADD/REMOVE.

## Pre-conditions

- For ADD `source: new_scaffold`: engine will be scaffolded from `tpcore/templates/engine_template/`. Engine dir must NOT yet exist.
- For ADD `source: existing_code`: engine code already shipped via a separate PR (the SP-F → catalyst pattern). Engine dir MUST already exist; engine package has passed `docs/superpowers/checklists/engine_readiness.md` (10 sections, non-optional; verified by the planner's `_check_readiness` AFTER the proposed `_PROFILE` edit is composed).
- For ADD `source: lab_candidate`: engine has passed `docs/superpowers/checklists/lab_candidate_readiness.md` AND the Lab dossier sidecar evidences DSR ≥ 0.95 ∧ credibility ≥ 60.
- All numeric gate evidence is **re-verified by the planner** against the cited dossier's JSON sidecar — never trusted from the ECR text.

## After

Successful ECR-ADD mutates `tpcore.engine_profile._PROFILE` and regenerates the sentinel-fenced shadows (smoke loop, `run_all_engines.sh`, etc.). NEVER hand-edit those (the 22-site-drift lesson).

## Adjacent SoT

- `.claude/rules/engine-roster.md` — the path-scoped invariant
- `.claude/rules/heavy-lane.md` — any `_PROFILE` change is heavy lane
- `.claude/skills/lab-target-run/SKILL.md` — generates the Lab dossier for `source: lab_candidate`
