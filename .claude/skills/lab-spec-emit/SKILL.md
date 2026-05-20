---
name: lab-spec-emit
description: "Slash-only wrapper for the SP-G thin advisory LLM spec-emitter — python -m ops.llm_lab_emitter --target <engine> [--reference-bundle <name>] [--intent {fold_existing|promote_new}]. Emits ONE single-hypothesis Lab candidate spec (rendered markdown + JSON sidecar) on a draft, human-merge-only PR. Spends one SP-A ledger row per emission BEFORE the draft PR is opened (orphaned-spend runbook: docs/runbooks/lab-spec-emit-orphaned-spend.md)."
disable-model-invocation: true
---

# Lab spec-emit (SP-G — thin advisory LLM spec-emitter)

Canonical CLI: `python -m ops.llm_lab_emitter --target <engine> [--reference-bundle <name>] [--intent {fold_existing|promote_new}] [--expected-trials <int>]`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs **one** SP-G emission cycle: builds an `EmissionContext` from the
roster (SP-B `lab_targetable_engines()`) + the cumulative ledger (SP-A
`cumulative_n_trials`) + the operator-named reference bundle; calls
the Anthropic SDK once; validates the response against `EmittedSpec`;
spends the SP-A ledger row; renders the SP-E / Readiness-shaped
markdown spec + the JSON sidecar; opens **one** draft, human-merge-only
PR.

The emitter is **advisory-only**. It never:

- merges a PR (draft + human-merge-only; no `--undraft` code path);
- mutates the roster (`tpcore/engine_profile.py`) or the data-feed
  binding (`tpcore/providers.py`);
- bypasses the deterministic gate (`DSR >= 0.95 AND credibility >= 60`);
- writes outside the three allow-listed paths (rendered spec, JSON
  sidecar, engine test stub) — the diff-scope fence reds the build on
  any over-broad diff;
- relaxes any clause of the Lab Candidate Readiness checklist.

## Usage

```bash
# Default (Sentinel re-tune candidate):
python -m ops.llm_lab_emitter --target sentinel

# With a curated reference bundle:
python -m ops.llm_lab_emitter --target reversion \
    --reference-bundle chan_algorithmic_trading

# Multiple bundles (comma-separated):
python -m ops.llm_lab_emitter --target vector \
    --reference-bundle carver_systematic_trading,chan_algorithmic_trading

# promote_new (new-engine candidate):
python -m ops.llm_lab_emitter --target reversion \
    --intent promote_new --expected-trials 100

# Replay (recover from orphaned spend — see runbook):
python -m ops.llm_lab_emitter --replay docs/lab/<sidecar>.json
```

## Pre-conditions

- `ANTHROPIC_API_KEY` is set in the operator's environment (the
  emitter runs CREDENTIAL-STARVED in CI; the operator command-path
  runs with the operator's credentials).
- The named `--target` engine appears in
  `tpcore.engine_profile.lab_targetable_engines()` (SP-B; LAB / PAPER
  / LIVE minus the allocator, `lab` sentinel, and `canary`). A
  category-error target is rejected pre-emission with no ledger
  spend.
- The named `--reference-bundle <name>` (if given) exists under
  `docs/lab_emitter_references/<name>.md` (the seed bundles are
  `carver_systematic_trading` and `chan_algorithmic_trading`).
- The cumulative ledger for the target has budget remaining
  (`cumulative + expected_trials <= EMISSION_QUOTA_PER_TARGET`,
  default 20 per Q2). An over-budget emission is rejected pre-LLM-
  call (no ledger spend, no Anthropic round-trip).

## The strict emission sequence (spec §3.4)

1. `ledger_gate.check_budget(target)` — reject if over-budget.
2. Build `EmissionContext` (roster + ledger + references + persona
   SHA).
3. Invoke the Anthropic SDK (no `tools`, no network beyond the SDK
   call).
4. Validate the response against `EmittedSpec` (pydantic v2 frozen +
   `extra="forbid"`).
5. `record_trial_spend(target, expected_trials, source="llm_emitter:<persona_sha>")`
   — the SP-A ledger row is written **before** the draft PR is opened.
6. Render the markdown spec; `enforce_diff_scope` against the would-be
   diff; `validate_no_gate_override` against the rendered markdown;
   `gh pr create --draft`.

If step 6 fails after step 5 succeeds, the ledger row stands — by
design (spec §3.4). See the orphaned-spend recovery runbook:
`docs/runbooks/lab-spec-emit-orphaned-spend.md`.

## After the run

The operator reviews the draft PR. The §3 byte-identical proof, §8
data prereqs, §9 lookahead honesty sections of the rendered spec
carry `[OPERATOR-DRAFT]` placeholders — these are the explicit
human-in-the-loop seams (spec §3.5). The operator:

1. Hardens the three OPERATOR-DRAFT sections.
2. Captures the characterization golden RED-first (Readiness §3 C1-C4).
3. Verifies the diff is the three-slot allow-list ONLY (the SP-G
   diff-fence is a build-time fail-loud; the operator's `git diff
   --name-only` should match).
4. Moves the PR out of draft via `gh pr ready` (the operator action;
   the LLM never does this).
5. Routes through the existing Lab pipeline: `/lab-target-run`
   skill -> `_run_lab_core` -> gate -> dossier -> `/ecr` skill.

Nothing in this skill or the SP-G agent touches the deterministic
gate, the rubric, the credibility scorer, the n_trials ledger
semantics, the readiness checklist, the ECR mechanism, the
`_PROFILE` roster, the data-feed roster, or any engine plug. The
chain is deliberately discontinuous at every gate.

## Adjacent SoT

- Spec: `docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`
- Operator decisions: spec §10 (Q1-Q6, operator-confirmed).
- Orphaned-spend runbook: `docs/runbooks/lab-spec-emit-orphaned-spend.md`.
- Lab Candidate Readiness checklist:
  `docs/superpowers/checklists/lab_candidate_readiness.md` (the gate
  the emitter respects).
- Diff-scope fence: `tpcore/lab/llm_emitter/diff_fence.py` (the
  structural enforcement of spec §4.4).
- Reference bundles: `docs/lab_emitter_references/`.
- Lab-target-run sibling skill (the dispatch the emitter does NOT
  invoke): `.claude/skills/lab-target-run/SKILL.md`.
- ECR sibling skill (the merge-time SoT mutation the emitter does NOT
  invoke): `.claude/skills/ecr/SKILL.md`.
