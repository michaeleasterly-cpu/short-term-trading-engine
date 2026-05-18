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
it stages every SoT edit + filesystem op, runs the full
`test_engine_lifecycle_consistency.py` suite **in-process against the
staged tree**, and commits the staged changes to disk **only if the
suite is green**. A red suite ⇒ roll back the staging, emit the
rejection, mutate nothing. (Symmetric to the data lane's "the prepared
diff must pass every invariant test *before* you are asked to
approve.") Git is **not** touched by the executor — it stages working-
tree changes only; committing is the operator's separate, normal step
(honors the standing "tests never touch the real repo" / executor never
runs git rule).

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
   from the cited dossier / on-record data, not read from the ECR.
2. **Dry consistency run**: build the *staged* `_PROFILE` + staged
   filesystem in a temp overlay; run the full
   `test_engine_lifecycle_consistency.py` against the overlay
   (parametrised to accept an injected profile dict + repo root — SP1's
   `_roster_sorted(profiles=…)` already supports an injected dict; SP3
   threads the same seam through the test helpers without changing the
   default-call behaviour the existing assertions pin).
3. **Readiness gate** (ADD only): run the programmatically-checkable
   `engine_readiness.md` items against the scaffolded package.
4. Any failure ⇒ `plan.rejection` is set; the CLI prints the exact
   reason and exits non-zero. **Nothing is mutated.**

### 5.3 `apply(plan)` — atomic-or-abort

Stage all edits (the `_PROFILE` source rewrite, the package move/
scaffold, the shadow-file region edits, the EULOGY render) into the
working tree; re-run the full consistency suite in-process against the
now-on-disk staged state; if green, leave it (the operator commits);
if red, **revert every staged change** (the executor records the exact
pre-state of every file it touched and restores it) and emit the
rejection. No partial application is ever left on disk.

### 5.4 Evidence is re-verified, never trusted

The ECR carries `gate_dsr`/`gate_cred`/`param_change` for *operator
legibility* (so the human sees what they are approving), but the
planner **recomputes every one** from the authoritative source:

- ADD(lab_candidate) / MODIFY: re-read + re-parse the cited
  `lab_dossier` markdown (it is the rendered form of the frozen
  `LabResult`); re-verify verdict == SURVIVED, DSR ≥ 0.95, credibility
  ≥ 60, `recommended_exit` matches the action, `target_engine` matches.
  A dossier whose numbers disagree with the ECR text ⇒ **reject for
  evidence mismatch** (a forged/stale ECR cannot get past the gate).
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
| Lifecycle SoT + states | `tpcore.engine_profile._PROFILE`, `LifecycleState`, `Cadence`, `_roster_sorted(profiles=…)` injectable seam | — (SP3 only *edits the literal*) |
| SoT-derived shadows | `roster_for_dispatch()`, `allocator_eligible_engines()`, `archived_engines()`, `engine_package_names()` — all auto-derive | — (proven: snap-out = one literal flip) |
| Consistency clockwork | `tpcore/tests/test_engine_lifecycle_consistency.py` (all SP1 legs) | **complete the partial archive leg** (§4.2.2) |
| Lab → SP3 contract | `LabResult`, `LabCandidate`, `ParamDelta`, `recommended_exit`, the dossier markdown (`ops/lab/dossier.py`) | the ECR-side dossier *re-parser/verifier* |
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
`credibility_pool` threading. **No new tpcore module; no new
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

> *Placeholder — filled by a separate expert-harden pass with `H-S3-*`
> hardening IDs and the `T0…Tn` task decomposition for the
> writing-plans pass. The decomposition outline proposed to that pass:*
>
> - **T0** — `ops/engine_sdlc/` package skeleton + `EngineChangeRequest`
>   model + `parse_ecr` (strict, reject-on-unknown) + the
>   `engine_change_request.md` checklist; unit tests on parse/reject.
> - **T1** — O1: per-engine `default_params()` in the 3 `backtest.py`
>   files + `ops/engine_sdlc/default_params.py` dispatcher + the
>   parity test; wire `_build_lab_result` (remove the `# TODO(SP3)`).
> - **T2** — the `credibility_pool` threading in `_run_lab_core` +
>   test that it is the only RW handle under an active `LabContext`
>   (legacy path byte-identical — SP2 oracle still green).
> - **T3** — `classify()` + `TransitionPlan` + the closed
>   classification table; pure-function tests for every cell incl.
>   every rejection.
> - **T4** — `validate()` incl. the dry in-overlay consistency run
>   (thread the injectable-profile/repo-root seam through the test
>   helpers without changing default-call behaviour).
> - **T5** — REMOVE executor + `tpcore/templates/eulogy_template.md`
>   + the completed archive-leg clockwork (§4.2.2); a synthetic
>   end-to-end retire of a throwaway fixture engine in a temp tree.
> - **T6** — ADD executor (new_scaffold + lab_candidate) incl. the
>   engine-readiness build gate invocation.
> - **T7** — MODIFY executor + zero-trust evidence re-verification.
> - **T8** — `ops/engine_sdlc/__main__.py` CLI (prepare→validate→
>   binary y/n for ADD/REMOVE; automated receipt for MODIFY/promote;
>   explicit non-zero, never silent 0) + `ENGINE_CHANGE_REQUEST`
>   audit emit.
> - **T9** — full-suite green pass: ruff, check_imports
>   (tpcore∌engine still clean — SP3 added no tpcore→engine import),
>   the SP1 N-way clockwork, the SP2 Lab oracle, the new SP3 tests;
>   wrapper scripts `bash -n` clean.

---

## 12. Open items needing an OPERATOR decision

None are design-blocking. The one item that is a genuine
*risk-acceptance* (not design) call, recorded with the expert's default
so SP3 is not blocked:

- **MODIFY approval class.** Defaulted to **automated-if-gated** with
  the full §6.2 justification + the zero-trust guardrail. This is the
  expert's call per the lane's "delegate design to the expert" rule;
  it is surfaced here only because it is the one place the dispatch
  brief flagged as a legitimate operator *risk-acceptance* axis. The
  operator may, with a one-line policy flip (not a redesign), elect to
  hold MODIFY for y/n — the spec is built so that is a config of the
  approval-class table, not an architectural change. **Default stands;
  SP3 proceeds automated-if-gated.**

---

*End of SP3 design. `§11 Hardening` to be filled by the expert-harden
pass (H-S3-* + T0–Tn) before writing-plans.*
