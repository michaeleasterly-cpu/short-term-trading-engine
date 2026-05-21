# Engine Change Request + Deterministic Lifecycle Transitions — Design (SP3)

**Status:** spec (this doc) — SP3 of the operator-approved 4-SP Engine SDLC
chain (SP1 ✅ engine SoT + clockwork → SP2 ✅ The Lab → **SP3 (this)** →
SP4 comprehensive-SDLC doc closure). Engine lane only. Brainstorm →
**spec** → expert-harden (`§Hardening` below) → plan → subagent build.

Operator directive (the epic): *"I want to snap engines in and out like
the data providers — a repeatable engine SDLC. One structured request I
fill in; the system prepares and validates the exact change and asks me
a single yes/no."*

This spec is the engine-domain analog of the **Data Provider Lifecycle**
(`docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`,
`docs/superpowers/checklists/data_feed_change_request.md`,
`tpcore/providers.py`, `tpcore/parity/`). Those files are a **READ-ONLY
symmetry reference** — adopted in *shape*, diverged wherever the engine
domain differs. They are never edited, never cloned section-for-section.

---

## 1. Problem

SP1 froze the engine SoT (`tpcore.engine_profile._PROFILE`: a frozen
`EngineProfile` per engine with `lifecycle_state ∈ {LAB, PAPER, LIVE,
RETIRED}`, `dispatch_order`, `allocator_eligible`) and the N-way
clockwork (`tpcore/tests/test_engine_lifecycle_consistency.py`). SP2
built **The Lab** (`ops.lab` / `tpcore.lab`): an isolated walk-forward
harness that produces a frozen `LabResult` + a graduation dossier with
one of two recommended exits — `promote_new` or `fold_existing` — but
**never applies it** (D-SP2-8: SP2 recommends, SP3 transitions).

What is still missing — the exact gap the operator named:

1. **There is no operator touchpoint to snap an engine in or out.**
   The Sigma archival (PR #170, commits 92fef6c → 66e2340 → 2416f21)
   was a hand-authored ~22-file cleanup that drifted across rosters,
   importers, the smoke loop, pyproject, ENGINE_TABLES, tip-sheet,
   and docs before a cohesive pass caught it. SP1's clockwork now
   *detects* a half-state, but nothing *prepares the correct change*
   for the operator. Snapping an engine out is still "remember all 22
   sites by hand" — the precise failure mode the data-lane Change
   Request was built to kill.

2. **The Lab's recommendation has no consumer.** A SURVIVED
   `LabResult` with `recommended_exit="fold_existing"` (re-tune
   reversion's params) or `"promote_new"` (graduate a new engine)
   dies in a dossier. Nothing turns it into a lifecycle transition.

3. **The SP1 clockwork's archive leg is partial.** SP1 left
   `test_retired_engine_fully_offboarded` asserting the
   `archive/<engine>/EULOGY.md` + no-package legs, but there is no
   *transition* that produces them — the consistency test can only
   *catch* a half-retirement after the fact, it cannot be the thing
   that makes a clean one. (Symmetric to the data 3-way
   `test_provider_lifecycle_consistency.py`, which is paired with the
   RETIRE checklist that *prepares* the atomic removal.)

4. **Two SP2→SP3 carry-forwards** (tracked, non-blocking, SP3-scoped):
   - **O1** — there is no per-engine `default_params()` accessor, so
     `ops/lab/run.py:_build_lab_result` honestly emits
     `ParamDelta(current=None, winning=v)` (a `# TODO(SP3)` marker is
     at `ops/lab/run.py` ~L843). The ECR's MODIFY path *needs* a real
     `current → winning` diff to validate "what changes".
   - **`LabContext.credibility_pool`** (`tpcore/lab/context.py`) is
     built + closed but never read by any SP2 caller — decide trim vs
     thread.

The class is identical to the data-side problem statement: *an
intended state change to a first-class entity, applied ad hoc, leaving
half-state*. The data lane solved it with a structured Change Request +
a deterministic gate router + an N-way consistency test. SP3 builds the
engine-domain analog, diverging wherever engines differ from feeds.

### 1.1 Non-goals (SP4 / out of scope)

- SP4 owns the **comprehensive Engine SDLC spec** and the doc closure
  (CLAUDE.md / OPERATIONS.md / glossary updates, non-Python shadow
  regeneration prose). SP3 must **not** do SP4's documentation work.
  SP3 ships exactly: the ECR mechanism, the ADD/REMOVE/MODIFY
  transition executor, the archive-leg consistency completion, and the
  two carry-forwards (O1 + credibility_pool).
- No **CUTOVER analogue**. The data lane's CUTOVER (swap the provider
  behind a feed, consumers unchanged) has **no engine counterpart**:
  an engine *is* its strategy; there is no "same engine, different
  implementation behind a stable interface" operation. CUTOVER is
  explicitly **not ported** (see §9 divergence ledger).
- No new engine is built or graduated by this work. SP3 is the
  *mechanism*; the operator drives it with a filled ECR later.
- No live trading is enabled. The paper-only mandate stands;
  `LifecycleState.LIVE` remains reserved and unreached.
- No ML / no probabilistic routing. Every transition is a
  deterministic function of the ECR + the SoT + (for ADD/MODIFY) a
  Lab/gate verdict.

---

## 2. The Engine Change Request (ECR) as a first-class artifact

### 2.1 Format decision — markdown checklist *fed to* a strict parser

The data lane's `data_feed_change_request.md` is a copy/fill markdown
block the operator feeds in; the system parses, routes, prepares +
validates the exact diff, and returns a binary y/n. **SP3 adopts that
exact shape**, with one deliberate divergence forced by the operator
directive: the data lane is *checklist-only* (no programmatic parser
module exists for it — verified: no `ChangeRequest` parser in
`ops/`/`tpcore/`; the data CR is processed by an operator/agent
following the markdown). SP3's operator scope explicitly requires a
**planner/executor** on top of the `engine_profile` SoT, so the ECR is:

- **A markdown checklist** —
  `docs/superpowers/checklists/engine_change_request.md` — the single
  copy/fill block the operator authors and feeds in (the human
  touchpoint, symmetric to `data_feed_change_request.md`).
- **Parsed by a strict deterministic parser** into a frozen pydantic-v2
  `EngineChangeRequest` model. The fenced ` ```ECR … ``` ` block in the
  markdown is the wire format; the parser is the single entry point
  (`ops.engine_sdlc.ecr.parse_ecr(text) -> EngineChangeRequest`). A
  request that does not parse is **rejected with the exact reason** —
  never best-effort-interpreted.

Rationale: the markdown keeps the operator touchpoint identical in
*feel* to the data lane (one structured block, no hand-editing
registries). The strict parser + frozen model gives SP3 the
programmatic planner/executor the operator asked for, with a typed,
test-pinnable contract — consistent with every other engine SoT
(`EngineProfile`, `LabResult`, `LabCandidate` are all frozen pydantic
v2, `extra="forbid"`).

### 2.2 The ECR wire block

````
ECR
action:        ADD | REMOVE | MODIFY        # exactly one
engine:        <engine name>                # _PROFILE key vocabulary
# ── ADD only (onboard / graduate) ─────────────────────────────────
source:        new_scaffold | lab_candidate # brand-new vs Lab-graduated
lab_dossier:   <path under docs/lab/…>      # required iff source=lab_candidate
cadence:       daily | weekly_first_trading_day | monthly_first_trading_day
allocator:     true | false                 # allocator_eligible
dispatch_order: <int>                        # unique among non-RETIRED
gate_dsr:      <float ≥ 0.95>               # evidence: the held-back DSR
gate_cred:     <int ≥ 60>                   # evidence: the credibility score
need:          <one line: the edge / why this engine exists>
# ── REMOVE only (retire / archive) ────────────────────────────────
reason:        <one line: why it is retired (cause of death)>
eulogy_notes:  <free text → seeds the EULOGY template>
# ── MODIFY only (re-tuned params on an existing engine) ───────────
lab_dossier:   <path under docs/lab/…>      # the SURVIVED fold_existing dossier
param_change:  <key>=<value>[, <key>=<value> …]   # the winning diff
gate_dsr:      <float ≥ 0.95>
gate_cred:     <int ≥ 60>
````

`action` selects exactly one block; fields outside the selected block
are rejected (`extra="forbid"` semantics enforced by the parser, not
just the model — unknown keys are an error, not ignored). All numeric
gate evidence is **re-verified by the planner against the cited Lab
dossier / SoT — never trusted from the ECR text** (§5.4).

### 2.3 The frozen model (`tpcore.engine_sdlc` is **not** where this lives — see §3)

```python
class ECRAction(StrEnum):
    ADD = "add"; REMOVE = "remove"; MODIFY = "modify"

class EngineChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ECRAction
    engine: str
    # ADD
    source: Literal["new_scaffold", "lab_candidate"] | None = None
    lab_dossier: str | None = None          # ADD(lab_candidate) | MODIFY
    cadence: Cadence | None = None          # ADD
    allocator: bool | None = None           # ADD
    dispatch_order: int | None = None       # ADD
    gate_dsr: float | None = None           # ADD | MODIFY
    gate_cred: int | None = None            # ADD | MODIFY
    need: str | None = None                 # ADD
    # REMOVE
    reason: str | None = None               # REMOVE
    eulogy_notes: str | None = None         # REMOVE
    # MODIFY
    param_change: dict[str, Any] | None = None  # MODIFY
```

(`Cadence` is reused from `tpcore.engine_profile` — no new enum.) A
model validator enforces "exactly the selected action's fields are
present and non-None; the others are None" so a malformed ECR fails at
construction, before the planner runs.

---

## 3. Architecture & module layering

### 3.1 Where the planner/executor lives — `ops/engine_sdlc/`

The SP2 H-S2-1 precedent is authoritative: **pure SoT/contract is
tpcore; engine-touching orchestration is ops** (the tpcore∌engine
layering invariant, commit 4e73fe8, enforced by
`tpcore.scripts.check_imports`). Mapping that to SP3:

| Component | Layer | Why |
|---|---|---|
| `EngineChangeRequest` model + `parse_ecr` | **`ops/engine_sdlc/ecr.py`** | The model is a contract, but the parser validates against the *Lab dossier filesystem* + cites engine packages; the planner that consumes it touches engines. Keeping the ECR types next to the planner avoids a tpcore type that only ops uses (the data lane's `ProviderBinding` lives in tpcore because the *ingest runtime* reads it; nothing in the engine *runtime* reads an ECR — it is a one-shot ops tool). |
| The lifecycle-transition planner/executor | **`ops/engine_sdlc/planner.py`** | It reads `tpcore.engine_profile._PROFILE`, *rewrites the `_PROFILE` source*, moves engine packages, scaffolds from `tpcore/templates/engine_template/`, runs the engine-readiness gate, reads Lab dossiers — every one of these is engine-touching orchestration. **Illegal in tpcore.** Mirrors `ops.lab.run` (imports engine packages, legal only in `ops/`) and `ops.engine_ladder` (engine-lane orchestration in ops). |
| `default_params()` accessor seam (O1) | **per-engine `<engine>/backtest.py`**, surfaced via a thin `ops/engine_sdlc/default_params.py` dispatcher | Each engine *owns* its defaults (parity with the existing `_runner_for`/`_context_runner_for` lazy per-engine dispatch in `ops/lab/run.py`). tpcore must never import an engine, so the dispatcher is in ops. See §7. |
| The CLI | **`ops/engine_sdlc/__main__.py`** (`python -m ops.engine_sdlc`) | Symmetric to `python -m ops.lab` — a separate OS process, operator-driven, never wired into any daemon/dispatch. |
| The completed archive-leg consistency test | **`tpcore/tests/test_engine_lifecycle_consistency.py`** (extend the existing file) | The clockwork is already there and is tpcore-layer (it imports only `tpcore.engine_profile` + stdlib + filesystem). SP3 *completes* the existing partial archive leg — it does not move it. |

The **SoT itself (`tpcore/engine_profile.py`) stays in tpcore,
unchanged structurally** — SP3 only *edits the `_PROFILE` dict literal's
text* via the planner (an AST/source rewrite of one file), exactly as a
human would, then re-runs the clockwork. No new tpcore module, no new
tpcore→engine import. CLAUDE.md "never modify tpcore without checking
all engines" applies only if SP3 changed tpcore *code*; it changes only
the `_PROFILE` *data literal* (the same edit Sigma's archival made by
hand) and **adds zero tpcore imports of engines**.

### 3.2 The transition pipeline (deterministic, single shape for all three actions)

```
parse_ecr(markdown) ──▶ EngineChangeRequest          (reject on parse error)
        │
        ▼
  classify(ecr) ──▶ TransitionPlan                    (the deterministic state machine, §4)
        │            { from_state, to_state, sot_diff, fs_ops, gate_checks,
        │              approval_class, rejection: str | None }
        ▼
  validate(plan)                                       (§5.4 — re-verify ALL evidence;
        │                                               run the dry consistency check;
        │                                               reject, never "force")
        ▼
  ┌─ approval_class == AUTOMATED ──▶ apply(plan) ──▶ receipt + audit event
  │
  └─ approval_class == OPERATOR  ──▶ render the prepared diff + the green
                                      validation result ──▶ binary  APPROVE? (y/n)
                                       │
                                       ├─ y ──▶ apply(plan) ──▶ receipt + audit event
                                       └─ n ──▶ abort, nothing mutated
```

`apply(plan)` is the **only** mutating step and is **atomic-or-abort**:
it stages every SoT edit + filesystem op into the working tree, then
runs the full `test_engine_lifecycle_consistency.py` suite **as a
fresh subprocess** against the now-staged on-disk tree (H-S3-1: a
subprocess with `cwd`=the staged tree, NOT in-process — the public
accessors and the consistency test bind module-global `_PROFILE`/
`REPO` at import, so an in-process or dict-injected run would validate
a *different* code path than CI; H-S3-4 journals every touched file's
pre-state first). The staged changes remain on disk **only if the
subprocess exits 0**; a non-zero exit ⇒ reverse-order restore from the
journal to byte-identical pre-state, emit the rejection, mutate
nothing. (Symmetric to the data lane's "the prepared diff must pass
every invariant test *before* you are asked to approve.") Git is
**not** touched by the executor — it stages working-tree changes only;
committing is the operator's separate, normal step (honors the
standing "tests never touch the real repo" / executor never runs git
rule).

### 3.3 Audit + state-comprehension

Every ECR outcome (applied, rejected, operator-declined) emits a
`platform.application_log` event `ENGINE_CHANGE_REQUEST` with the
action, engine, from/to state, and the rejection reason if any —
symmetric to the data lane's audit-event + weekly-digest discipline.
**SP3 emits the event; it does not build a new digest** (the engine
weekly-digest surface is the engine-ladder/SP4 territory — SP3 stays in
scope by emitting the canonical event the existing bus already carries,
not by adding a reporting lane).

---

## 4. The three transitions as deterministic state machines

`LifecycleState` (frozen, SP1): `LAB → PAPER → LIVE → RETIRED`. SP3
defines exactly which ECR action drives which edge. **No edge is
inferred; an ECR that does not map to a defined edge is rejected.**

### 4.1 ADD — onboard / graduate (operator-approved)

Two sub-cases by `source`:

**`source=new_scaffold`** — a brand-new engine from
`tpcore/templates/engine_template/`. There is no Lab dossier yet (a new
engine has no backtest history). The new engine enters at
`LifecycleState.LAB` — **ADD never lands a new engine straight into
PAPER**. Rationale: a new engine must earn PAPER through the Lab +
gate, exactly as every existing engine did; ADD(new_scaffold) only
*creates the scaffold + the `_PROFILE` LAB entry*, it does not graduate.

- `from_state`: none (engine absent from `_PROFILE`).
- `to_state`: `LAB`.
- `fs_ops`: scaffold `<engine>/` from `tpcore/templates/engine_template/`
  (the readiness checklist's documented start point); the engine-
  readiness gate (`docs/superpowers/checklists/engine_readiness.md`) is
  invoked as the **ADD build gate** — its programmatically-checkable
  sections (package present, 5 plugs subclass `BaseEnginePlug`,
  `<engine>/tests/` present, importable `<engine>.scheduler`) are run;
  a failing readiness check rejects the ECR.
- `sot_diff`: add an `EngineProfile(engine=…, cadence=…, dispatch_order=…,
  lifecycle_state=LAB, allocator_eligible=False)` row to `_PROFILE`
  (LAB ⇒ `allocator_eligible` is forced False — SP1 invariant
  `test_no_half_state`).
- `gate_checks`: none required for `new_scaffold` (no history); the
  `gate_dsr`/`gate_cred` ECR fields are **rejected as not-applicable**
  for `new_scaffold` (a new engine cannot present a gate score it has
  not earned — a deliberate fail-closed).
- **Approval: OPERATOR** (engine existence is structural — §6).

**`source=lab_candidate`** — graduate a Lab-proven candidate. The cited
`lab_dossier` must be a SURVIVED `LabResult` with
`recommended_exit="promote_new"` (the planner re-reads/re-parses the
dossier and re-verifies DSR ≥ 0.95 ∧ credibility ≥ 60 ∧
`recommended_exit=="promote_new"` against the dossier — never the ECR
text). The candidate is scaffolded to a real engine package and lands
at **`LAB`** (it must still go through paper-trading observation before
PAPER; the Lab proves the *backtest* edge, not live behaviour). The
ADD here is "create the durable engine package + LAB `_PROFILE` row
backed by a SURVIVED dossier".
  - `recommended_exit="fold_existing"` is **not** an ADD — it is a
    MODIFY of the `target_engine` (§4.3). The planner rejects an
    ADD whose dossier says `fold_existing` with that exact guidance.
- **Approval: OPERATOR.**

> **LAB → PAPER promotion is automated/deterministic, NOT an ECR
> action.** Once an engine is a wired LAB package with a SURVIVED
> dossier on record, the LAB→PAPER edge is a gated, deterministic
> transition (the gate is the existing capital-gate / `graduation_ready`
> authority, not a new judgement). SP3 defines this edge as
> **automated** (the §6 policy: intermediate gated transitions need no
> approval). It is exposed as `python -m ops.engine_sdlc promote
> --engine <e>` which runs the gate and flips `LAB→PAPER` *iff* the
> gate is green — no ECR, no y/n. This is the precise engine analog of
> the data lane's "EVALUATE/CUTOVER are automated; only ADD/REMOVE are
> operator-gated". (Operator-gated *existence*; automated *promotion*.)

### 4.2 REMOVE — retire / archive (operator-approved) — the formalized snap-out

The Sigma worked example, made deterministic and checklisted. A
`PAPER`/`LIVE` engine (or a `LAB` scaffold being abandoned) →
`RETIRED`, fully offboarded in one atomic change.

- `from_state`: `PAPER` | `LIVE` | `LAB`.
- `to_state`: `RETIRED`.
- `sot_diff`: flip `_PROFILE["<engine>"].lifecycle_state` to `RETIRED`
  and force `allocator_eligible=False` (SP1 `test_no_half_state`).
  Because `ROSTER = roster_for_dispatch()` and
  `allocator_eligible_engines()` / `engine_package_names()` are **all
  SoT-derived** (verified: `ops/engine_dispatch.py:30
  ROSTER = roster_for_dispatch()`), this single literal flip
  automatically delists the engine from dispatch, the allocator pool,
  and the check_imports package set — **snap-out is a SoT edit, not
  bespoke per-site wiring** (SP1's design goal, here proven and
  consumed).
- `fs_ops` (the deterministic archive checklist — derived from the
  Sigma retirement checklist in `archive/sigma/EULOGY.md`):
  1. `git mv`-equivalent **working-tree move** `<engine>/` →
     `archive/<engine>/` (the executor uses `shutil`/`pathlib` moves,
     not git — §3.2; the move shows up as a normal working-tree change
     the operator commits).
  2. Move the engine's wrapper scripts/CSVs that live by name (e.g.
     `scripts/run_<engine>_search.sh` if present) alongside, mirroring
     the Sigma `{scripts => archive}/run_sigma_search.sh` move.
  3. Generate `archive/<engine>/EULOGY.md` from a **template**
     (`tpcore/templates/eulogy_template.md`, new — §4.2.1) seeded with
     the ECR's `reason` + `eulogy_notes` + the last on-record
     credibility/DSR (read from `platform.data_quality_log` /
     `backtest_credibility.<engine>` if present, else "no surviving
     gate record"). The template encodes the Sigma EULOGY's proven
     section structure (Cause of death / What it leaves behind /
     Retirement checklist) — *structure reused, content engine-
     specific*.
  4. Update the **structural shadows** the SP1 clockwork pins so they
     stay green: `scripts/run_smoke_test.sh` step-3 loop and
     `pyproject.toml` `testpaths` + `packages.find.include` (these are
     the *only* non-SoT-derived sites — everything else derives from
     `_PROFILE`). The executor edits exactly these, then the
     `test_structurally_parseable_shadows_match_sot` clockwork proves
     it.
  5. Remove the engine's `ENGINE_TABLES` entry if present (a documented
     SP1 seam D-SDLC1-1; the clockwork
     `test_engine_tables_keys_are_known_engines` then proves no orphan
     key remains).
- `gate_checks`: none (REMOVE has no edge that needs a gate — you
  always may stop trading something).
- **Approval: OPERATOR** (engine existence is structural — §6).
- After `apply`, the **completed archive-leg clockwork** (§4.2.2)
  proves the offboarding is whole; a partial REMOVE fails CI exactly
  as a half-retired data feed fails `test_provider_lifecycle_
  consistency.py`.

> **Note on the non-SoT shadow edits in fs_op 4.** These are the small,
> bounded set the SP1 clockwork already enumerates as "structurally-
> parseable shadows". The planner edits *only* those two files'
> *enumerated regions* (the `for engine in …; do` token list; the
> testpaths/include arrays) — it does not free-edit prose. Doc prose
> (CLAUDE.md/glossary) is **SP4's closure**, not SP3's — SP3 produces a
> clean, CI-green offboarding; SP4 narrates it.

#### 4.2.1 The EULOGY template (new)

`tpcore/templates/eulogy_template.md` — a copy/seed template (lives in
templates alongside `engine_template/`, the documented scaffold home).
It encodes the *structure* validated by `archive/sigma/EULOGY.md`
(title + date; "Cause of death" with the gate evidence; "What it leaves
behind (still in tpcore)"; "Retirement checklist (all done <date>)").
The planner fills the placeholders deterministically from the ECR + the
on-record gate data. **It is a template, not a clone of Sigma's
content** — Sigma's eulogy stays the worked example, untouched.

#### 4.2.2 Completing the consistency-test archive leg (SP3 scope item 4)

SP1 left `test_retired_engine_fully_offboarded` asserting:
RETIRED ⇒ not in roster, not allocator-eligible, in `archived_engines()`,
`archive/<engine>/EULOGY.md` exists, no top-level `<engine>/` package.
That is the *detector*. SP3 **completes the leg** by adding the two
checks SP1 explicitly deferred, so a partial REMOVE fails CI symmetric
to the data 3-way half-retirement test:

- **EULOGY content floor:** `archive/<engine>/EULOGY.md` must contain
  the template's required section headers (a non-empty
  "## Cause of death" and a "## Retirement checklist" section) — a
  zero-byte or stub EULOGY (the data-lane "fake-healable HealSpec"
  failure-class analog) fails CI. This makes the EULOGY a *real*
  artifact, not a touched file.
- **Shadow-purge completeness:** assert the retired engine's name no
  longer appears in the `run_smoke_test.sh` step-3 loop **nor** in
  `pyproject.toml` testpaths/include (the existing
  `test_structurally_parseable_shadows_match_sot` proves the *live*
  set matches; SP3 adds the explicit *RETIRED-absent* assertion so a
  retire that forgot a shadow fails on the retire leg, not only
  indirectly).

These extend the **existing** tpcore test file (no new test module,
no tpcore→engine import — the file already imports only
`tpcore.engine_profile` + stdlib + filesystem).

### 4.3 MODIFY — re-tuned params on an existing engine

A SURVIVED Lab `fold_existing` dossier says "apply this param diff to
`reversion`". MODIFY is the transition that consumes it.

- `from_state` == `to_state` (no lifecycle edge — the engine stays
  PAPER/LIVE/LAB; only its parameters change).
- `sot_diff`: **none in `_PROFILE`** — engine params are *not* in the
  lifecycle SoT (they live in the engine's `backtest.py` /
  `models.py` defaults). MODIFY's diff is to the **engine's
  `default_params()` source** (the O1 seam — §7), validated `current →
  winning` against the cited dossier.
- `gate_checks`: the planner re-reads the cited `lab_dossier`,
  re-verifies it is SURVIVED with `recommended_exit="fold_existing"`,
  `target_engine == ecr.engine`, and that every key in
  `ecr.param_change` matches the dossier's winning params **and** is a
  declared sampled param for that engine in `ops.lab.run.PARAM_RANGES`
  (no smuggling a param the Lab never swept). Mismatch ⇒ reject.
- **Approval class — the expert call (see §6.2): MODIFY is
  AUTOMATED-if-gated, NOT operator-approved.**

---

## 5. The planner/executor — detailed behaviour

### 5.1 `classify(ecr) -> TransitionPlan`

Pure function: maps `(action, current _PROFILE state, source)` to the
single defined edge (§4) or a `rejection`. The classification table is
**total and closed** — any `(action, from_state)` not in the table is a
typed rejection, never an inferred edge:

| action | engine in `_PROFILE`? | from_state | source | → to_state | approval |
|---|---|---|---|---|---|
| ADD | no | — | new_scaffold | LAB | OPERATOR |
| ADD | no | — | lab_candidate | LAB | OPERATOR |
| ADD | yes | * | * | **reject** (already exists; use MODIFY/REMOVE) | — |
| REMOVE | yes | PAPER\|LIVE\|LAB | — | RETIRED | OPERATOR |
| REMOVE | yes | RETIRED | — | **reject** (already retired) | — |
| REMOVE | no | — | — | **reject** (nothing to remove) | — |
| MODIFY | yes | PAPER\|LIVE\|LAB | — | (unchanged) | AUTOMATED |
| MODIFY | yes | RETIRED | — | **reject** (cannot tune a retired engine) | — |
| MODIFY | no | — | — | **reject** (nothing to modify) | — |

`allocator`, `reversion`, `vector`, `momentum`, `sentinel`, `canary`
SP1 invariants are preserved by construction: the classifier never
proposes a `dispatch_order` collision (it validates uniqueness against
the staged `_PROFILE` before producing the plan), never sets
`allocator_eligible=True` on a non-PAPER/LIVE engine, and the
`canary`-never-graduates and allocator-separate-path invariants are
untouched (SP3 adds no edge that could violate them; a REMOVE of
`canary` is *allowed* by the table but, like any REMOVE, is operator-
gated and CI-proven — there is no SP3 reason to special-case it).

### 5.2 `validate(plan)` — reject, never force

Mirrors the data lane's "a request that cannot produce a consistent
diff is rejected with the reason — never handed to you to force":

1. **Evidence re-verification** (§5.4): every gate number is recomputed
   from the frozen `LabResult` JSON sidecar (H-S3-9) / on-record data,
   not read from the ECR and **not** scraped from rendered markdown.
2. **Dry consistency run (H-S3-1 — the corrected mechanism):**
   `shutil.copytree` the worktree (minus `.git`/`.venv`/`__pycache__`/
   `backtests`) into an isolated temp tree, stage the *proposed*
   `_PROFILE` source + filesystem ops into **that** copy, then run
   `python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py
   -q` **as a fresh subprocess with `cwd`=the temp tree** (so its
   `import tpcore.engine_profile`, its `_PROFILE`, and its
   module-constant `REPO` are all the *proposed* ones — zero
   in-process state bleed). The dry run passes iff that subprocess
   exits 0. The `_roster_sorted(profiles=…)` injection seam is **NOT**
   the dry-run mechanism (it is insufficient — the public accessors and
   the consistency test bind module-global `_PROFILE`/`REPO`; see
   H-S3-1/D2); it is retained only as a fast `_roster_sorted` unit-test
   convenience.
3. **Readiness gate** (ADD only): run the programmatically-checkable
   `engine_readiness.md` items against the scaffolded package.
4. Any failure ⇒ `plan.rejection` is set; the CLI prints the exact
   reason and exits non-zero. **Nothing is mutated.**

### 5.3 `apply(plan)` — atomic-or-abort

Journal the exact pre-state (bytes-or-absent) of every file the plan
will touch + every dir-move source/dest (H-S3-4); stage all edits (the
AST-validated `_PROFILE` source rewrite, the shadow-file region edits,
the EULOGY render — text edits first; the package `shutil.move` **last**,
the irreversible-ish op after the cheap-to-revert ones) into the
working tree; re-run the full consistency suite **as a fresh
subprocess** against the now-on-disk staged state (H-S3-1 — not
in-process); if it exits 0, leave it (the operator commits); on
non-zero **or any exception**, **restore every journaled file to its
exact prior bytes and every dir move to its origin, in reverse order**,
then emit the rejection; a restore failure escalates loudly
(`ENGINE_CHANGE_REQUEST` `outcome=apply_restore_failed`). No partial
application is ever left on disk.

### 5.4 Evidence is re-verified, never trusted

The ECR carries `gate_dsr`/`gate_cred`/`param_change` for *operator
legibility* (so the human sees what they are approving), but the
planner **recomputes every one** from the authoritative source:

- ADD(lab_candidate) / MODIFY: the planner re-reads the **frozen
  `LabResult` JSON sidecar** (`<dossier>.json`, model-validated back
  into the `LabResult` pydantic model — `extra="forbid"`, so a
  tampered/extra field is a hard reject), **not** the rendered
  `.md` prose. The markdown is a human rendering; re-scraping rendered
  markdown for the load-bearing automated-MODIFY gate would couple the
  zero-trust check to the dossier *template's* formatting and is
  rejected as a fragile coupling (design-defect fix H-S3-9: SP2's
  `write_lab_dossier` currently writes *only* the `.md`; SP3 makes it
  additionally emit the byte-stable `LabResult.model_dump_json()`
  sidecar — an additive, behaviour-preserving change to
  `ops/lab/dossier.py`, in SP3 scope as the SP2 carry-forward that
  makes the evidence machine-verifiable). Re-verify, against the
  parsed `LabResult`: `verdict == "SURVIVED"`, `dsr ≥ 0.95`,
  `credibility_score ≥ 60`, `recommended_exit` matches the action
  (`promote_new` for ADD(lab_candidate), `fold_existing` for MODIFY),
  `target_engine == ecr.engine`. A sidecar whose numbers disagree
  with the ECR text ⇒ **reject for evidence mismatch** (a
  forged/stale ECR cannot get past the gate). The sidecar must also
  be **identity-fresh**: its `candidate`/`target_engine`/`seed` and
  the `winning_params` keyset must match the ECR's cited dossier path
  + (MODIFY) the `param_change` keyset exactly — a sidecar for a
  *different* run or a stale path is a hard reject.
- The Lab credibility namespace invariant is honored: the planner
  reads `backtest_credibility.<engine>` (live namespace) for the
  EULOGY's "last on-record gate" and `lab.<candidate>` only via the
  dossier — it never promotes a `lab.<candidate>` score into a live
  decision (SP2 H-S2-3 preserved).

---

## 6. Operator-interaction policy (AUTHORITATIVE — the §10 engine analog)

This section is the engine-domain authority, the analog of the data
lane's §10. It supersedes any looser wording elsewhere in SP1–SP3.

**Operator approval is required for exactly two operations: ADD (a new
or Lab-graduated engine — "does this strategy exist on the platform at
all") and REMOVE (retire/archive — "stop trading this strategy
forever"). Everything else is automated, deterministic, gated, with no
operator approval.**

| operation | operator y/n? | what the operator sees |
|---|---|---|
| **ADD** (new_scaffold or lab_candidate) | **YES** | the prepared diff (`_PROFILE` row + scaffold/move + shadow edits) **+** a green dry consistency run **+** the re-verified gate evidence → `APPROVE? (y/n)` |
| **REMOVE** (retire/archive) | **YES** | the prepared diff (the `_PROFILE` flip + the exact archive move list + the rendered EULOGY + shadow purge) **+** a green dry consistency run → `APPROVE? (y/n)` |
| **MODIFY** (re-tuned params, gated) | **NO — automated** | a done-receipt: the validated `current → winning` diff + the dossier reference + the `ENGINE_CHANGE_REQUEST` audit event |
| **LAB → PAPER promotion** (gated) | **NO — automated** | a done-receipt (`python -m ops.engine_sdlc promote`); flips iff the capital-gate/`graduation_ready` authority is green |
| backtest re-gate / consistency dry-run | **NO — automated** | nothing (internal) |

The operator **never hand-edits `_PROFILE`, the shadows, or an
EULOGY** — exactly the data-lane discipline ("you do not hand-edit
`_BINDINGS`… that is exactly how the system gets broken"; the Sigma
22-site drift is the engine-side proof). The single touchpoint is the
filled ECR; the system prepares + validates + asks one binary question.

### 6.1 Why ADD/REMOVE are the operator-gated pair (symmetry-justified)

Identical reasoning to data §10: minimizing operator interaction is not
the goal; minimizing *opportunity for irreversible harm* is. The two
genuinely structural decisions — *whether a strategy trades real
capital at all* — are ADD and REMOVE. Everything reversible and
gate-verified (a param tune that already passed DSR/credibility; a
LAB→PAPER promotion the gate already cleared) is automated, with the
gate supplying the human-equivalent judgement.

### 6.2 The MODIFY approval-class decision (the expert call — justified)

**Decision: MODIFY is AUTOMATED-if-it-passes-the-gate, NOT
operator-approved like ADD/REMOVE.**

The dispatch brief flagged this as a genuine expert call ("a MODIFY
that changes live-traded params trades real capital — is it ADD/REMOVE-
class or automated-if-gated?"). Reasoning, weighed against the data §10
shape *and* engine risk reality:

1. **The data-lane structural analogy is exact.** Data §10's
   operator-gated pair is *feed existence* (ADD/REMOVE); its automated
   class is the *reversible, parity-gated* change (CUTOVER —
   "the parity gate already supplied the human-equivalent judgement").
   A MODIFY here is precisely that: a *reversible* parameter change
   whose human-equivalent judgement was **already supplied by the
   DSR ≥ 0.95 ∧ credibility ≥ 60 gate** that the Lab `fold_existing`
   dossier had to pass to even *be* a valid MODIFY input. Treating
   MODIFY as operator-gated would be the engine-side version of the
   *reverted* "CUTOVER is operator-confirmed" wording the data spec
   explicitly corrected.
2. **Engine risk reality does not push the other way.** A MODIFY only
   re-tunes parameters of an *already-PAPER/LIVE* engine that already
   trades; it cannot create or destroy a strategy's existence. The
   blast radius is bounded by the same RiskGovernor / per-engine
   `RiskLimits` / capital gate that bound the engine's *current*
   params — a re-tuned reversion is still reversion under the same
   risk envelope. The irreversible-harm axis (does this strategy
   exist) is unchanged by a MODIFY.
3. **The gate has provable teeth.** Sigma is the worked proof that the
   DSR/credibility gate *refuses to graduate an unearned strategy*.
   The same gate guards a MODIFY: a param change that did not SURVIVE
   the Lab cannot become a valid MODIFY ECR (the planner rejects it at
   §5.2 evidence re-verification). Operator approval would add
   ceremony without adding a check the gate does not already enforce.
4. **Paper-only mandate is the backstop.** No engine is LIVE; a MODIFY
   today changes paper behaviour only. Even when LIVE is eventually
   reached, the gate + RiskGovernor envelope is the control, not a
   y/n on a number the operator cannot independently re-derive.

**Guardrail attached to the decision (fail-closed):** because MODIFY is
automated, the planner's evidence re-verification (§5.4) is the *only*
thing standing between a dossier and live params — so it is
**zero-trust**: the dossier is re-parsed and every gate number
recomputed; `target_engine`/`recommended_exit`/param-key membership in
`PARAM_RANGES` are all re-checked; any mismatch is a hard reject. The
automated MODIFY is logged to the audit bus (`ENGINE_CHANGE_REQUEST`)
so it is visible in state-comprehension exactly like an automated data
CUTOVER. *(Operator override note: if the operator ever wants a
specific MODIFY held for y/n, that is a one-line policy escalation, not
a redesign — but the default, justified above, is automated.)*

---

## 7. The O1 `default_params()` seam (carry-forward) + credibility_pool

### 7.1 O1 — per-engine `default_params()` accessor

**Problem:** `ops/lab/run.py:_build_lab_result` emits
`ParamDelta(name=k, current=None, winning=v)` with a `# TODO(SP3)`
because no engine exposes its live defaults. The ECR MODIFY path needs
a real `current → winning` diff to validate and to show the operator.

**Seam design (minimal, parity-correct, tpcore∌engine-safe):**

- Each engine's `<engine>/backtest.py` already has the param defaults
  baked into its `run_*_with_context(context, overrides=…)` signature /
  `models.py`. Add a module-level **pure** function
  `default_params() -> dict[str, Any]` to each engine's `backtest.py`
  that returns the engine's current default values for **exactly the
  keys that engine declares in `ops.lab.run.PARAM_RANGES`** (the
  closed set the Lab can sweep — so the diff is always well-defined and
  symmetric with the search space). This is the engine-plug-parity
  shape: one accessor, same name, every engine (reversion/vector/
  momentum — the three with `PARAM_RANGES` entries; sentinel/canary
  have no search space so no accessor, which a coverage test asserts).
- A thin dispatcher `ops/engine_sdlc/default_params.py` does the
  **lazy per-engine import** (exact parity with
  `ops/lab/run.py:_runner_for` / `_context_runner_for` — the proven
  legal-in-ops engine-dispatch pattern; **never** a tpcore import of
  an engine).
- `ops/lab/run.py:_build_lab_result` is updated to call the dispatcher
  and emit `ParamDelta(current=default_params()[k], winning=v)` — the
  `# TODO(SP3)` is removed, the honest `None` placeholder becomes the
  real value. This is an additive change to one ops function; the SP2
  T1 characterization oracle pins `amain`'s rc + the
  `write_credibility_score` call args (not the `param_diff` contents),
  so this change does not disturb the oracle.
- **CLAUDE.md "never modify tpcore without checking all engines" does
  not trigger:** the accessor is added to *engine* `backtest.py`
  files, not tpcore; no tpcore module changes; no tpcore→engine
  import is added. A parity test (`ops/lab/tests/` or
  `tpcore/tests/test_engine_lifecycle_consistency.py` extension —
  decided at plan time, default the former to keep tpcore engine-free)
  asserts every `PARAM_RANGES` engine exposes `default_params()` whose
  key set **==** that engine's `PARAM_RANGES` key set (a new searched
  param without a default fails CI — the same "cannot be forgotten"
  clockwork discipline as the HealSpec coverage test).

### 7.2 `LabContext.credibility_pool` — decision: **thread it, do not trim**

`LabContext.credibility_pool` (the single allowlisted RW handle) is
built + closed but never *read* by an SP2 caller because
`ops/lab/run.py:_run_lab_core` creates its **own** short-lived
`asyncpg.create_pool` for the `write_credibility_score` call (L745)
*inside* the active `LabContext`. That is a latent inconsistency: SP2's
isolation contract designates `credibility_pool` as *the* one RW seam,
yet the actual write bypasses it with an ad-hoc pool.

**Decision: thread it (minimal correct), do not trim.** Trimming would
delete the very seam SP2's isolation design declares canonical and
leave the ad-hoc pool as the de-facto (undocumented) RW path — the
opposite of the contract. The minimal correct change: when a
`LabContext` is active, `_run_lab_core` uses the context's
`credibility_pool` for the `write_credibility_score` call instead of
creating its own; the legacy `python scripts/search_parameters.py`
path (no `LabContext`, `candidate is None`) keeps its own pool exactly
as today (byte-identical legacy contract — the SP2 oracle pins it).
This is a ~5-line, behavior-preserving change that makes the isolation
contract *true* rather than aspirational, and removes a second RW
pool opened inside an isolation boundary. (If plan-time review finds
threading materially riskier than documented-trim, the fallback is to
**document** `credibility_pool` as reserved-for-future + add a test
asserting it is the only RW handle — but thread is the default; trim-
to-nothing is rejected as it weakens the contract.)

---

## 8. Reused vs new (search-then-extend ledger)

| Concern | Reused (compose) | New (SP3) |
|---|---|---|
| Lifecycle SoT + states | `tpcore.engine_profile._PROFILE`, `LifecycleState`, `Cadence` (the `_roster_sorted(profiles=…)` arg is a `_roster_sorted` unit-test convenience only — **not** the dry-run mechanism; H-S3-1/D2) | — (SP3 only *edits the literal*; the dry run is an isolated-temp-tree subprocess) |
| SoT-derived shadows | `roster_for_dispatch()`, `allocator_eligible_engines()`, `archived_engines()`, `engine_package_names()` — all auto-derive | — (proven: snap-out = one literal flip) |
| Consistency clockwork | `tpcore/tests/test_engine_lifecycle_consistency.py` (all SP1 legs) | **complete the partial archive leg** (§4.2.2) |
| Lab → SP3 contract | `LabResult`, `LabCandidate`, `ParamDelta`, `recommended_exit`, the dossier renderer (`ops/lab/dossier.py`) | the **`LabResult` JSON sidecar** emit (additive to `write_lab_dossier`, H-S3-9) + the ECR-side sidecar *loader/verifier* (model-validate, never markdown-scrape) |
| Engine scaffold | `tpcore/templates/engine_template/`, `docs/superpowers/checklists/engine_readiness.md` | ECR ADD invokes them as the build gate |
| Archive worked example | `archive/sigma/EULOGY.md` structure, the Sigma retirement checklist | `tpcore/templates/eulogy_template.md` (structure-reuse) |
| ops engine-dispatch pattern | `ops/lab/run.py:_runner_for` lazy per-engine import (legal-in-ops) | `ops/engine_sdlc/default_params.py` mirrors it |
| ops CLI shape | `ops/lab/__main__.py`, `ops/engine_ladder.py` `_amain` (explicit non-zero, never silent 0) | `ops/engine_sdlc/__main__.py` |
| Audit bus | `platform.application_log` + the `ENGINE_*` event convention | `ENGINE_CHANGE_REQUEST` event |
| Param search space | `ops.lab.run.PARAM_RANGES` (the closed sweepable set) | O1 `default_params()` keyed to it |

Net new code surface: `ops/engine_sdlc/{ecr,planner,default_params,
__main__,__init__}.py`, `docs/superpowers/checklists/
engine_change_request.md`, `tpcore/templates/eulogy_template.md`,
per-engine `default_params()` in 3 `backtest.py` files, the archive-leg
test completion + a `default_params` parity test, the
`credibility_pool` threading, the additive `LabResult` JSON sidecar
emit in `ops/lab/dossier.py` (H-S3-9). **No new tpcore module; no new
tpcore→engine import; no new daemon (SP3 is an on-demand operator
tool, exactly like the Lab).**

---

## 9. Symmetry / divergence ledger vs the Data Provider Lifecycle

**ADOPT (shape reused, content engine-native):**

- Flat single-SoT registry as the control surface
  (`engine_profile._PROFILE` ≈ `providers._BINDINGS`).
- One structured Change Request as the *only* operator touchpoint;
  never hand-edit the registry (`engine_change_request.md` ≈
  `data_feed_change_request.md`).
- Operator approves **exactly ADD + REMOVE**; everything reversible/
  gated is automated (§6 ≈ data §10, AUTHORITATIVE in each lane).
- "System prepares + validates the exact diff; binary y/n; a request
  that cannot produce a consistent diff is rejected, never forced."
- N-way CI consistency test where half-state fails the build
  (`test_engine_lifecycle_consistency.py` ≈
  `test_provider_lifecycle_consistency.py`).
- Snap-out = atomic offboard checklist with a provenance artifact
  (archive + EULOGY ≈ CSV-archive + 3-way-atomic RETIRE).
- Audit event on every operation for state-comprehension.

**DIVERGE (engine domain differs — deliberate):**

- **Gate is DSR/credibility, not data-parity.** Engine graduation
  evidence is the Lab's held-back DSR ≥ 0.95 ∧ credibility ≥ 60, not
  a `tpcore.parity` byte-equivalence. The ECR's evidence fields and
  the planner's re-verification are gate-numeric, not parity.
- **The Lab is the engine's EVALUATE.** Data's EVALUATE is a parity
  gate against a FALLBACK provider; the engine's "is this good enough"
  authority is The Lab (SP2) producing a SURVIVED dossier. The ECR
  *consumes* the dossier; it does not re-run the Lab.
- **paper→… lifecycle, no provider/fallback duality.** An engine has
  no "two providers, one active" structure; `LifecycleState` is a
  *maturity* axis (LAB→PAPER→LIVE→RETIRED), not an active/fallback
  pair. There is no ACTIVE/FALLBACK, no parity-verified standby.
- **REMOVE is a physical archive move + EULOGY, not a status flip
  with the code in place.** Data RETIRE flips a binding to RETIRED and
  keeps the adapter importable; engine REMOVE *physically relocates*
  `<engine>/ → archive/<engine>/` and writes a human EULOGY (the
  strategy's code is offboarded, not just deactivated).
- **NO CUTOVER analogue — explicitly not ported.** There is no "swap
  the implementation behind a stable engine interface" operation: an
  engine *is* its strategy. The data spec's CUTOVER section and the
  `data_provider_cutover.md` checklist have **no SP3 counterpart** by
  design (a re-tuned engine is a MODIFY, not a CUTOVER; a replacement
  strategy is REMOVE-then-ADD, two operator-gated decisions).
- **LAB→PAPER automated promotion** is the engine analog of data's
  automated CUTOVER *as a class* (reversible, gate-supplied judgement,
  no y/n) — but mechanically it is a maturity-edge, not a provider
  swap.
- **No weekly-digest construction in SP3.** Data §10 leans on the
  weekly digest as the state-comprehension floor; SP3 *emits the
  audit event* the existing bus carries but does **not** build an
  engine digest (engine-ladder/SP4 territory) — staying in lane.

---

## 10. Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Forged/stale ECR gate numbers | §5.4 zero-trust re-verification from the dossier/on-record data; mismatch = hard reject |
| Partial REMOVE (the Sigma-drift class) | atomic-or-abort `apply` + the completed archive-leg clockwork (§4.2.2) — a half-retire fails CI before merge |
| `dispatch_order` collision on ADD | classifier validates uniqueness against the staged `_PROFILE`; SP1 `test_no_half_state` re-proves it in the dry run |
| ADD lands a new engine straight to PAPER (unearned) | ADD always → LAB; PAPER is only reachable via the gated automated LAB→PAPER promotion |
| MODIFY smuggles a param the Lab never swept | planner rejects any `param_change` key not in that engine's `PARAM_RANGES` and not in the dossier's winning set |
| Lab credibility score poisons a live decision | planner reads `lab.<candidate>` only via the dossier, `backtest_credibility.<engine>` only for the EULOGY record — SP2 H-S2-3 namespace preserved |
| Executor leaves the tree dirty/half-applied on crash | `apply` records every touched file's pre-state and restores all on any failure; nothing committed by the executor (operator commits) |
| O1 accessor drifts from the search space | parity test: `default_params()` key set == `PARAM_RANGES` key set per engine; a new searched param without a default fails CI |
| ECR for `allocator` (the structurally-separate engine) | classifier treats `allocator` like any `_PROFILE` engine for MODIFY/REMOVE, but ADD of a second allocator-path engine is rejected (no `_dispatch_allocator` for it) — documented edge, fail-closed |
| Engine renamed mid-flight | out of scope — a rename is REMOVE-then-ADD (two operator-gated decisions); no in-place rename edge exists (fail-closed by the closed classifier table) |

---

## 11. Hardening

This section is the output of the adversarial expert-harden pass. It
supersedes the §10 failure-mode table where they overlap (§10 is the
design-time sketch; §11 is the enforced contract). Every `H-S3-*`
entry is **risk → mitigation → enforcement point** (the test/guard that
makes the mitigation true, not aspirational).

### 11.1 The H-S3-* hardening register

**H-S3-1 — The dry-consistency run must execute the REAL clockwork
against the proposed tree, not a dict-injected approximation.**
*Risk:* the spec's §5.2 idea of "inject a profile dict via the
`_roster_sorted(profiles=…)` seam" is **insufficient and unsafe**:
verified, `roster_for_dispatch()` / `allocator_eligible_engines()` /
`archived_engines()` call `_roster_sorted()` with **no** `profiles=`
arg (they bind module-global `_PROFILE` at import), and
`test_engine_lifecycle_consistency.py` reads a module-constant
`REPO = Path(__file__).resolve().parents[2]` and calls
`roster_for_dispatch()` *directly*. A dict-injection dry run would
validate a *different* code path than the one CI runs after merge — a
green dry run could still be a red real build (the exact false-negative
that lets a half-state through).
*Mitigation:* `validate(plan)` stages the full proposed tree (the
rewritten `tpcore/engine_profile.py` source, the moved/scaffolded
package dirs, the rendered EULOGY, the shadow-file edits) into an
**isolated temp repo copy** (`shutil.copytree` of the worktree minus
`.git`/`.venv`/`__pycache__`/`backtests`), then runs
`python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py
-q` **as a fresh subprocess with `cwd` = the temp tree** (so its
`REPO`, its `import tpcore.engine_profile`, and `_PROFILE` are all the
*proposed* ones, with zero in-process module-state bleed). The dry run
passes iff that subprocess exits 0. The dict-injection seam is **not**
the dry-run mechanism; it is retained only as a fast unit-test
convenience for `_roster_sorted` itself.
*Enforced at:* `ops/engine_sdlc/planner.py::validate` (subprocess
invocation + exit-code assertion); pinned by
`tpcore/tests/test_engine_sdlc_planner.py::
test_validate_runs_real_clockwork_in_isolated_tree` (a staged tree with
a deliberately-introduced half-state must make `validate` reject with
the clockwork's own failure text) — **T4**.

**H-S3-2 — The `dispatch_order` literal pin must transition with the
ECR, never be hand-patched or silently broken.**
*Risk:* `test_dispatch_order_invariant_is_the_frozen_literal` asserts
`roster_for_dispatch() == ("reversion","vector","momentum","sentinel",
"canary")` — a hard literal (roster order is high-risk per DA-3/Sub-C).
A legitimate ADD(→LAB) does **not** change it (LAB is not dispatchable,
filtered by `_DISPATCHABLE`); a REMOVE of a *currently-rostered* engine
**does** change it; a LAB→PAPER promotion **does** change it. If that
literal is left stale the honest build red-fails; if a task "fixes" it
by hand the operator-never-hand-edits contract is violated and the pin
stops being a pin.
*Mitigation:* (a) the dry-run subprocess (H-S3-1) runs that exact test
against the *proposed* tree — so a transition that changes the roster
is **rejected unless the literal is updated in the same staged change**;
(b) the planner, when (and only when) the transition changes
`roster_for_dispatch()`, mechanically rewrites the literal tuple inside
`test_dispatch_order_invariant_is_the_frozen_literal` to the
SoT-derived proposed roster **as part of the same staged diff** (it is
a structurally-parseable shadow exactly like the
`run_smoke_test.sh`/`pyproject` shadows — added to the enumerated
shadow-edit set in fs_op 4); (c) the operator sees the literal change
in the prepared diff and approves it as part of the ADD/REMOVE binary
y/n — it is never a separate hand-edit. ADD(new_scaffold)/
ADD(lab_candidate) → LAB asserts the literal is **unchanged** (defends
against an accidental dispatch_order that leaks a LAB engine into the
roster).
*Enforced at:* `planner.py` (conditional literal rewrite, gated on
`roster_for_dispatch()` delta); `tpcore/tests/test_engine_sdlc_planner
.py::test_remove_rostered_engine_updates_frozen_literal` and
`::test_add_lab_engine_leaves_frozen_literal_untouched` — **T5/T6**.

**H-S3-3 — The `_PROFILE` source rewrite must be structurally safe
(AST-faithful), never a regex smash of a frozen-pydantic literal.**
*Risk:* `_PROFILE` is a hand-formatted dict of `EngineProfile(...)`
calls with inline comments (the `allocator`/`sigma`/`lab` sentinels) in
a tpcore module every engine imports. A naïve string/regex rewrite can
(i) corrupt the literal (broken syntax → every engine's import dies,
platform-wide outage), (ii) silently drop the explanatory comments
(losing the D-SDLC provenance), or (iii) reorder keys (cosmetic but
review-noisy and diff-hostile).
*Mitigation:* the rewrite is **AST-validated, append/replace-shaped,
and re-parsed before staging**: (a) ADD inserts a new
`"<engine>": EngineProfile(...)` entry **immediately before the
`allocator` sentinel comment** (a stable, documented anchor) — never
reformats existing entries; (b) REMOVE/MODIFY-of-state edits **only the
single target entry's** `lifecycle_state=`/`allocator_eligible=` tokens
in place (a targeted, line-anchored replace of that one
`EngineProfile(...)` call), touching no sibling; (c) after the textual
edit the planner does `ast.parse()` on the new source **and**
`compile()`s it **and** imports it in the H-S3-1 subprocess — any
`SyntaxError`/`ValueError` (e.g. pydantic `extra="forbid"` /
`frozen=True` violation, duplicate key) aborts with the parser's exact
error and **stages nothing**; (d) MODIFY never touches `_PROFILE` at
all (engine params are not in the lifecycle SoT — §4.3), removing that
entire risk surface for the highest-frequency action.
*Enforced at:* `planner.py::_rewrite_profile_source` (ast.parse +
compile gate before any disk write); `test_engine_sdlc_planner.py::
test_profile_rewrite_is_ast_valid_and_preserves_siblings` and
`::test_malformed_rewrite_aborts_with_zero_disk_change` — **T5**.

**H-S3-4 — `apply()` must be atomic-or-abort with a real
pre-state journal; a crash mid-apply must leave the tree byte-identical
to pre-apply.**
*Risk:* a half-applied transition (`_PROFILE` flipped but the
`shutil.move(<engine>/ → archive/<engine>/)` failed; or the package
moved but the literal not flipped; or the EULOGY written but the
shadow-edits not) is exactly the Sigma-drift class the whole SP exists
to kill — and worse here because it is *machine*-produced.
*Mitigation:* `apply(plan)` is journaled and reversible: (a) **before
any mutation** it records, for every file it will touch, the exact
prior bytes (or "absent"), and for every dir move, the source/dest
pair; (b) it performs the mutations in a fixed order: write `_PROFILE`
+ shadow-file edits + EULOGY (text edits, easy to revert) **first**,
the package `shutil.move` **last** (the irreversible-ish op last, so
the cheap-to-revert ops are validated before it); (c) it then runs the
**on-disk** consistency subprocess (H-S3-1) against the now-staged real
tree; (d) **green ⇒ leave it** (the operator commits via normal git —
the executor never touches git); (e) **red OR any exception ⇒ restore
every journaled file to its exact prior bytes and every dir move to its
origin, in reverse order**, then emit the rejection; (f) the journal
restore is itself wrapped so a restore failure escalates loudly
(`ENGINE_CHANGE_REQUEST` with `outcome=apply_restore_failed`) rather
than silently leaving a half-state. No `DATA_OPERATIONS_COMPLETE`-style
sacred-invariant analog is needed because the executor never enables
trading; the consistency subprocess is the gate.
*Enforced at:* `planner.py::apply` (journal + reverse-order restore);
`test_engine_sdlc_planner.py::test_apply_red_consistency_rolls_back_to_
byte_identical` and `::test_apply_move_failure_restores_text_edits` —
**T5** (REMOVE is the first action that exercises the package move, so
the atomicity test lands with it).

**H-S3-5 — Completing the archive-leg clockwork: a partial REMOVE must
fail CI exactly as a half-retired data feed does.**
*Risk:* SP1 left `test_retired_engine_fully_offboarded` as a *detector*
only (RETIRED ⇒ archive dir + EULOGY exists + no package). The gaps:
(i) an EULOGY that is a touched zero-byte/stub file passes today (the
data-lane "fake-healable HealSpec" analog — a real artifact-shaped
hole); (ii) a RETIRE that forgot a shadow (`run_smoke_test.sh` /
`pyproject`) only fails *indirectly* via
`test_structurally_parseable_shadows_match_sot`, not on the retire leg;
(iii) the inverse — an `archive/<engine>/` dir or
`archive/<engine>/EULOGY.md` with **no** RETIRED `_PROFILE` entry
(orphan archive) — is not asserted at all; (iv) an engine still
importable as a live `<engine>.scheduler` while RETIRED.
*Mitigation:* extend the **existing** tpcore test file (no new module,
no tpcore→engine import — it imports only `tpcore.engine_profile` +
stdlib + filesystem) with: (a) **EULOGY content floor** — the file must
contain a non-empty `## Cause of death` and a `## Retirement checklist`
section (header present + ≥1 non-blank line under each); (b)
**shadow-purge completeness** — a RETIRED engine's name must be **absent
from** the `run_smoke_test.sh` step-3 loop token list **and** the
`pyproject` testpaths/include arrays (the explicit RETIRED-absent
assertion, so a forgotten shadow fails on the retire leg); (c)
**no-orphan-archive** — every `archive/<dir>/` that contains an
`EULOGY.md` must correspond to a `_PROFILE` entry with
`lifecycle_state == RETIRED` (catches an archive with no SoT entry);
(d) **RETIRED ⇒ not importable as a live engine** — `importlib.util.
find_spec(f"{name}.scheduler")` must be `None` for a RETIRED engine
(symmetric to the live-engine leg's positive assertion). This is the
direct symmetry of the data 3-way `test_provider_lifecycle_consistency
.py` (`test_fully_retired_feed_offboarded_everywhere`) — shape reused,
engine-native predicates.
*Enforced at:* `tpcore/tests/test_engine_lifecycle_consistency.py`
(extended in place); the new assertions are exercised end-to-end by the
REMOVE executor's synthetic-throwaway-engine test — **T5** (the test
extension lands in the SAME task as the REMOVE executor that makes a
clean retire, per TDD: the clockwork that proves a behavior ships with
the behavior, never before — landing it earlier would red-fail an
honest build with no producer).

**H-S3-6 — Automated MODIFY zero-trust: the gate is the ONLY thing
between a dossier and live params, so it re-derives, never trusts, and
cannot escape the risk envelope.**
*Risk:* operator-confirmed `automated-if-gated` (§6.2, §12 — operator
risk-acceptance recorded 2026-05-18) means no human y/n stands between
a `fold_existing` dossier and a live-traded param change. A forged ECR,
a stale dossier, a dossier for a *different* engine, a `param_change`
key the Lab never swept, or a MODIFY that smuggles a lifecycle/strategy
change would each be a real-capital hazard.
*Mitigation:* (a) **evidence is re-derived from the frozen `LabResult`
JSON sidecar, never the ECR text and never the rendered markdown**
(H-S3-9): model-validate the sidecar (`extra="forbid"`), then assert
`verdict=="SURVIVED"`, `dsr≥0.95`, `credibility_score≥60`,
`recommended_exit=="fold_existing"`, `target_engine==ecr.engine`; (b)
**identity-freshness**: the sidecar's `candidate`/`seed`/path must match
the ECR's cited dossier and `winning_params` must be a superset of
`ecr.param_change` with **equal values** (a value mismatch = reject);
(c) **search-space membership**: every key in `ecr.param_change` must be
in that engine's `ops.lab.run.PARAM_RANGES` set **and** in the sidecar
`winning_params` (no smuggling a param the Lab never swept); (d)
**lifecycle-immutability**: MODIFY's `sot_diff` is asserted **empty** —
the planner hard-fails if a MODIFY plan carries any `_PROFILE` edit
(strategy existence / lifecycle / allocator-eligibility cannot be
touched by MODIFY by construction); (e) **envelope invariance**: the
re-tuned param keyset is a subset of `PARAM_RANGES`, which is by
definition within the engine's existing RiskGovernor/`RiskLimits`/
capital-gate envelope (a MODIFY changes *which* params, never the risk
contract — documented invariant, not a runtime check, because no SP3
code path can reach RiskGovernor); (f) **paper backstop pinned**: no
engine is `LifecycleState.LIVE` (paper-only mandate, §1.1) — a MODIFY
today changes paper behaviour only; this is recorded as the residual
backstop, not relied on as the primary control (the gate is). Any
failure of (a)–(d) ⇒ hard reject, zero mutation, `ENGINE_CHANGE_
REQUEST` audit with `outcome=rejected` + the precise reason.
*Enforced at:* `planner.py::validate` (the MODIFY branch); `test_
engine_sdlc_planner.py::test_modify_rejects_{forged_numbers,wrong_
target,non_param_ranges_key,value_mismatch,stale_sidecar}` and
`::test_modify_plan_sot_diff_is_always_empty` — **T7**.

**H-S3-7 — Operator-interaction integrity: a declined/rejected ECR
mutates exactly zero bytes; the binary confirm is explicit and
un-spoofable; every applied transition emits an audit receipt.**
*Risk:* the prepare→validate→binary-confirm flow is the whole
operator-safety contract for ADD/REMOVE (the structural, irreversible
decisions). If a non-`y` answer could still mutate, or a stray
stdin/EOF defaulted to "yes", or an applied transition emitted no
audit, the contract is hollow.
*Mitigation:* (a) `apply()` is **never called** on the OPERATOR
approval path until an **exact, case-sensitive `y` (or `yes`)** is read
from a TTY prompt — any other token, empty line, EOF, or
non-interactive stdin ⇒ **declined**, `apply` not called, zero mutation
(fail-closed default = decline, mirroring the data-lane binary y/n and
the `should_fire` fail-closed posture); (b) the validation+diff render
runs **before** the prompt, so the operator only ever confirms a
green-validated diff (a rejected plan never reaches the prompt — it
exits non-zero with the reason, exactly the data-lane "rejected with
the reason, never handed to you to force"); (c) **every** terminal
outcome emits one `platform.application_log` `ENGINE_CHANGE_REQUEST`
event with `action`, `engine`, `from_state`, `to_state`, `approval_
class`, and `outcome ∈ {applied, rejected, operator_declined, apply_
restore_failed}` + the rejection reason if any (engine-native audit,
symmetry-ref of the data-lane audit-event discipline — the single
existing `application_log` bus, not a new digest/reporting lane, §3.3);
(d) the MODIFY/promote automated paths emit the same event with
`approval_class=AUTOMATED` so an automated change is as visible as an
operator one.
*Enforced at:* `ops/engine_sdlc/__main__.py` (TTY-explicit-`y` gate,
fail-closed) + `planner.py::apply`/`validate` (audit emit on every
terminal branch); `test_engine_sdlc_cli.py::test_{non_y_declines_zero_
mutation,eof_declines,rejected_plan_never_prompts,every_outcome_emits_
audit}` — **T8**.

**H-S3-8 — SP2 carry-forwards must not regress SP2's isolation
contract or its characterization oracle.**
*Risk:* O1 (`default_params()` + dispatcher + wiring `_build_lab_
result`) and the `credibility_pool` threading both touch live SP2
surfaces. Specific hazards: (i) adding a tpcore→engine import while
wiring the dispatcher (breaks `check_imports`); (ii) the
`_build_lab_result` change perturbing the T1 characterization oracle
(`scripts/tests/test_search_parameters_characterization.py`) which pins
`amain`'s rc + the `write_credibility_score` call args; (iii) threading
`credibility_pool` accidentally making the ONE allowlisted RW write
read-only under `_LAB_ACTIVE` (the credibility append is the single
intentional RW exception inside the Lab — `tpcore/tests/test_lab_
isolation.py` pins zero live-write delta + the no-poison namespace);
(iv) the legacy `python scripts/search_parameters.py` path (candidate
is None) drifting from byte-identical.
*Mitigation:* (a) the `default_params()` accessor is added **only to
the three engine `backtest.py` files** (reversion/vector/momentum — the
`PARAM_RANGES` engines); the dispatcher
`ops/engine_sdlc/default_params.py` does the **lazy per-engine import
inside the function body** (exact parity with the proven-legal
`ops/lab/run.py::_runner_for`) — **zero tpcore module touched, zero
tpcore→engine import**; a parity test asserts
`set(default_params(e)) == set(PARAM_RANGES[e])` per engine and that
sentinel/canary (no search space) expose **no** accessor (a new
searched param without a default fails CI — the HealSpec-coverage
discipline); (b) `_build_lab_result` calls the dispatcher and emits
`ParamDelta(current=default_params(e)[k], winning=v)` — the T1 oracle
pins rc + the credibility-call args, **not** `param_diff` contents, so
this is oracle-neutral by construction; the task **re-runs the T1
oracle unchanged** as its gate; (c) `credibility_pool` threading: when
a `LabContext` is active *and* `candidate is not None`, `_run_lab_core`
uses `context.credibility_pool` (the existing allowlisted RW handle)
instead of opening its own `asyncpg.create_pool`; when `candidate is
None` (legacy search CLI, no LabContext) the **own-pool path is
byte-identical, untouched**; the `write_credibility_score` *call args*
(engine_name, score) are unchanged in both paths (only the pool object
differs), so the T1 oracle and `test_lab_isolation.py` both stay green
— pinned by a new assertion that under an active `LabContext` the
write goes through `context.credibility_pool` and **no second RW pool
is created inside the isolation boundary**; (d) the LabContext is
**not reentrant** (verified: inner exit resets `_LAB_ACTIVE`) — the
threading must read `context.credibility_pool` from the *active* CM,
not construct a nested `LabContext`.
*Enforced at:* `<engine>/backtest.py::default_params` ×3 +
`ops/engine_sdlc/default_params.py` + `ops/lab/run.py::_build_lab_
result`/`_run_lab_core`; `tpcore/tests/test_engine_default_params_
parity.py` (parity + coverage), the unchanged T1 oracle, the unchanged
`test_lab_isolation.py`, and a new `tpcore/tests/test_lab_credibility_
pool_threaded.py::test_active_labcontext_write_uses_context_pool_no_
second_rw_pool` — **T1 (O1)**, **T2 (pool)**.

**H-S3-9 — The Lab evidence the automated MODIFY gate trusts must be a
machine-readable frozen artifact, not scraped rendered markdown
(design-defect fix).**
*Risk (design defect found during hardening):* SP2's
`ops/lab/dossier.py::write_lab_dossier` writes **only** the rendered
`.md`; there is **no persisted machine-readable `LabResult`**. The
spec's original §5.4 had the planner "re-parse the dossier markdown".
For the **automated, no-y/n** MODIFY path that is the single
load-bearing gate, re-scraping prose rendered by a template is fragile
(template reformat silently breaks the gate or, worse, mis-parses a
number) — unacceptable for a real-capital control.
*Mitigation:* `write_lab_dossier` additionally writes a sibling
`<dossier>.json` = `LabResult.model_dump_json()` (frozen pydantic,
deterministic field order). The SP3 planner loads + `model_validate`s
that JSON (`extra="forbid"` ⇒ a tampered/extra field is a hard reject)
and re-derives every gate number from the validated model — the `.md`
stays purely human-facing. This is an **additive, behaviour-preserving
~3-line change to one ops function** (the rendered markdown is
byte-unchanged; only a new sibling file is added) — in SP3 scope as the
SP2→SP3 evidence carry-forward, not new SP2 design. It is the precise
engine-domain analog of the data lane persisting structured state
(not prose) as the authority a gate reads.
*Enforced at:* `ops/lab/dossier.py::write_lab_dossier` (sidecar emit) +
`planner.py::validate` (sidecar load/validate, never markdown);
`tpcore/tests/test_lab_dossier_sidecar.py::test_sidecar_roundtrips_
labresult_and_md_unchanged` (round-trip + markdown-byte-stability) —
**T3** (lands before any executor that consumes it; the planner's
sidecar reader and the REMOVE/MODIFY executors that depend on it come
after).

**H-S3-10 — Lane + collision discipline: no tpcore→engine import; the
`ops`-package vs `scripts/ops.py` `sys.modules` collision is
pre-empted; SP3 does no data-lane or SP4 work.**
*Risk:* (i) any new tpcore→engine edge fails `check_imports` (the
4e73fe8 layering invariant); (ii) **the SP2-proven `ops` collision**:
`tpcore/tests/test_ops.py` does `sys.path.insert(0, scripts/)` then
`import ops` — which binds `sys.modules["ops"]` to **`scripts/ops.py`**
(a 160 KB single-file module), *not* the `ops/` package. Any SP3 test
that imports `ops.engine_sdlc.*` **at module/collection scope** can
clobber or be clobbered by that binding (the exact bite SP2 T9/T10
hit); (iii) SP3 accidentally doing SP4's doc closure
(CLAUDE.md/OPERATIONS.md/glossary regen) or touching a data-lane SoT.
*Mitigation:* (a) every SP3 test that needs `ops.engine_sdlc.*` imports
it **lazily inside the test function body**, never at module top
(exact parity with SP2's `tpcore/tests/test_lab_isolation.py` discipline
— "we do NOT import ops.lab.run at module level"); SP3 test files live
in **`tpcore/tests/`** (a collected pyproject testpath) — **not**
`ops/engine_sdlc/tests/` (uncollected ⇒ a safety test there silently
never runs), matching the SP2 decision verbatim; (b) `check_imports` is
run in the T9 full-suite gate and the planner's `_PROFILE` rewrite is
asserted **data-only** (a test greps the rewritten source for any
`import`/`from` line delta vs the original — the rewrite may only
change `EngineProfile(...)` data tokens, never add an import); (c) SP3
edits **no** `CLAUDE.md`/`OPERATIONS.md`/`glossary.md` and **no**
`tpcore/providers.py`/`tpcore/feeds/`/`tpcore/selfheal/` (data-lane) —
a scope assertion in the T9 gate diff-checks that the SP3 change set is
confined to the §8 net-new surface + the enumerated extends.
*Enforced at:* test file placement + lazy-import convention (all SP3
test files); `test_engine_sdlc_planner.py::test_profile_rewrite_adds_no
_import`; the T9 `ruff` + `check_imports` + scope-diff gate — **every
task** (placement/lazy-import is a standing constraint), **T9**
(the suite-level proof).

**H-S3-11 — The ADD readiness gate and the `lab_candidate`/`new_
scaffold` split must fail closed (no unearned PAPER, no half-scaffold).**
*Risk:* ADD is operator-gated but the *scaffold quality* is machine-
produced. Hazards: a `new_scaffold` ADD that presents a `gate_dsr`/
`gate_cred` it cannot have earned; a `lab_candidate` ADD whose dossier
actually says `fold_existing` (that is a MODIFY, not an ADD); a
scaffold that imports but has no `tests/`/no `BaseEnginePlug` plugs;
ADD landing straight into PAPER (unearned graduation).
*Mitigation:* (a) ADD **always** → `LifecycleState.LAB`, never PAPER
(PAPER is reachable only via the separate automated gated LAB→PAPER
`promote` — §4.1); the classifier hard-rejects any ADD plan whose
`to_state != LAB`; (b) `new_scaffold` ADD **rejects** non-None
`gate_dsr`/`gate_cred` (a new engine cannot present a score it has not
earned — fail-closed); (c) `lab_candidate` ADD re-derives from the JSON
sidecar (H-S3-9) and **rejects** unless `recommended_exit==
"promote_new"` (a `fold_existing` sidecar with that exact guidance is a
MODIFY — explicit redirect in the rejection text); (d) the
programmatically-checkable `engine_readiness.md` items (package
present, `<engine>/tests/` dir, importable `<engine>.scheduler`, 5
plugs subclass `BaseEnginePlug` via the documented `grep -E
"class\s+\w+\(BaseEnginePlug\)"` count == 5) are run against the
*staged scaffold* in `validate`; any miss ⇒ reject, zero mutation; (e)
LAB-entry forces `allocator_eligible=False` (SP1 `test_no_half_state`),
re-proven by the H-S3-1 dry run.
*Enforced at:* `planner.py::classify`/`validate` (ADD branch);
`test_engine_sdlc_planner.py::test_add_{new_scaffold_rejects_gate_
fields,lab_candidate_requires_promote_new,readiness_miss_rejects,always_
lands_LAB}` — **T6**.

**H-S3-12 — The CLI must never silently succeed; explicit non-zero on
every non-apply outcome (the canary `-m`-no-op lesson).**
*Risk:* a `python -m ops.engine_sdlc` invocation that prints nothing
and exits 0 on a rejected/declined/parse-failed ECR would let a
no-op masquerade as success (the documented canary-`-m`-no-op /
"explicit non-zero, never silent 0" lesson the Lab CLI already
encodes).
*Mitigation:* the CLI mirrors `ops/lab/__main__.py::_amain` /
`ops/engine_ladder._amain` exactly: parse failure → printed reason +
rc1; rejected plan → printed reason + rc1; operator-declined → printed
"declined, nothing changed" + rc1; applied → printed receipt + rc0;
MODIFY/promote automated-applied → printed done-receipt + rc0; **no
code path returns 0 without a successful `apply` (or a clean no-work
explicitly stated)**. `python -m ops.engine_sdlc` with no/invalid args
exits non-zero with usage.
*Enforced at:* `ops/engine_sdlc/__main__.py`; `test_engine_sdlc_cli.py
::test_{parse_fail_rc1,reject_rc1,decline_rc1,apply_rc0,no_args_rc_
nonzero}` — **T8**.

### 11.2 Residual risks consciously accepted

- **R1 — MODIFY automated is operator-confirmed risk-acceptance, not
  expert-eliminated.** The operator explicitly chose
  automated-if-gated (2026-05-18, recorded §12). The residual (a param
  change reaches paper-traded behaviour with no human y/n) is bounded
  by H-S3-6 zero-trust + the paper-only backstop + the one-line
  documented escape-hatch (operator may flip a single MODIFY to y/n via
  the §6 approval-class table, not a redesign). **Accepted as
  operator-owned, not residual-expert-owned.**
- **R2 — `apply()` is working-tree-atomic, not git-atomic.** The
  executor never runs git (standing rule); a crash *after* a green
  apply but *before* the operator commits leaves a valid, CI-green,
  uncommitted change (the operator's normal `git status` shows it).
  This is the same posture as every other operator tool here and is
  **accepted** — git-atomicity would require the executor to run git,
  which is explicitly forbidden.
- **R3 — The H-S3-1 dry run runs the full consistency suite via a
  subprocess `copytree`, which is O(repo size).** Accepted: it is an
  on-demand operator tool (not a daemon, not on the trade-submit path),
  the copy excludes `.git`/`.venv`/`__pycache__`/`backtests`, and
  fidelity (real test, real `_PROFILE`, real `REPO`) is worth more than
  a few seconds — a dict-injected fast path was rejected (H-S3-1) as
  validating the wrong code path.

### 11.3 Design defects found and fixed inline

- **D1 (fixed §5.4, §8, H-S3-9):** SP2 persists only rendered dossier
  markdown; the automated-MODIFY gate cannot safely rest on
  markdown-scraping. Fixed by the additive `LabResult` JSON sidecar in
  `write_lab_dossier` + the planner reading the validated frozen model.
- **D2 (fixed H-S3-1):** the spec's §5.2 "inject a profile dict via
  `_roster_sorted(profiles=…)`" is insufficient — the public accessors
  and the consistency test bind module-global `_PROFILE`/`REPO`, so a
  dict-injected dry run validates a different path than CI. Fixed by
  the isolated-temp-tree subprocess dry run; the injection seam is
  retained only for `_roster_sorted` unit tests.

---

## 11A. Final ordered TDD task decomposition (T0–T9)

Every task is self-contained and **CI-green on its own** (the pinning
test ships in the **same** task as the behaviour — never a behaviour
without its test, never a test before its producer). Lane/collision
constraints (H-S3-10) apply to **every** task: SP3 test files live in
`tpcore/tests/`, import `ops.engine_sdlc.*` **lazily inside test
bodies**, and SP3 touches no tpcore *code* / no data-lane SoT / no SP4
doc. Ordering rationale follows the data dependency: contracts/seams
(T0–T3) → pure logic (T4-classify is folded into T3? no — see below) →
executors that consume them (T5–T7) → CLI/audit surface (T8) →
suite-level proof (T9).

- **T0 — `ops/engine_sdlc/` package + ECR contract + checklist.**
  *Create:* `ops/engine_sdlc/__init__.py` (one-line docstring noting
  ops is exempt from the check_imports tpcore∌engine scan, parity with
  `ops/lab/__init__.py`), `ops/engine_sdlc/ecr.py`
  (`ECRAction`, `EngineChangeRequest` frozen pydantic-v2
  `extra="forbid"` + the exactly-one-action model validator,
  `parse_ecr(text) -> EngineChangeRequest` strict fenced-block parser
  that rejects unknown keys / wrong-block fields with the exact
  reason), `docs/superpowers/checklists/engine_change_request.md` (the
  copy/fill block, symmetric in feel to `data_feed_change_request.md`,
  with the §6 operator-interaction policy header).
  *Create test:* `tpcore/tests/test_ecr_parse.py` —
  `test_{valid_add_parses,valid_remove_parses,valid_modify_parses,
  unknown_key_rejected,cross_block_field_rejected,multi_action_
  rejected,nonparsing_rejected_with_reason}` (lazy `import
  ops.engine_sdlc.ecr` inside each).
  *Pinning test:* `test_unknown_key_rejected` +
  `test_multi_action_rejected` (the strict-parser contract).

- **T1 — O1: per-engine `default_params()` + dispatcher + wiring +
  parity test (carry-forward; H-S3-8).**
  *Create:* `default_params() -> dict[str, Any]` module-level pure fn
  in `reversion/backtest.py`, `vector/backtest.py`,
  `momentum/backtest.py` (returns current defaults for **exactly** that
  engine's `PARAM_RANGES` keys — reuse the existing module accessors:
  reversion `_hard_stop_pct`/`_max_hold_days`/`_volume_climax_threshold`
  + the `Z_SCORE_THRESHOLD` default; vector `_pb_ceiling`/`_de_ceiling`/
  `_catalyst_window_days`/`_hard_stop_pct`/`_swing_score_threshold`;
  momentum the existing `_lookback`/`_skip`/`_hold`/`_top_decile`
  block); `ops/engine_sdlc/default_params.py` (lazy per-engine import
  dispatcher, parity with `ops/lab/run.py::_runner_for`).
  *Modify:* `ops/lab/run.py::_build_lab_result` — replace the
  `# TODO(SP3)` line with `ParamDelta(current=default_params(args.
  engine)[k], winning=v)`.
  *Create test:* `tpcore/tests/test_engine_default_params_parity.py` —
  `test_each_param_ranges_engine_default_keyset_equals_param_ranges`
  (the cannot-be-forgotten clockwork), `test_sentinel_canary_have_no_
  accessor`.
  *Re-run unchanged as gate:* the T1 characterization oracle
  `scripts/tests/test_search_parameters_characterization.py` (must pass
  identically — pins rc + credibility-call args, not `param_diff`).
  *Pinning test:* `test_each_param_ranges_engine_default_keyset_equals_
  param_ranges` (a new searched param without a default fails CI).

- **T2 — `credibility_pool` threading (carry-forward; H-S3-8).**
  *Modify:* `ops/lab/run.py::_run_lab_core` — when a `LabContext` is
  active **and** `candidate is not None`, use the active context's
  `credibility_pool` for the `write_credibility_score` call instead of
  `asyncpg.create_pool`; `candidate is None` (legacy CLI, no
  LabContext) keeps its own pool **byte-identical**. The
  `write_credibility_score(engine_name=…, score=…)` call args are
  unchanged in both paths.
  *Create test:* `tpcore/tests/test_lab_credibility_pool_threaded.py::
  test_active_labcontext_write_uses_context_pool_no_second_rw_pool`
  (DB-gated, skip-without-`DATABASE_URL`, parity with
  `test_lab_isolation.py`'s gating).
  *Re-run unchanged as gate:* `test_lab_isolation.py` (zero live-write
  delta + no-poison still green) **and** the T1 oracle (unchanged).
  *Pinning test:* `test_active_labcontext_write_uses_context_pool_no_
  second_rw_pool`.

- **T3 — Lab `LabResult` JSON sidecar (design-defect fix; H-S3-9) +
  the planner-side sidecar loader/verifier.**
  *Modify:* `ops/lab/dossier.py::write_lab_dossier` — additionally
  write `<dossier>.json = LabResult.model_dump_json()` (rendered `.md`
  byte-unchanged).
  *Create:* `ops/engine_sdlc/_evidence.py` —
  `load_labresult_sidecar(md_path) -> LabResult` (resolve the sibling
  `.json`, `LabResult.model_validate_json`, `extra="forbid"`; raise a
  typed `EvidenceError` with the exact reason on missing/tampered).
  *Create test:* `tpcore/tests/test_lab_dossier_sidecar.py::
  test_sidecar_roundtrips_labresult_and_md_unchanged`,
  `::test_loader_rejects_missing_sidecar`,
  `::test_loader_rejects_tampered_extra_field`.
  *Pinning test:* `test_sidecar_roundtrips_labresult_and_md_unchanged`
  (round-trip fidelity + markdown byte-stability).

- **T4 — `classify()` + `TransitionPlan` + the closed classification
  table (pure, no I/O) + `validate()`'s dry-consistency mechanism
  (H-S3-1, H-S3-2 read-side).**
  *Create:* `ops/engine_sdlc/planner.py` — `TransitionPlan`
  (frozen: `from_state`, `to_state`, `sot_diff`, `fs_ops`,
  `gate_checks`, `approval_class`, `rejection: str | None`);
  `classify(ecr, profile_snapshot) -> TransitionPlan` (the **total,
  closed** §5.1 table — every `(action, in-profile?, from_state,
  source)` maps to an edge or a typed rejection); `validate(plan,
  *, repo_root)` skeleton with the **isolated-temp-tree subprocess
  consistency runner** (`shutil.copytree` minus `.git/.venv/__pycache__
  /backtests` → `python -m pytest tpcore/tests/test_engine_lifecycle_
  consistency.py -q` with `cwd`=temp → pass iff rc0) — executors
  (T5–T7) fill the action-specific staging.
  *Create test:* `tpcore/tests/test_engine_sdlc_planner.py` —
  `test_classify_every_table_cell` (every row of §5.1 incl. every
  rejection: ADD-exists, REMOVE-absent, REMOVE-retired, MODIFY-absent,
  MODIFY-retired), `test_validate_runs_real_clockwork_in_isolated_tree`
  (a staged half-state tree ⇒ `validate` rejects with the clockwork's
  own failure text), `test_profile_rewrite_adds_no_import`.
  *Pinning test:* `test_classify_every_table_cell` +
  `test_validate_runs_real_clockwork_in_isolated_tree`.

- **T5 — REMOVE executor + EULOGY template + completed archive-leg
  clockwork + atomicity (H-S3-3, H-S3-4, H-S3-5, H-S3-2 REMOVE leg).**
  *Create:* `tpcore/templates/eulogy_template.md` (Sigma-validated
  section structure: title+date / `## Cause of death` / `## What it
  leaves behind` / `## Retirement checklist` — structure only, not
  Sigma content).
  *Modify:* `ops/engine_sdlc/planner.py` — the REMOVE `apply` leg:
  AST-validated single-entry `_PROFILE` flip to RETIRED +
  `allocator_eligible=False`; `shutil` package move
  `<engine>/ → archive/<engine>/` (+ by-name wrapper scripts) **last**;
  EULOGY render from template; the enumerated shadow edits
  (`run_smoke_test.sh` step-3 loop, `pyproject` testpaths/include) +
  the conditional `test_dispatch_order_invariant_is_the_frozen_literal`
  literal rewrite **iff** the roster changes; `ENGINE_TABLES` orphan
  removal; the journaled pre-state + reverse-order restore.
  *Modify (extend in place):* `tpcore/tests/test_engine_lifecycle_
  consistency.py` — add the H-S3-5 assertions (EULOGY content floor;
  RETIRED-absent shadow purge; no-orphan-archive; RETIRED ⇒ not
  importable).
  *Create test:* in `test_engine_sdlc_planner.py` —
  `test_remove_throwaway_engine_end_to_end` (synthetic fixture engine
  in a temp tree → clean retire → the extended clockwork passes),
  `test_apply_red_consistency_rolls_back_to_byte_identical`,
  `test_apply_move_failure_restores_text_edits`,
  `test_profile_rewrite_is_ast_valid_and_preserves_siblings`,
  `test_malformed_rewrite_aborts_with_zero_disk_change`,
  `test_remove_rostered_engine_updates_frozen_literal`.
  *Pinning test:* `test_remove_throwaway_engine_end_to_end` +
  `test_apply_red_consistency_rolls_back_to_byte_identical`.

- **T6 — ADD executor (new_scaffold + lab_candidate) + readiness build
  gate (H-S3-11, H-S3-2 ADD leg).**
  *Modify:* `planner.py` — ADD `apply` leg: scaffold from
  `tpcore/templates/engine_template/` (new_scaffold) or from the
  Lab-proven candidate (lab_candidate, sidecar-verified via T3
  `_evidence`); insert the LAB `_PROFILE` entry (AST-safe, before the
  `allocator` sentinel anchor, `allocator_eligible=False`); run the
  programmatically-checkable `engine_readiness.md` items against the
  staged scaffold; assert the frozen-literal is **unchanged** (LAB not
  rostered).
  *Create test:* `test_engine_sdlc_planner.py::test_add_{new_scaffold_
  rejects_gate_fields,lab_candidate_requires_promote_new,readiness_miss_
  rejects,always_lands_LAB,leaves_frozen_literal_untouched}`.
  *Pinning test:* `test_add_always_lands_LAB` +
  `test_add_lab_candidate_requires_promote_new`.

- **T7 — MODIFY executor + zero-trust evidence re-verification +
  LAB→PAPER `promote` (H-S3-6).**
  *Modify:* `planner.py` — the MODIFY branch: re-derive from the T3
  sidecar (verdict/dsr/cred/recommended_exit==`fold_existing`/
  target_engine/identity-freshness/`PARAM_RANGES` membership/value
  match); assert `plan.sot_diff` **empty**; apply the validated
  `current→winning` diff to the engine's `default_params()` source
  (the O1 seam — line-anchored edit of the engine `backtest.py`
  default tokens, AST-validated). The automated gated `LAB→PAPER
  promote` edge (capital-gate/`graduation_ready` authority, no y/n).
  *Create test:* `test_engine_sdlc_planner.py::test_modify_rejects_
  {forged_numbers,wrong_target,non_param_ranges_key,value_mismatch,
  stale_sidecar}`, `::test_modify_plan_sot_diff_is_always_empty`,
  `::test_promote_flips_lab_to_paper_iff_gate_green`.
  *Pinning test:* `test_modify_plan_sot_diff_is_always_empty` +
  `test_modify_rejects_forged_numbers`.

- **T8 — `ops/engine_sdlc/__main__.py` CLI + audit emit
  (H-S3-7, H-S3-12).**
  *Create:* `ops/engine_sdlc/__main__.py` (`python -m ops.engine_sdlc`,
  separate OS process, never wired to a daemon; mirrors
  `ops/lab/__main__.py::_amain` shape): parse → classify → validate →
  render diff → (ADD/REMOVE) explicit TTY `y`/`yes` gate, fail-closed
  on anything else/EOF/non-TTY → apply; (MODIFY/promote) automated
  apply + done-receipt; `ENGINE_CHANGE_REQUEST` `application_log` emit
  on **every** terminal outcome; explicit non-zero, never silent 0.
  *Create test:* `tpcore/tests/test_engine_sdlc_cli.py::test_{parse_
  fail_rc1,reject_rc1,non_y_declines_zero_mutation,eof_declines,
  rejected_plan_never_prompts,apply_rc0,every_outcome_emits_audit,no_
  args_rc_nonzero}`.
  *Pinning test:* `test_non_y_declines_zero_mutation` +
  `test_every_outcome_emits_audit`.

- **T9 — Suite-level proof + lane/scope gate (H-S3-10).**
  *No new behaviour.* Run, and pin green: `ruff check`,
  `python -m tpcore.scripts.check_imports tpcore <every live engine>`
  (tpcore∌engine still clean — SP3 added zero tpcore→engine import),
  the full SP1 `test_engine_lifecycle_consistency.py` (incl. the T5
  extensions), the SP2 T1 oracle + `test_lab_isolation.py`
  (both unchanged-green), every new SP3 test, `bash -n` on any new/
  edited wrapper scripts. *Create:* a scope-diff assertion
  (`scripts/tests/` or a doc check) that the SP3 change set is confined
  to the §8 net-new surface + the enumerated in-place extends — **no**
  `CLAUDE.md`/`OPERATIONS.md`/`glossary.md`/data-lane SoT touched
  (SP4/data-lane boundary).
  *Pinning test:* the green full suite + the scope-diff assertion.

> **Ordering invariants (TDD-correct, reviewer-uncatchable hazards
> pre-empted):** (i) the archive-leg clockwork extension (H-S3-5) lands
> **with** the REMOVE executor (T5), never earlier — landing it before
> T5 would red-fail an honest build with no producer of a clean retire
> (a behavior's pinning test ships with the behavior); (ii) the JSON
> sidecar (T3) lands **before** the executors that consume it (T5–T7)
> — the planner's sidecar reader has no producer until T3; (iii) the O1
> `_build_lab_result` wiring (T1) and the pool threading (T2) each
> re-run the **unchanged** SP2 T1 oracle / `test_lab_isolation.py` as
> their own gate, so an SP2 regression fails *that* task, not T9; (iv)
> every SP3 test file is in `tpcore/tests/` with **lazy** in-body
> `ops.engine_sdlc` imports — the `scripts/ops.py`↔`ops/` `sys.modules`
> collision (SP2 T9/T10's bite) cannot occur at collection; (v) `apply`
> never runs git and never enables trading — the consistency subprocess
> is the only gate, the operator commits separately (R2 accepted).

---

## 12. Open items needing an OPERATOR decision

None are design-blocking. There are no open expert-default items: the
one *risk-acceptance* axis has been operator-decided.

- **MODIFY approval class — OPERATOR-CONFIRMED (2026-05-18):
  automated-if-it-passes-the-DSR≥0.95/credibility≥60-gate.** This is
  **no longer an expert default** — the operator explicitly chose
  automated-if-gated over the y/n option **and** over the
  live-only-hybrid (recorded 2026-05-18; matches the §6.2 / §6
  approval-class-table decision, which stands as operator-confirmed).
  The full §6.2 justification + the H-S3-6 zero-trust guardrail + the
  paper-only backstop bound the residual (R1, §11.2). The documented
  one-line escape-hatch is retained: the operator may, with a single
  policy flip in the §6 approval-class table (a config change, **not**
  a redesign), elect to hold a specific MODIFY for y/n. **Decision is
  operator-owned and final; SP3 proceeds automated-if-gated.**

---

*End of SP3 design. `§11 Hardening` (H-S3-* register + §11A T0–T9 TDD
decomposition + §11.2 residuals + §11.3 fixed defects) completed by the
adversarial expert-harden pass 2026-05-18 — ready for writing-plans.*
