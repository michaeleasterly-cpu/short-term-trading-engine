# ECR `source: existing_code` — post-hoc roster registration for shipped engine code

**Status:** DESIGN + IMPLEMENTATION (bundled per the operator's lean cadence — small surgical change).
**Lane:** heavy (touches `ops/engine_sdlc/`, the engine-roster mutator).
**Date:** 2026-05-20.

## §1 Motivation

The ECR-ADD planner (`ops/engine_sdlc/planner.py`) supports two `source` values:

- **`new_scaffold`** — copies `tpcore/templates/engine_template/` into `<engine>/`, then registers in `_PROFILE`. Fails if `<engine>/` already exists on disk.
- **`lab_candidate`** — Lab-graduated; sidecar-gated; same scaffold-from-template + register flow.

**The gap.** When an engine is shipped via a separate scaffolding PR (the SP-F → catalyst pattern: PR #137 shipped `catalyst/` with operator-reviewed implementation, the ECR-ADD activation was deferred), the planner cannot register it. The `_apply_add` executor always tries to `shutil.copytree(engine_template, <engine>/)`, and rejects with *"ADD target catalyst/ already exists on disk — refusing to clobber"* (planner.py L626). The same wall is about to hit Carver (PR #149 spec + #151 plan; build PR queued).

**Why the gap exists.** Both existing source paths assume greenfield scaffolding. The "spec-first, ship-the-code-then-register-the-roster" pattern was always implicit but never had an ECR source value, so the discriminating constraint (engine dir must already exist) was never expressed.

## §2 Design

Add a third `source` value:

```
source: existing_code
```

**Semantics:** the engine code already exists on disk (shipped via a separate PR that passed engine_readiness). This ECR registers it in `_PROFILE` and regenerates the sentinel-fenced shadows — nothing else.

**Discriminating constraint (the safety property):** `existing_code` REQUIRES the engine package directory to already exist on disk. Without that constraint the new path would let someone register a phantom engine.

**LAB landing invariant:** unchanged. ADD always lands LAB regardless of source (H-S3-11a). Engine still has to graduate through the normal LAB→PAPER gate.

**Readiness check:** unchanged. `_check_readiness` runs against the existing package; if plugs are missing or malformed, the apply aborts (H-S3-11d).

**Gate fields:** REJECTED, same as `new_scaffold` — a freshly-registered engine has not earned a gate score yet. Operator-asserted gate evidence is only valid for `lab_candidate` source (which gets re-verified against the dossier JSON sidecar).

**Journal / reverse-replay:** **MUST NOT** record the engine package directory as a `sentinel_absent` move (which would cause reverse-replay to `rmtree` the existing engine code on failure). The journal records ONLY the `_PROFILE` file write; reverse-replay restores `_PROFILE` byte-identical and leaves the engine package untouched. This is the key safety distinction from `new_scaffold`.

## §3 Surface (the diff)

### `ops/engine_sdlc/ecr.py` (parser)

L44 — extend the `Literal`:

```python
source: Literal["new_scaffold", "lab_candidate", "existing_code"] | None = None
```

No other change. The parser already enforces extra="forbid" + the action-scoped field set; `existing_code` slots in with the same field rules as `new_scaffold` (gate fields rejected by the model_validator).

### `ops/engine_sdlc/planner.py` (validator + executor)

**`validate()`** (L479–513) — add the `existing_code` branch:

```python
elif ecr.source == "existing_code":
    if ecr.gate_dsr is not None or ecr.gate_cred is not None:
        return _reject(
            ecr, "existing_code ADD must NOT carry gate_dsr/gate_cred "
                 "— a freshly-registered engine has not earned a gate "
                 "score (fail-closed; same invariant as new_scaffold).")
    if repo_root is not None:
        pkg = repo_root / ecr.engine
        if not pkg.is_dir():
            return _reject(
                ecr, f"existing_code ADD requires {ecr.engine}/ to "
                     f"already exist on disk — got nothing. Use "
                     f"source: new_scaffold to scaffold from the "
                     f"template, or ship the engine code first.")
```

**`_apply_add()`** (L605–657) — branch on source:

- `new_scaffold` / `lab_candidate`: existing behavior (copy template + `record_move(sentinel_absent, pkg)`).
- `existing_code`: skip the template copy + the `sentinel_absent` journal entry. Verify `pkg.is_dir()` (defence-in-depth; validate already gated this). Still call `_check_readiness` AFTER the proposed `_PROFILE` source is composed and BEFORE the file write.

### Docs

- `docs/superpowers/checklists/engine_change_request.md` — add `existing_code` to the source enum + "When to use" line.
- `.claude/skills/ecr/SKILL.md` — update Pre-conditions to enumerate the three source values.

### Tests (`tpcore/tests/`)

In `test_engine_sdlc_planner.py`:

1. `test_add_existing_code_succeeds_when_engine_dir_present(tmp_path)` — happy path; planner validates + dry-run green; `_apply_add` does NOT call `shutil.copytree`; `_PROFILE` insert happens; LAB landing.
2. `test_add_existing_code_rejects_when_engine_dir_absent(tmp_path)` — discriminating constraint; planner rejection cites the missing dir.
3. `test_add_existing_code_rejects_gate_fields()` — same invariant as new_scaffold; gate_dsr/gate_cred → hard reject.
4. `test_add_existing_code_lands_LAB(tmp_path)` — H-S3-11a non-vacuity for the new source.
5. `test_add_existing_code_fails_readiness_when_plugs_missing(tmp_path)` — H-S3-11d still gates the new path.
6. `test_new_scaffold_still_rejects_when_dir_present(tmp_path)` — regression; the old guard is intact + its rejection message now points at `existing_code` as the right fix.

In `test_ecr_parse.py`:

7. `test_parser_accepts_existing_code_source()` — the new Literal value parses.

## §4 Out of scope (deliberate)

- **`existing_code` for MODIFY/REMOVE:** N/A. MODIFY/REMOVE always operate on engines already in `_PROFILE`; source is an ADD-only key.
- **Auto-detection ("if engine dir exists, pick existing_code"):** explicitly NOT auto-inferred. The operator declares intent; the planner enforces the declared discriminator. Auto-inferring would silently change the safety story.
- **Backward compatibility for the legacy "ADD target X/ already exists" rejection:** the rejection MESSAGE is updated to point at `existing_code`, but the rejection still fires for `new_scaffold` against an existing dir (regression-tested).
- **Touching `_classify` (planner.py L84-94):** no change. Classify only routes by action + presence-in-`_PROFILE`; source-specific gates live in `validate()` (the established pattern).

## §5 Lane discipline

Heavy lane per `.claude/rules/heavy-lane.md` (touches `ops/engine_sdlc/`). Authoritative gate: whole single-process pytest + bidirectional order-flip; `gh pr checks <n>`; gate on `statusCheckRollup` conclusion==SUCCESS.

Per the operator's lean cadence memo (`feedback_cut_process_overhead_ship`), spec+plan+build are combined into ONE PR because the change is small (~60 lines code, ~80 lines tests, ~15 lines docs). Code-quality-reviewer agent pass before merge.

## §6 Follow-up (separate PR after this lands)

`ecr_catalyst.txt` is updated to `source: existing_code` and the activation ECR re-runs. The same fix unblocks Carver's roster activation when its build PR lands.
