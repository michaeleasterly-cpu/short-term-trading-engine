# Engine SDLC — Design (the canonical spec)

**Status:** approved design 2026-05-18 (ENGINE lane). **Epic:** Engine
SDLC — the operator-approved 4-sub-project chain **SP1 Roster SoT
(#57)** → **SP2 The Lab (#68)** → **SP3 Engine Change Request +
lifecycle transitions (#81)** → **SP4 (this) comprehensive SDLC docs +
non-Python shadow-manifest closure**. SP1/SP2/SP3 are all **merged on
`main`** (`86c65c1`); SP4 is FORMALIZE-AND-DOCUMENT plus ONE genuinely
new build (the shadow-manifest generator + CI-divergence gate). This
spec is the single authoritative description of the now-shipped Engine
SDLC.

Brainstorm → **spec (this doc)** → expert-harden (§14, now filled
with the H-S4-\* register + the final T0–Tn decomposition + the one
design defect DDF-1 the pass surfaced) → writing-plans →
subagent-driven build.

**Symmetry reference (READ-ONLY, NOT cloned):** the data-domain analog
`docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`
and `docs/superpowers/checklists/data_feed_change_request.md`. SP4 is
the engine-domain SDLC; the data SDLC is referenced for *symmetry of
approach* (the flat-SoT registry pattern, the structured change
request, the operator ADD/REMOVE-only policy, the N-way
half-state-fails-CI test). It is **adapted, never transcribed** — the
engine domain diverges materially (§9 ledger is binding/authoritative).

---

## 1. Problem & Goal

### 1.1 Problem

An *engine* is a first-class entity that is born (built/scaffolded),
validated (backtested → DSR/credibility gate), promoted (paper, then
one day live), and eventually retired (archived with an EULOGY). For
years this lifecycle existed only as tribal knowledge spread across
`engine_readiness.md`, the graduation gate, ad-hoc scaffolding, and the
Sigma-archival cleanup. The **Sigma archival (#170)** is the motivating
incident: removing one engine drifted across ~10–22 sites (rosters,
importers, the smoke loop, `pyproject`, docstrings, EULOGY) before a
cohesive cleanup pass — the exact "intended state change to a
first-class entity, applied ad hoc, leaving half-state" class the data
lane independently hit and solved.

SP1→SP3 **shipped the machinery**: the unified roster SoT, The Lab,
the Engine Change Request + deterministic transition executor, and the
N-way consistency clockwork. **Two gaps remain, and they are exactly
SP4's scope:**

1. **No canonical SDLC document.** There is no single authoritative
   description of the engine lifecycle — the data lane has one
   (`2026-05-17-data-provider-lifecycle-design.md`); the engine lane's
   knowledge is scattered across the SP1/SP2/SP3 specs (design docs,
   not the operator-facing canonical reference) and stale prose in
   `CLAUDE.md` / `OPERATIONS.md` / `glossary.md`.
2. **The non-Python shadows are drift-DETECTED only, never
   regenerated.** SP1 made `tpcore.engine_profile` the SoT and *derived*
   every Python shadow, but explicitly deferred to SP4 (SP1 §4 /
   H-B6): the structurally-parseable non-Python shadows
   (`scripts/run_smoke_test.sh` step-3 loop, `pyproject.toml`
   testpaths + `packages.find.include`, the frozen-literal tuple) got a
   **drift-detection test** but no regeneration; the *prose-only*
   shadows (`scripts/run_all_engines.sh` docstring,
   `ops/platform_pipeline.py` docstring) got **nothing** (regex over
   English prose is brittle — SP1 H-B6 deferred them whole). And the
   SP1 H-B6 reverse assertion (`roster_for_dispatch() ⊆
   ENGINE_TABLES`) was left **visible-but-deferred** at the test site
   (`test_engine_lifecycle_consistency.py:112-113`).

### 1.2 Goal

Deliver, on a single CI-green-mergeable branch to `main`:

1. **This spec** — the canonical Engine SDLC reference, engine-domain
   first, with a binding §9 symmetry/divergence ledger vs the data
   SDLC.
2. **Doc closure** — `CLAUDE.md`, `docs/OPERATIONS.md`,
   `docs/superpowers/checklists/engine_readiness.md`,
   `docs/glossary.md` updated to describe the *shipped* SDLC accurately
   (verified against the real code, never aspirational).
3. **The non-Python shadow-manifest generator + CI-divergence gate** —
   a single generator that regenerates *every* non-Python shadow from
   the `tpcore.engine_profile` SoT, with a `--check` mode that FAILS CI
   on divergence, closing the SP1 H-B6 carry-forward (incl. the reverse
   `roster ⊆ ENGINE_TABLES` assertion) so a silent roster/shadow drift
   is ungameable.
4. **An honest known-limitations section** recording the SP3
   carry-forwards (`_ENGINE_DEFAULT_CONSTS` reversion-only;
   `_validate_modify` `type(want)(v)` bool footgun) and any other
   genuine gap, so the documentation is truthful, not aspirational.

**Non-goals (§13):** SP4 implements no engine, graduates none, enables
no live trading, touches no data-lane file, and does **not** fix the
SP3 carry-forwards (only records them).

---

## 2. Engine as a first-class entity + the roster SoT (SP1)

An **Engine** is a named strategy package (`reversion/`, `vector/`,
`momentum/`, `sentinel/`, `canary/`) with the five mandatory Plugs
(setup_detection, lifecycle_analysis, execution_risk, aar_logging,
capital_gate), a `scheduler`, a `backtest`, and a `tests/` dir. It is
**not** a free-floating concept: every engine that exists has exactly
one row in the single mechanically-enforced roster SoT.

**The SoT: `tpcore/engine_profile.py::_PROFILE`** — a frozen
pydantic-v2 `dict[str, EngineProfile]`. `EngineProfile` (frozen,
`extra="forbid"`) carries `engine`, `cadence` (`Cadence` StrEnum:
DAILY / MONTHLY_FIRST_TRADING_DAY / WEEKLY_FIRST_TRADING_DAY),
`dispatch_order`, `lifecycle_state` (§3), `market_closed_required`,
`allocator_eligible`. This is the engine-domain analog of the data
lane's flat-SoT pattern (`tpcore.feeds.FeedProfile`,
`tpcore.selfheal.HealSpec`, `tpcore.risk.limits_profile`,
`ProviderBinding`) — pydantic-v2, frozen, declarative.

**Derived accessors (the public read surface — never reach into
`_PROFILE` directly from a consumer):**

| Accessor | Returns | Consumer |
|---|---|---|
| `profile_for(engine)` | `EngineProfile \| None` | anyone needing one engine's cadence/state |
| `roster_for_dispatch()` | ordered tuple of PAPER/LIVE, non-allocator engines by `dispatch_order` | **the** authority for `ops.engine_dispatch.ROSTER` |
| `allocator_eligible_engines()` | the inverse-vol pool (allocator_eligible subset) | the allocator's `engines=` default |
| `archived_engines()` | RETIRED engine names (sorted) | `engine = ANY($1::text[])` provenance query |
| `engine_package_names()` | non-RETIRED, non-allocator package dirs (frozenset) | `tpcore.scripts.check_imports.ENGINE_PACKAGES` (the tpcore∌engine layering invariant) |
| `cadence_window_start(engine, now)` | start (UTC) of the cadence cycle | the should_fire "already ran this cycle" guard |
| `should_fire(engine, now, pool)` | fail-CLOSED `FireDecision` | `ops.engine_dispatch` (the event-driven trade trigger) |

**SP1 invariants preserved (SP4 documents them, changes none):**
`roster_for_dispatch() == ("reversion","vector","momentum","sentinel",
"canary")` (the frozen-literal pin — roster-order changes are high-risk
and must be explicit); `allocator` is structurally separate (its own
`_dispatch_allocator` path, never in the ROSTER loop, never a top-level
package); every *Python* shadow is hard-derived (no drift possible);
the non-Python shadows had drift-detection only (SP4 closes this — §10).

---

## 3. The lifecycle states & transitions (SP3)

### 3.1 States — `LifecycleState` StrEnum

| State | Meaning | Dispatched? | Allocator? |
|---|---|---|---|
| `LAB` | candidate / experiment; SP2 territory | NO | NO |
| `PAPER` | graduated, paper-trading (the current reality for all five live engines) | YES | iff `allocator_eligible` |
| `LIVE` | reserved; **no engine here yet** (paper-only mandate stands) | YES | iff `allocator_eligible` |
| `RETIRED` | snap-out complete; archive/EULOGY exists; never dispatched | NO | NO |

`_DISPATCHABLE = frozenset({PAPER, LIVE})` — the single gate that
filters `roster_for_dispatch()` / `allocator_eligible_engines()` and
the `should_fire` lifecycle guard. LAB and RETIRED engines are
**filtered out of every dispatch/allocator accessor by derivation** —
their `cadence`/`dispatch_order` are inert placeholders, never
consumed. The `_PROFILE` carries durable sentinels proving each
non-{PAPER} state is real and exercised: `sigma` (RETIRED) and `lab`
(LAB) — neither is a runnable engine (no package/scheduler); both are
asserted absent from dispatch by the clockwork (§7).

### 3.2 The transition state machine — `ops/engine_sdlc/planner.py`

`classify(ecr, profile_snapshot) -> TransitionPlan` is **pure** (a
read-only snapshot in, a frozen plan out — NO I/O, NO `_PROFILE`
mutation). It maps `(action, in-profile?, from_state, source)` to the
single defined edge or a **typed rejection** — the table is TOTAL and
CLOSED (any cell not defined is a typed rejection, never an inferred
edge):

| Action | Pre-state | Edge | Approval |
|---|---|---|---|
| **ADD** | absent | `∅ → LAB` (ADD **always** lands LAB, never PAPER — H-S3-11a) | OPERATOR (binary y/n) |
| **ADD** | present | reject ("already exists; MODIFY or REMOVE") | — |
| **REMOVE** | present, not RETIRED | `cur → RETIRED` | OPERATOR (binary y/n) |
| **REMOVE** | absent / already RETIRED | reject | — |
| **MODIFY** | present, not RETIRED | `cur → cur` (NO lifecycle edge — params only) | AUTOMATED (gated) |
| **MODIFY** | absent / RETIRED | reject | — |
| **promote** (not an ECR action) | LAB | `LAB → PAPER` | AUTOMATED (gated) |

`validate(plan, ecr)` is **reject-never-force**: ADD runs the
fail-closed readiness/evidence gate (new_scaffold must NOT carry a gate
score it has not earned; lab_candidate must present a SURVIVED,
DSR≥0.95, cred≥60, `recommended_exit=promote_new` sidecar that is
identity-fresh to the cited dossier); MODIFY runs the H-S3-6 zero-trust
re-derivation (every gate number re-read from the **frozen LabResult
JSON sidecar**, never the ECR text / rendered markdown); REMOVE has no
gate (you may always stop). Then — for **every mutating action** — the
spec-mandated **pre-approval isolated dry consistency run**: copytree
the worktree into an ephemeral temp tree, stage the *exact* edits
`apply()` would write, and run the **REAL**
`test_engine_lifecycle_consistency.py` clockwork as a fresh subprocess
with `cwd=` the temp tree. A red dry run is a hard reject; the operator
only ever confirms a green-validated diff (the CLI never fabricates
GREEN).

`apply(plan)` is **atomic-or-abort** (H-S3-4): a `_Journal` records
every touched file's prior bytes / every per-item move *before* it is
performed; text edits first, the package `shutil.move` last; re-run the
on-disk clockwork as a fresh subprocess; green ⇒ leave it (the operator
commits with normal git — the executor NEVER runs git); red OR any
exception ⇒ reverse-order restore to **byte-identical**, set the typed
rejection, emit the audit. A failed transition leaves ZERO trace.

`promote(engine)` is **LAB→PAPER, automated, gated, NOT an ECR
action** — it flips iff the capital-gate/`graduation_ready` authority
is green, reusing the same `_Journal` byte-identical-rollback +
`_rewrite_profile_source` (the ONLY `_PROFILE` editor) +
clockwork-subprocess discipline. A promote without a resolved gate
verdict is a hard reject (never a silent flip — zero-trust parity with
the MODIFY gate).

---

## 4. The Lab (SP2) — the LAB state, made operable

The Lab is the engine-domain capability that has **no data analog**
(§9): an isolated, concurrent, shadow/candidate backtest harness for
hunting parameter edges *without touching the live platform*. It is the
operable form of `LifecycleState.LAB`.

- **Entrypoint:** `python -m ops.lab --candidate <name> --target-engine
  {reversion|vector|momentum} --intent {promote_new|fold_existing}
  [--param-overrides JSON] [--trials N] [--seed S] …`. A separate OS
  process, operator-driven, **NEVER wired into any daemon / dispatch /
  engine_service** (§6 concurrency-with-live safety). No-DSN resolves
  to an explicit non-zero rc + a logged error — never a silent 0.
- **Isolation contract — `tpcore/lab/context.py::LabContext`:** an
  async CM that (a) forces the server pool **read-only** while a Lab
  run is active (`build_asyncpg_pool` honors `lab_is_active()`),
  (b) provides the **single allowlisted RW credibility pool** (built
  *before* the active flag is set so it stays the one intentional RW
  exception — the credibility append), and (c) installs a fail-closed
  reentrancy guard (`assert_not_in_lab()`) at every live-side-effect
  boundary (risk / aar / order / broker / startup). A live side-effect
  class constructed inside an active Lab run raises
  `LabIsolationViolation`. Not reentrant by contract (a single
  sweep-level CM).
- **Output — the two-exit graduation dossier
  (`ops/lab/dossier.py`):** a rendered `docs/lab/{day}-{candidate}-
  {verdict}-seed{seed}.md` PLUS a byte-frozen `.json` sidecar
  (`LabResult.model_dump_json()`, deterministic field order). The
  sidecar is the **machine-readable evidence** the SP3 automated-MODIFY
  / ADD-lab_candidate gate re-derives every number from (H-S3-9 — never
  scrape rendered markdown). The dossier recommends a next step
  (`promote_new` → ADD a new engine; `fold_existing` → MODIFY the
  target; `none` → iterate) but **SP2 never applies it** — SP3's ECR
  does, gated.
- **Credibility namespace:** Lab runs persist credibility under the
  `lab.<candidate>` namespace (SP2 invariant — never the live engine's
  namespace), via the single allowlisted RW pool only.

---

## 5. The graduation gate (DSR ≥ 0.95 ∧ credibility ≥ 60)

The structural defense against overfit — the engine-domain analog of
the data lane's data-parity cutover gate (§9: same *role*, different
*mechanism*). An engine candidate may not advance into the live roster
unless its walk-forward + held-back evidence clears **both**:

- **DSR ≥ 0.95** — the Deflated Sharpe Ratio (n_trials-deflated;
  `tpcore.backtest.credibility`), the binding constraint today (all
  five live engines produce positive OOS edge candidates 0.78–1.26 but
  every one currently FAILS this gate — the platform is honest about
  signal strength being the limiter).
- **credibility ≥ 60** — the credibility rubric score
  (`CredibilityScore`).

The gate is enforced in three load-bearing places, all reading the
*same* rubric, never re-deriving: (1) The Lab's verdict
(`LabResult.verdict == "SURVIVED"` iff both clear); (2) the SP3
`validate()` zero-trust re-derivation for ADD(lab_candidate) and
MODIFY (re-read from the frozen sidecar); (3) `promote()`'s
`graduation_ready` authority for LAB→PAPER. **`canary` is the one
documented compliance deviation** — non-graduating by construction
(spec §4b), it never calls `write_credibility_score` and is
allocator-excluded by omission; the SP1 clockwork accommodates it
(it is PAPER + wired but never expected to clear a gate).

---

## 6. The Engine Change Request — the operator interface (SP3)

### 6.1 The single structured touchpoint

`docs/superpowers/checklists/engine_change_request.md` carries the
frozen `ECR` wire-format block. The operator fills it and runs
`python -m ops.engine_sdlc --ecr <file>` (or `--promote <engine>`).
The operator **never hand-edits** `_PROFILE`, the smoke loop,
`pyproject`, the frozen-literal, or an EULOGY — that is exactly how the
Sigma 22-site drift happened. `ops/engine_sdlc/ecr.py::parse_ecr` is
the single strict entry point: a request that does not parse is
rejected with the EXACT reason (duplicate key, unknown key, stray
action-field — `extra=forbid` at both the parser and the pydantic
model), never best-effort-interpreted.

### 6.2 Operator-interaction policy (AUTHORITATIVE)

Identical *shape* to the data lane's policy (§9 ADOPT), engine-specific
content:

> The operator approves **exactly two** operations: **ADD** an engine
> (new scaffold or Lab-graduated) and **REMOVE** one (retire/archive) —
> a binary **APPROVE? (y/n)** on a *proven-consistent, dry-run-green*
> diff. Everything reversible and gate-verified — a **MODIFY**
> (re-tuned params that already passed DSR≥0.95 ∧ credibility≥60) and
> a **LAB→PAPER promote** the capital gate already cleared — is
> **automated, deterministic, no operator approval**. A request that
> cannot produce a consistent diff is **rejected with the exact reason
> — never handed to the operator to force**.

The y/n is **fail-closed** (H-S3-7a): non-interactive stdin / EOF /
anything not exactly `y`/`yes` ⇒ declined, nothing changed, audit
emitted. Every terminal outcome (rejected / operator_declined /
applied / apply_restore_failed) emits one
`platform.application_log` `ENGINE_CHANGE_REQUEST` row (DB-best-effort:
a missing `DATABASE_URL` logs + returns — the executor is an on-demand
tool, never on the trade path). Explicit non-zero rc, never a silent 0
(the canary `-m`-no-op lesson).

### 6.3 The snap-out (REMOVE) — archive + EULOGY, a physical move

A REMOVE is the engine-domain "snap-out": `cur → RETIRED` in `_PROFILE`
(via the AST-validated single-entry rewrite); `ENGINE_TABLES` orphan
removed (the documented D-SDLC1-1 seam); the two structurally-parseable
shadows purged; the frozen-literal rewritten *iff* the roster changed
(same staged diff — never a hand-edit); the package **CONTENTS
physically moved** to `archive/<engine>/` (journaled per-item for
reversibility); an EULOGY rendered from `tpcore/templates/
eulogy_template.md` (the operator's `reason`/`eulogy_notes` threaded
in). This is a **physical move + state flip**, NOT a pure status flip
(§9 DIVERGE — the data lane's RETIRE keeps the provider for provenance;
an engine's *code* is relocated). The EULOGY is a real artifact: the
clockwork enforces a non-empty `## Cause of death` AND `## Retirement
checklist` (a stub fails CI — the analog of the data-lane fake-healable
HealSpec).

---

## 7. The consistency clockwork (SP1, extended by SP3)

`tpcore/tests/test_engine_lifecycle_consistency.py` is the
engine-domain analog of `test_provider_lifecycle_consistency.py`: an
**N-way** half-state-fails-CI oracle. A new, removed, or archived
engine fails the build unless it is coherently wired or fully
offboarded *in the same change* (exactly as the data 3-way does for
feeds). Its legs (the shipped reality SP4 documents accurately):

1. `test_dispatch_order_invariant_is_the_frozen_literal` — the roster
   tuple is the frozen literal (roster-order changes are explicit/
   high-risk).
2. `test_live_engine_is_wired` — a PAPER/LIVE engine must have a
   top-level `<name>/` package + `<name>/tests/` + an importable
   `<name>.scheduler` (allocator exempt — separate path).
3. `test_retired_engine_fully_offboarded` — RETIRED ⇒ absent from
   roster + allocator, present in `archived_engines()`,
   `archive/<name>/EULOGY.md` exists, `<name>/` package **gone**.
4. `test_no_half_state` — no RETIRED+allocator_eligible, no duplicate
   `dispatch_order` among non-RETIRED, `_PROFILE` key == `.engine`.
5. `test_engine_tables_keys_are_known_engines` — every `ENGINE_TABLES`
   key ⊆ the live roster ∪ `{allocator}`. **SP4 closes the reverse
   here** (§10.4).
6. `test_structurally_parseable_shadows_match_sot` — the
   `run_smoke_test.sh` step-3 loop + `pyproject` testpaths/include ==/⊇
   the SoT roster.
7. `test_lab_sentinel_is_not_wired` — the durable LAB sentinel proves
   LAB is real but is not runnable; LAB is the ONLY
   non-{PAPER,LIVE,RETIRED} state.
8. `test_retired_engine_eulogy_content_floor` (H-S3-5) — EULOGY
   sections non-empty.
9. `test_retired_engine_absent_from_structural_shadows` (H-S3-5) — the
   explicit RETIRED-absent assertion on the retire leg.
10. `test_no_orphan_archive` (H-S3-5) — every `archive/<dir>/EULOGY.md`
    maps to a RETIRED `_PROFILE` entry.
11. `test_retired_engine_not_importable_as_live` (H-S3-5) — a RETIRED
    `<name>.scheduler` is no longer importable.

**Relationship to SP4's new manifest gate (no redundancy — §10.5):**
the clockwork is a *consistency oracle* (does the committed state
cohere with the SoT?). SP4's manifest gate is a *regeneration
mechanism* (regenerate the shadows FROM the SoT; `--check` fails on
drift). The clockwork stays the oracle; the manifest gate adds
regeneration + extends coverage to the **prose-only** shadows SP1 H-B6
could not safely regex. SP4 does **not** add a second drift mechanism
for the structurally-parseable shadows the clockwork already pins —
instead the clockwork's structural-shadow assertions are **re-expressed
through the generator's `--check`** (one canonical mechanism; §10.5
specifies the exact dedup).

---

## 8. The build gate — `engine_readiness.md` (the ADD path)

A `new_scaffold` ADD is the *birth* of an engine. The 10-section
`docs/superpowers/checklists/engine_readiness.md` is the
**non-optional** build gate (§10 of it enumerates the six compliance
verifications the Sentinel 2026-05-15 audit surfaced: BaseEnginePlug on
every plug, FilterDiagnostics on signals, credibility write, trading-day
gate, classify_exit_reason, stale-order cancel). The SP3 ADD executor
machine-checks the programmatically-checkable subset
(`planner._check_readiness`: scaffold dir present, a BaseEnginePlug
class). Start every new engine from `tpcore/templates/engine_template/`
— the scaffold satisfies the gaps by construction. This has **no data
analog** (§9: a feed has no 10-section human-judgement build gate; its
ONBOARD is the 6-stage adapter contract — a different shape entirely).
SP4's doc-closure reconciles `engine_readiness.md` with the SDLC (the
ADD path's build gate, cross-referenced both ways).

---

## 9. Symmetry / divergence ledger vs the data SDLC (BINDING)

This ledger is **authoritative** — it governs how SP4 adapts the data
SDLC's *approach* without cloning its *content*. Engine-domain
correctness wins every conflict.

### 9.1 ADOPT (the data lane's pattern is the right shape — reuse it)

| Pattern (data) | Engine adaptation |
|---|---|
| Flat-SoT frozen pydantic registry (`ProviderBinding`) | `tpcore.engine_profile._PROFILE` / `EngineProfile` — same pattern, engine columns |
| `StrEnum` status (`ProviderStatus`) | `LifecycleState` StrEnum |
| Single structured change request, never hand-edit the registry | `engine_change_request.md` + `python -m ops.engine_sdlc` |
| Operator approves **ONLY ADD/REMOVE**; everything reversible+gated is automated | §6.2 — identical policy shape, engine content |
| N-way "half-state fails CI" consistency test | `test_engine_lifecycle_consistency.py` (11 legs) |
| Snap-in / snap-out checklist | `engine_readiness.md` (ADD build gate) + the REMOVE archive/EULOGY checklist |
| Generated-manifest discipline (a SoT change must regenerate or CI reds) | SP4's §10 shadow-manifest generator + `--check` gate |
| The change request prepares + validates the EXACT diff for a binary y/n | SP3 `validate()` pre-approval dry-run; the operator confirms a green diff |

### 9.2 DIVERGE (the engine domain is materially different — do NOT
mirror)

| Data SDLC | Engine SDLC | Why it diverges |
|---|---|---|
| **Data-parity gate** (candidate ≥ incumbent coverage/freshness/accuracy over an overlap window) | **DSR ≥ 0.95 ∧ credibility ≥ 60** graduation gate | An engine has no "incumbent serving the same need" to run alongside; its quality is an *absolute* overfit-deflated statistic, not a *relative* parity. Same role (the structural anti-degradation gate), different mechanism. |
| (no analog) | **The Lab** (isolated concurrent candidate backtest; the LAB state) | A feed has no "experiment a candidate in isolation" stage — EVALUATE runs the real candidate adapter against live data. The Lab is engine-unique. |
| ACTIVE (serving now) | **PAPER → LIVE** | An engine graduates *paper-first*, then (one day) live — a maturity ladder a feed lacks (a feed is either serving or not). LIVE is reserved (paper-only mandate). |
| ONBOARD = 6-stage adapter contract | ADD = the 10-section **`engine_readiness.md`** build gate | Different artifact, different shape (human-judgement build readiness vs the ingest/test/validate/dashboard/schedule/self-heal adapter contract). |
| RETIRE = status flip; provider kept for provenance | REMOVE = **physical `archive/` move + EULOGY** + state flip | An engine *is* its strategy code; retiring it relocates the code (provenance is the archived package + EULOGY), not a row left in place. |
| **CUTOVER** (swap the provider behind a feed; consumers unchanged) | **NO CUTOVER analog** | An engine *is* its strategy — there is no "same engine, different implementation behind a stable interface". Engine replacement = **REMOVE-then-ADD**, two operator-gated decisions, never an automated swap. |
| EVALUATE / CUTOVER / self-heal automated | MODIFY / promote automated; ADD/REMOVE operator | Same *policy* (only existence is operator-gated), but the automated set differs because the operations differ. |

### 9.3 Net

SP4 adopts the data lane's **structural disciplines** (flat SoT,
structured CR, operator ADD/REMOVE-only, N-way CI, generated-manifest)
and **diverges on every domain mechanism** (graduation not parity, The
Lab has no analog, paper→live, the build gate, the physical archive,
no CUTOVER). The §1 of this spec, the state machine (§3), and the
gates (§5/§8) are written **engine-first**; the data spec is cited only
where the *approach* is shared.

---

## 10. The non-Python shadow-manifest generator + CI-divergence gate (NEW)

This is SP4's one genuinely new build — closing the SP1 H-B6
carry-forward so every non-Python shadow is **regenerated from the
SoT** and a silent drift is **ungameable**.

### 10.1 The shadow inventory (verified against the shipped tree)

| Shadow | Today | Kind | SP4 action |
|---|---|---|---|
| `scripts/run_smoke_test.sh:51` step-3 `for engine in … ; do` loop | drift-DETECTED only (clockwork leg 6) | structurally-parseable, **behavioral** | generate + `--check` |
| `pyproject.toml` `[tool.pytest] testpaths` engine `<e>/tests` rows | drift-DETECTED only (clockwork leg 6) | structurally-parseable, **behavioral** | generate + `--check` |
| `pyproject.toml` `[tool.setuptools.packages.find] include` `"<e>*"` globs | drift-DETECTED only (clockwork leg 6) | structurally-parseable, **behavioral** | generate + `--check` |
| `scripts/run_smoke_test.sh:7-8` docstring engine listing | nothing (SP1 H-B6 prose-deferred) | prose | generate + `--check` |
| `scripts/run_all_engines.sh:10` docstring "Engines dispatched: …" | nothing (SP1 H-B6 prose-deferred) | prose | generate + `--check` |
| `ops/platform_pipeline.py:13-14` docstring engine listing | nothing (SP1 H-B6 prose-deferred) | prose | generate + `--check` |
| `roster_for_dispatch() ⊆ ENGINE_TABLES` reverse assertion | visible-but-deferred (clockwork leg 5 comment) | Python invariant | close (§10.4) |

`ops.engine_dispatch.ROSTER` is **already** `roster_for_dispatch()` —
NOT a shadow (hard-derived in SP1); `run_all_engines.sh` is a thin
`exec` of `ops.engine_dispatch` with no engine loop. The frozen-literal
tuple is rewritten by the SP3 executor in the staged diff (not a
generator concern — it is a *test pin*, not a shadow of the live
roster; the clockwork owns it).

### 10.2 Mechanism — sentinel-delimited generated regions + a single
generator

**Decision (expert call):** the cleanest, ungameable mechanism is a
**single generator module that owns sentinel-delimited regions** inside
the shadow files, with a `--check` mode that regenerates in-memory and
diffs against the committed bytes.

- **Where it lives:** `scripts/gen_engine_manifest.py` (the generator
  is a build/repo tool — `scripts/`, not `tpcore/`; it imports
  `tpcore.engine_profile` for the SoT but is not itself shared
  library, and it edits files under `scripts/`/`pyproject.toml`/`ops/`
  which a `tpcore` module must not). It is the engine-domain analog of
  the data lane's generated-manifest discipline. It must NOT import any
  engine (it only needs `roster_for_dispatch()` / `archived_engines()`
  — pure SoT reads; preserves tpcore∌engine by never importing an
  engine package).
- **How a region is marked:** each generated span is fenced by
  language-appropriate sentinel comments, e.g.
  `# >>> engine-manifest:smoke-loop (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>`
  … `# <<< engine-manifest:smoke-loop <<<`. The generator rewrites
  **only** the bytes between a matched sentinel pair; everything else
  is untouched. This kills the "fragile sed" problem (SP4 scope
  question): the bash `for engine in` loop becomes a single
  generated line inside a sentinel region — the generator emits
  `for engine in reversion vector momentum sentinel canary; do`
  verbatim from `roster_for_dispatch()`; no regex-replace of
  live-but-unfenced text.
- **Prose shadows:** the docstring "Engines dispatched: a → b → c"
  lines are likewise fenced (a Python `# >>> … >>>` pair inside the
  module docstring region, or — for `run_all_engines.sh` /
  `run_smoke_test.sh` headers — a `#`-comment sentinel pair). The
  generator emits the canonical join (` → ` for the dispatch order,
  `, ` for the smoke listing) from the SoT. SP1 H-B6 rejected a
  *regex over arbitrary prose*; SP4's answer is **not** a regex —
  it is a **fenced region the generator owns**, so the prose is no
  longer arbitrary (the brittleness SP1 named is structurally
  removed).
- **`pyproject.toml`:** the `testpaths` engine rows and the
  `packages.find.include` engine globs are each a sentinel-fenced
  span. TOML comments (`#`) are legal and ignored by `tomllib`, so the
  committed file still parses; the generator rewrites only the fenced
  engine rows (the non-engine `testpaths` like `tests`,
  `tpcore/tests`, `scripts/tests` and the `"tpcore*"` include stay
  outside the fence, hand-owned).

### 10.3 The `--check` CI-divergence gate (ungameable)

`python scripts/gen_engine_manifest.py --check` regenerates every
fenced region **in memory** from the live `tpcore.engine_profile` SoT
and **diffs** it against the bytes on disk. Exit 0 iff every region is
byte-identical to its regeneration; exit non-zero with a unified diff
naming the drifted file/region otherwise. A `--write` (default) mode
rewrites the regions in place (the operator/ECR-executor runs this; it
is idempotent).

- **CI wiring:** a `scripts/tests/test_engine_manifest_in_sync.py`
  invokes `gen_engine_manifest.py --check` as a subprocess (or imports
  the pure regenerate-and-diff function — expert call at plan time;
  subprocess is the faithful CI shape) and asserts exit 0. This is the
  ungameable gate: a roster change that does not regenerate the
  shadows **fails the build** with the exact drifted region. Symmetric
  to the SP1 N-way clockwork and the data-SDLC generated-manifest
  discipline.
- **ECR integration:** the SP3 `apply()` already stages the
  structurally-parseable shadow edits inline (`_shadow_edit_remove` /
  the ADD `_PROFILE` insert). SP4 **does not duplicate that** — instead
  the SP3 executor's existing shadow edits are migrated to *call the
  generator* (or the generator's pure region-render function) so there
  is exactly **one** mechanism that knows how a shadow is shaped. The
  generator's `--write` is what the executor invokes for the shadow
  step; the pre-approval dry-run + post-apply clockwork then prove
  consistency. (Exact dedup is a plan-phase task; the *decision* is:
  one renderer, two callers — CI `--check` and the ECR executor.)

### 10.4 Closing the reverse `roster ⊆ ENGINE_TABLES` assertion

SP1 left `test_engine_tables_keys_are_known_engines` with only the
forward direction (`ENGINE_TABLES keys ⊆ live roster ∪ {allocator}`)
and an explicit `# SP4 will also assert the reverse` comment at
`test_engine_lifecycle_consistency.py:112-113`. SP4 adds the reverse
leg: **every live PAPER/LIVE engine that requires a data-dep entry must
have an `ENGINE_TABLES` row** — a live engine with no data-dep entry is
a half-state. The exact assertion shape (full `roster_for_dispatch() ⊆
set(ENGINE_TABLES)` vs. an allowlist for engines that legitimately have
no data dep, e.g. `canary`/`sentinel` if they carry no per-engine
table) is a plan-phase decision driven by reading the *actual*
`ENGINE_TABLES` contents — the spec mandates *closing the deferral*,
the precise predicate is grounded at implementation against the shipped
`capital_gate.ENGINE_TABLES` (do not assume; verify). It lands in the
**clockwork** (it is a Python invariant, not a regenerated shadow — it
belongs with leg 5, and the deferred comment is removed in the same
change).

### 10.5 No-redundancy contract (clockwork vs generator)

Two mechanisms must not both own "is shadow X in sync":

- **The generator (`--check`)** owns *regeneration + byte-identity* for
  **all** non-Python shadows (structurally-parseable AND prose).
- **The clockwork** keeps the legs that are *not* shadow-regeneration:
  the frozen-literal pin (leg 1), wired/offboarded structure (legs
  2–4, 7–11), and the **closed** `ENGINE_TABLES` two-way invariant
  (leg 5, §10.4).
- **Leg 6** (`test_structurally_parseable_shadows_match_sot`) is now
  redundant with the generator `--check` (which already covers the
  smoke-loop + pyproject, by byte-identity, *plus* the prose shadows
  leg 6 never touched). **Expert decision (binding, not a plan-phase
  choice): DELETE leg 6's body and replace it with a one-line
  delegation that asserts the manifest `--check` passes** (so the
  clockwork still *fails* if a structural shadow drifts — same
  diagnostic surface as today for any reader running the clockwork —
  but the *shaping/regeneration logic lives in exactly one place*, the
  generator). It is NOT kept as an independent parsed-roster
  assertion (that would be a second mechanism that can disagree with
  the generator's byte-identity verdict — the precise anti-pattern
  this contract forbids). The clockwork remains the
  *structure/lifecycle* oracle; the generator is the sole
  *shadow-shape/bytes* oracle; zero overlap, one shadow mechanism. The
  plan-phase task implements this exact decision (delegation vs.
  outright deletion-with-pointer is a cosmetic implementation detail,
  not a design choice — both honor "one mechanism").

---

## 11. Doc-closure deliverable (first-class operator deliverable)

Every doc that describes engine wiring/lifecycle gains specific,
**code-accurate** content (verified against `tpcore/engine_profile.py`,
`ops/engine_sdlc/*`, `ops/lab/*`,
`test_engine_lifecycle_consistency.py` — never aspirational). The
*content* is designed here; the actual edits are SP4 implementation
tasks (planned later).

### 11.1 `CLAUDE.md`

- **Architecture / conventions:** add an **Engine SDLC** entry — the
  lifecycle states (LAB→PAPER→LIVE→RETIRED), the roster SoT
  (`tpcore.engine_profile`), that all five live engines are PAPER, the
  durable `sigma`(RETIRED)/`lab`(LAB) sentinels.
- **Session Rules:** add the canonical operator commands —
  `python -m ops.engine_sdlc --ecr <file>` (the ECR: ADD/REMOVE
  binary y/n, MODIFY/promote automated) and `python -m ops.lab
  --candidate … --target-engine … --intent …` (The Lab — the SP2
  carry-forward Lab section, recommendation-only). State that engine
  roster/lifecycle changes go through the ECR **only** (never
  hand-edit `_PROFILE`/shadows — the Sigma-drift rule), symmetric to
  the existing data-feed-change-request rule.
- **Engine-build compliance shortlist:** cross-reference the SDLC ADD
  path → `engine_readiness.md`; note the manifest generator
  (`scripts/gen_engine_manifest.py`) — a roster change must regenerate
  or CI reds (the engine-domain analog of the data generated-manifest
  discipline already documented).
- Accuracy guard: state plainly that all five engines currently FAIL
  the DSR/credibility gate (signal strength is the binding constraint)
  — do not imply any engine has graduated.

### 11.2 `docs/OPERATIONS.md`

- **New "Engine SDLC" section:** the ECR workflow (`python -m
  ops.engine_sdlc`), the operator-interaction policy (ADD/REMOVE y/n;
  MODIFY/promote automated), the snap-out (REMOVE → archive/EULOGY),
  the consistency clockwork, the manifest gate.
- **The Lab runbook:** `python -m ops.lab` — args, the
  `docs/lab/<dossier>.md` + `.json` sidecar output, that it is
  recommendation-only / never auto-applies / never wired into a
  daemon, the isolation contract.
- **Replace the stale `scripts/search_parameters.py`-as-prod-entrypoint
  framing** flagged in the SP2 holistic (OPERATIONS.md currently
  references `scripts/run_*_search.sh` / `scripts/replay_history.py`
  as the parameter-search path — reframe: the canonical on-demand
  edge-hunt is now `python -m ops.lab`; `scripts/search_parameters.py`
  remains the *underlying* walk-forward engine the Lab wraps, NOT the
  operator entrypoint). Keep accurate: do not delete the search
  scripts' description if they still exist — reframe their role.
- Accuracy guard: verify the daemon/Railway sections are not
  contradicted (SDLC tools are on-demand, never daemon-wired — say so).

### 11.3 `docs/superpowers/checklists/engine_readiness.md`

- Add a header note tying it to the SDLC: this checklist **is** the
  ADD-path build gate (§8); a `new_scaffold` ADD via the ECR
  machine-checks the programmatically-checkable subset
  (`planner._check_readiness`), the rest is operator-verified before
  filing the ECR. Cross-link the SDLC spec and the ECR checklist
  (bidirectional). Reconcile any item that the SP3 executor now
  enforces (so the checklist says "enforced by the ECR" where true,
  "operator-verified" where not) — accurate to `planner._check_readiness`.

### 11.4 `docs/glossary.md`

Add engine-domain terms, symmetric in form to the existing **Data
Provider Lifecycle** / **ProviderBinding** / **Data Feed Change
Request** entries (so the glossary reads consistently across lanes):

- **Engine SDLC** — the lifecycle for trading engines; states
  LAB→PAPER→LIVE→RETIRED; spec
  `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`. Operator
  approves ONLY ADD/REMOVE; MODIFY/promote automated.
- **The Lab** — `python -m ops.lab`; isolated concurrent candidate
  backtest → two-exit graduation dossier (`docs/lab/…` + `.json`
  sidecar); recommendation-only; `lab.<candidate>` credibility
  namespace; isolation via `tpcore.lab.context.LabContext`.
- **Engine Change Request (ECR)** —
  `docs/superpowers/checklists/engine_change_request.md` +
  `python -m ops.engine_sdlc`; the single structured touchpoint; never
  hand-edit `_PROFILE`/shadows.
- **LifecycleState** — `tpcore.engine_profile.LifecycleState`
  StrEnum; `_DISPATCHABLE = {PAPER, LIVE}`.
- **promote** — the automated, gated LAB→PAPER transition (not an ECR
  action).
- **snap-out** — the REMOVE operation: state→RETIRED + physical
  `archive/<engine>/` move + EULOGY + shadow purge, atomic-or-abort.
- **engine roster SoT** — `tpcore.engine_profile._PROFILE`; the
  mechanically-enforced single source for what engines exist / in what
  order / cadence / lifecycle.
- **engine shadow-manifest** — `scripts/gen_engine_manifest.py`; the
  generator + `--check` CI-divergence gate that keeps the non-Python
  shadows in sync with the roster SoT.

---

## 12. Reused vs new ledger (explicit)

| Reused (compose/document — no rebuild) | New (this SP4) |
|---|---|
| `tpcore.engine_profile` SoT + accessors + `LifecycleState` (SP1) | This canonical SDLC spec |
| The N-way `test_engine_lifecycle_consistency.py` clockwork (SP1+SP3) | `scripts/gen_engine_manifest.py` — the shadow-manifest generator |
| The ECR mechanism + `planner`/`ecr`/`_evidence` transition executor (SP3) | `gen_engine_manifest.py --check` — the CI-divergence gate |
| The Lab (`ops/lab/*`, `tpcore/lab/*`) + `LabContext` isolation (SP2) | `scripts/tests/test_engine_manifest_in_sync.py` |
| `engine_change_request.md` / `engine_readiness.md` / `eulogy_template.md` | The reverse `roster ⊆ ENGINE_TABLES` clockwork leg (closing SP1 H-B6) |
| `tpcore.backtest.credibility` (DSR/credibility gate) | The doc-closure content in CLAUDE.md / OPERATIONS.md / engine_readiness.md / glossary.md |
| The data-SDLC spec/checklist (READ-ONLY symmetry reference) | The §9 symmetry/divergence ledger |
| `roster_for_dispatch()` already wiring `ops.engine_dispatch.ROSTER` | (the generator REPLACES drift-detection-only leg 6 with regeneration; one mechanism) |

~85% compose-and-document; the single new structural core is the
generator + `--check` gate (+ the one closed clockwork leg).

---

## 13. Non-goals / known-limitations / future-work

### 13.1 Non-goals (out of SP4 scope)

- **No engine built, graduated, or promoted.** SP4 is docs + the
  manifest mechanism; it drives no ECR.
- **No live trading.** The paper-only mandate stands; LIVE remains
  reserved.
- **No data-lane edit.** The data-SDLC spec/checklist/registry are
  READ-ONLY symmetry reference. The 8 data-lane-owned files
  (`tpcore/calendar.py`, `tpcore/risk/*`, `tpcore/risk/governor.py`,
  `ops/engine_supervisor.py`, `ops/engine_service.py`,
  `ops/engine_ladder.py`, `tpcore/supervisor_state.py`,
  `tpcore/trade_monitor.py`) are untouched.
- **No SP3 carry-forward fix** (only recorded — §13.2). SP4 does not
  extend `_ENGINE_DEFAULT_CONSTS` or change `_validate_modify`.
- **No should_fire / dispatch / DB / application_log behavior change.**

### 13.2 Known-limitations (honest, recorded — NOT fixed in SP4)

These are the SP3 carry-forwards. The documentation must state them so
it is truthful, not aspirational:

- **(a) MODIFY is reversion-only today.** `ops/engine_sdlc/planner.py::
  _ENGINE_DEFAULT_CONSTS` maps **only `reversion`** (`z_threshold` →
  `reversion/models.py:Z_SCORE_THRESHOLD`, plus
  `volume_climax_multiplier`/`max_hold_days`/`stop_pct` in
  `reversion/backtest.py`). A `vector` or `momentum` MODIFY is a
  **documented fail-loud reject** — `_apply_modify` raises `RuntimeError
  ("no MODIFY default-constant map for engine …")` and `apply()`'s
  except-leg restores byte-identical (so it fails *safely*, never
  silently mis-applies). (Note: `ops/engine_sdlc/default_params.py`
  *does* dispatch reversion/vector/momentum for the *read* side — the
  asymmetry is precisely that the **write** side
  (`_ENGINE_DEFAULT_CONSTS`) is reversion-only.) Future-work: a
  per-engine MODIFY-rollout that adds the `(file, const)` map for
  vector/momentum, verified against each engine's real source.
- **(b) `_validate_modify` `type(want)(v)` bool footgun.** The ECR
  param value-equality coerces the ECR string to the sidecar value's
  type via `type(want)(v)`. For `bool`, `bool("False") is True` —
  harmless **today** because every Lab-swept param in `PARAM_RANGES` is
  numeric (int/float), so the coercion is correct; it would mis-coerce
  a future *boolean* swept param. Future-work: a type-aware coercion
  (explicit bool parse) when the first boolean param enters
  `PARAM_RANGES`.
- **(c) The clockwork dry-run is O(repo) copytree.** SP3 accepted this
  (on-demand, not a daemon — `_staged_copytree` excludes
  `.git/.venv/__pycache__/backtests`). Recorded for honesty; not a SP4
  concern.

### 13.3 Future-work (explicitly deferred, not SP4)

- Per-engine MODIFY rollout (closes 13.2a).
- Type-aware ECR coercion (closes 13.2b).
- A LIVE-graduation operator gate (when an engine first clears DSR ≥
  0.95 ∧ cred ≥ 60 and the operator chooses paper→live) — out of every
  current SP's scope (paper-only mandate).

---

## 14. Hardening

This section is the binding output of the SP4 expert-harden pass. It
carries the **H-S4-\*** register (each: the risk → the precise
mitigation → where it is enforced) and the **final ordered T0–Tn TDD
decomposition** (each task self-contained, CI-green on its own, the
pinning test in the same task as its behavior, lane/collision-safe).
The hardening below is grounded against the *actually shipped* code
read during this pass (`ops/engine_sdlc/planner.py`,
`tpcore/tests/test_engine_sdlc_planner.py`,
`tpcore/tests/test_engine_lifecycle_consistency.py`,
`tpcore/engine_profile.py`, `tpcore/quality/validation/capital_gate.py`,
the four shadow files, `scripts/tests/test_lab_cli_entrypoint.py`,
`scripts/tests/test_sp3_scope_confined.py`) — not assumed.

### 14.1 Design defect found & fixed during this pass (DDF-1)

**DDF-1 — the SP3 synthetic-tree builder hand-edits the *un-fenced*
shadow byte-strings; T2 silently breaks the entire SP3 atomicity
suite.** `tpcore/tests/test_engine_sdlc_planner.py::
_make_synthetic_engine_tree` (and the per-test setup in
`test_apply_red_consistency_rolls_back_to_byte_identical` /
`test_apply_mid_move_loop_failure_byte_identical` etc.) constructs the
pre-REMOVE staged tree by `str.replace()` on the *exact current
un-fenced* shadow forms — literally
`"for engine in reversion vector momentum sentinel canary; do"` and
`'"canary*"]  # sigma archived 2026-05-16'`. The moment T2 wraps those
spans in sentinel fences, those byte-strings change, every
`.replace()` becomes a **silent no-op**, the synthetic tree's shadows
no longer carry `throwaway`, the pre-REMOVE staged clockwork goes RED,
and the **entire SP3 byte-identical-rollback suite fails** — a
cross-task regression no per-task reviewer of T2 (which only edits
shadow files) could ever see. The original §14 placeholder ordering
(T2 then T5) does not surface or mitigate this. **Fix (folded into the
decomposition below):** the SP3 `_shadow_edit_remove` and the
synthetic-tree shadow construction are *both* migrated to the ONE
renderer in **T5** (the SP3-coupled task), and **T2 is reordered to
land *with* a same-task update to `_make_synthetic_engine_tree`** that
re-expresses the `throwaway`-injection against the fenced form (or, as
the decomposition specifies, makes the synthetic-tree shadows
themselves a renderer call against a `throwaway`-augmented roster
snapshot). T2's pinning test therefore includes
`pytest tpcore/tests/test_engine_sdlc_planner.py -q` going GREEN, not
only "the generator round-trips byte-identical" — so the cross-task
break is caught *inside* T2. This is the single genuine design defect
the hardening pass forced; the rest of the spec is sound.

### 14.2 The H-S4-\* hardening register

**H-S4-1 — the renderer must be a pure `str → str`; it must NEVER
write, mutate, or own the journal (the T5 atomicity-preservation
invariant).** *Risk:* migrating the SP3 executor's inline shadow edits
(`_shadow_edit_remove`, the ADD `_PROFILE` insert,
`_maybe_rewrite_frozen_literal`) to a shared renderer could let the
renderer perform the file write, bypassing the `_Journal`
record-before-mutate contract — silently regressing the SP3
#C1/#C2/H-S3-4 byte-identical-rollback property (a failed transition
must leave ZERO trace). *Mitigation:* the renderer's ONLY public seam
is `render_region(file_text: str, region: str, roster: tuple[str,...],
archived: tuple[str,...]) -> str` (and a `render_all(...)` over a
file) — it takes bytes/text in and returns the regenerated bytes/text;
it does **no** filesystem I/O, holds **no** journal, and is import-time
engine-free. The SP3 executor seam is unchanged in shape: `_apply_*`
still `jn.record_file(p)` **before** any write, then writes the
renderer's *returned* new text, exactly as today — the renderer
replaces only the `re.sub`/`str.replace` *computation* inside, never
the journal+write+rollback ordering. The CI `--check` is a third,
read-only caller (regenerate-in-memory + diff, never write). There are
**exactly three callers and one writer-of-files-per-context**: (a) CI
`--check` (reads, never writes), (b) the generator `--write` CLI
(writes, no journal — it is the operator/idempotent-regen tool, not
the transaction path), (c) the SP3 executor (journals OLD bytes →
writes renderer's NEW bytes → rollback restores OLD). NO fourth write
path. *Enforced at:* T5 — the renderer module has no `open(`/`.write_`/
`Path.write` (a grep-assertion test
`test_renderer_is_pure_no_filesystem_io`); and the SP3 atomicity
tests `test_apply_red_consistency_rolls_back_to_byte_identical`,
`test_apply_mid_move_loop_failure_byte_identical`,
`test_apply_modify_edits_default_const_and_rolls_back_byte_identical`,
`test_add_red_consistency_rolls_back_to_byte_identical`,
`test_add_readiness_miss_rolls_back_to_byte_identical`,
`test_apply_move_failure_restores_text_edits` must ALL stay GREEN
post-T5 (re-run as the T5 pinning gate).

**H-S4-2 — a generated region is rewritten ONLY between a matched
sentinel pair; an unmatched / duplicated / missing sentinel FAILS LOUD,
never silently no-ops or eats live text.** *Risk:* a regex/scan that
silently skips a region with a typo'd or duplicated fence would let
drift through (ungameability hole) OR a greedy match could clobber
live un-fenced text between two unrelated fences. *Mitigation:* the
renderer parses each file for *exactly one* `>>> engine-manifest:<id>
>>>` open and *exactly one* matching `<<< engine-manifest:<id> <<<`
close per declared region id; zero, >1, crossed, or nested ⇒ raise
`ManifestFenceError(file, region, reason)` (LOUD); the rewrite
replaces only the bytes strictly *between* the matched pair, leaving
the sentinel lines and every byte outside untouched. *Enforced at:*
T1 — `test_unmatched_sentinel_raises`,
`test_duplicate_sentinel_raises`,
`test_missing_close_sentinel_raises`,
`test_text_outside_fence_is_never_touched` (a fixture file with live
content on both sides of a region; assert byte-identical outside).

**H-S4-3 — regeneration is idempotent and round-trip byte-identical
(`--check` after a real `--write` is clean).** *Risk:* a generator
whose emitted bytes differ from what its own parser accepts (trailing
whitespace, newline, quoting) would make CI permanently red or require
a "regenerate twice" ritual — and would break the `--check` gate's
ungameability claim. *Mitigation:* `render_all` is a fixed point:
`render_all(render_all(x)) == render_all(x)` for every shadow file;
the emitted region bytes are exactly what the parser re-reads
(including the exact join: `, ` for the smoke list / pyproject rows,
` → ` for the dispatch-order prose, single-space for the bash `for
engine in` loop, the trailing newline policy pinned per file).
*Enforced at:* T2 — `test_generator_is_idempotent` (run `--write`,
capture bytes, run `--write` again, assert byte-identical) and
`test_check_clean_after_write` (`--write` then `--check` exits 0) for
each of the four shadow files.

**H-S4-4 — the generator reads ONLY `tpcore.engine_profile` SoT and
imports NO engine (tpcore∌engine / H-S3-10 layering, import-time
clean).** *Risk:* the generator pulling an engine package (e.g. to
"discover" engines) would violate the layering invariant the platform
mechanically enforces and could re-introduce import cycles. *Mitigation:*
`scripts/gen_engine_manifest.py` imports `from tpcore.engine_profile
import roster_for_dispatch, archived_engines` and stdlib only; no
`import <engine>`, no `importlib` of an engine, no `ops.*`. *Enforced
at:* T1 — `test_generator_imports_no_engine` (import the module, assert
no engine package name in `sys.modules` attributable to it; mirrors
the SP3 `test_importing_engine_sdlc_main_does_not_eager_import_an_
engine` pattern) and the CI `check_imports` ruff step in the final
task.

**H-S4-5 — a hand-edit INSIDE a fenced region is clobbered by regen +
caught by `--check`; a hand-edit OUTSIDE the fences is preserved.**
*Risk:* if `--check` only diffed the whole file it could false-RED on
legitimate out-of-fence edits; if it only checked fence presence it
could miss in-fence tampering (the ungameability hole). *Mitigation:*
`--check` regenerates **only** the fenced regions in memory and diffs
*those* against on-disk; out-of-fence bytes are never part of the
verdict. *Enforced at:* T3 —
`test_hand_edit_in_fence_fails_check` (mutate a byte inside a region,
assert `--check` rc≠0 with a unified diff naming that file/region),
`test_hand_edit_out_of_fence_passes_check` (mutate a byte outside every
region, assert `--check` rc==0).

**H-S4-6 — the bash `for engine in …` loop and the `pyproject.toml`
fenced rows must regenerate to *valid* bash / *parseable* TOML / a
*valid* Python docstring; engine-name quoting/escaping is pinned even
though names are `[a-z0-9_]`.** *Risk:* a generator that emits a fence
comment or join that breaks `bash -n` / `tomllib.loads` / the module
docstring (an unterminated string, a `#` where TOML wants a value, a
`"""` inside the Python docstring fence) bricks the build in a way the
`--check` byte-diff would *pass* (it only checks its own
regeneration). *Mitigation:* the spec pins per-file fence syntax,
verified against the real files read this pass: (a) `run_smoke_test.sh`
/ `run_all_engines.sh` — `#`-comment sentinels (bash ignores `#`
lines); the loop emits `for engine in <space-joined roster>; do`
verbatim (names are `[a-z0-9_]`, no quoting needed, but the renderer
still rejects any roster token not matching `^[a-z0-9_]+$` —
fail-loud, never emit unsafe bash); (b) `pyproject.toml` — `#`-comment
sentinels (legal, `tomllib`-ignored); the `testpaths` rows emit
`    "<e>/tests",` and the `packages.find.include` emits the engine
globs on the existing single `include = [...]` line as a fenced
*sub-span* only if that stays valid TOML — **decision pinned here:**
because the `include` array is one physical line shared with the
hand-owned `"tpcore*"` and the `# sigma archived` trailing comment,
the generator owns the engine glob tokens via a fenced **comment-block
form is not viable on a single array line**, so the `include` line is
re-expressed at T2 as a multi-line array where each engine glob is its
own fenced row (`"<e>*",`), the `"tpcore*"` row and trailing comment
stay outside the fence — `tomllib.loads` still parses (multi-line TOML
arrays are legal); (c) `platform_pipeline.py` / `run_smoke_test.sh`
header docstrings — the fenced span is plain prose lines that contain
no `"""`; the renderer rejects any roster join that would introduce a
`"""` (impossible with `[a-z0-9_]` names + ` → `/`, ` joins, but
asserted). *Enforced at:* T2 — `test_smoke_sh_still_parses` (`bash -n
scripts/run_smoke_test.sh` rc 0 post-fence+regen),
`test_run_all_engines_sh_still_parses`,
`test_pyproject_still_valid_toml` (`tomllib.loads` succeeds AND the
parsed `testpaths`/`include` equal the SoT-derived expectation),
`test_platform_pipeline_docstring_still_valid` (`ast.parse` the module,
assert `ast.get_docstring` is non-None and contains the regenerated
roster line).

**H-S4-7 — folding clockwork leg 6 into a one-line delegation must NOT
create a circular/oracle gap, must NOT weaken the lifecycle/structure
legs, and must still FAIL CI on a roster/shadow drift.** *Risk:* (a) a
delegation that imports/invokes the generator could hit the
`scripts/ops.py`↔`ops` sys.modules collision or a pytest
collection-order bootstrapping hazard; (b) replacing leg 6's parsed
assertion with a generator call could *lose* coverage if the
delegation is mis-wired (e.g. swallows the rc); (c) the structure legs
(1–5, 7–11) must remain independent and intact. *Mitigation:* leg 6's
body becomes a call to the generator's **pure in-process**
regenerate-and-diff function (NOT a subprocess from inside the
clockwork — the clockwork is itself run as a subprocess by the SP3
executor, and a subprocess-in-subprocess plus the `ops`/`scripts`
collision is the exact SP2-T9/SP3 hazard); the function returns the
drift diff or `None`; leg 6 asserts it is `None` with the diff as the
message. The generator's pure function imports only
`tpcore.engine_profile` + stdlib (H-S4-4), so the clockwork (already
importing `tpcore.engine_profile`) gains no new collision surface. The
structure legs are untouched (only leg 6's *body* changes; legs 1–5,
7–11 are read and re-asserted unchanged in the same task). The §10.4
reverse `ENGINE_TABLES` leg is added to leg 5 (a Python invariant,
*not* a regenerated shadow — zero overlap with the generator).
*Enforced at:* T4 — `test_leg6_fails_on_roster_drift` (monkeypatch /
fixture a drifted shadow in a temp tree, assert the delegated leg 6
RED with the file/region named), `test_leg6_green_on_clean_tree`, and
the *unchanged* legs 1–5/7–11 still GREEN in the same run (full
`pytest tpcore/tests/test_engine_lifecycle_consistency.py` is the T4
pinning gate); plus the collision-preemption stanza (H-S4-9) is NOT
needed here because the delegation is in-process pure (no `ops`
import) — explicitly asserted by `test_clockwork_imports_no_ops`.

**H-S4-8 — the reverse `roster ⊆ ENGINE_TABLES` predicate is the
*exact* grounded rule, not a vague "close the deferral".** *Risk:* a
guessed predicate (full subset vs allowlist) that does not match the
shipped `ENGINE_TABLES` would either false-RED today or fail to catch
the real half-state. *Mitigation:* grounded against the shipped
`tpcore/quality/validation/capital_gate.py::ENGINE_TABLES` read this
pass — its keys are exactly `{reversion, vector, momentum, sentinel,
allocator, canary}`; `roster_for_dispatch()` is exactly `(reversion,
vector, momentum, sentinel, canary)`. **Every live roster engine has
an `ENGINE_TABLES` row today** ⇒ the precise predicate is the *full
subset* `set(roster_for_dispatch()) <= set(ENGINE_TABLES) -
{allocator}` (allocator is excluded from the roster but legitimately
keyed in `ENGINE_TABLES` via its own `_dispatch_allocator` path — it
is on the existing forward leg's `allowed` set, not the reverse).
**No allowlist is needed** (every current engine has a real per-engine
data-dep; `canary`/`sentinel` both already carry `frozenset({...})`
rows). The failure it catches: a future ADD that wires a PAPER/LIVE
engine but forgets its `ENGINE_TABLES` data-dep row — a silent
un-gated engine (the `_required_sources` fail-safe would fall back to
`EXPECTED_SOURCES`, masking the omission; the reverse leg makes it a
hard CI fail). The SP1 deferred comment at
`test_engine_lifecycle_consistency.py:112-113` is **removed in the
same task** (T4). *Enforced at:* T4 — extend
`test_engine_tables_keys_are_known_engines` (or a sibling
`test_live_engine_has_engine_tables_row`) asserting
`set(roster_for_dispatch()) <= (set(ENGINE_TABLES) - {"allocator"})`
with the precise diagnostic; the deferred comment lines are deleted.

**H-S4-9 — every SP4 test that imports `ops`/the generator must
pre-empt the `scripts/ops.py`↔`ops` sys.modules collision.** *Risk:*
`scripts/ops.py` (a 160 KB non-package module) cached as `ops` in
`sys.modules` by an earlier test in full-suite collection order makes
`import ops.engine_sdlc.planner` (T5) and any `ops`-touching SP4 test
resolve the wrong `ops` — the exact bug that bit SP2-T9/T10/SP3.
*Mitigation:* the canonical eviction stanza already proven at
`scripts/tests/test_lab_cli_entrypoint.py:25-31` is copied verbatim
into every SP4 test module under `scripts/tests/` that imports `ops.*`
(the new `test_engine_manifest_in_sync.py` and the SP4 scope-gate
test): insert `REPO_ROOT` on `sys.path`, then evict any
`sys.modules["ops"|"ops.*"]` lacking `__path__`. The generator itself
is under `scripts/` and imports only `tpcore.*` (never `ops`), so it
is collision-immune by construction; the manifest test invokes it as a
**subprocess** (`[sys.executable, "scripts/gen_engine_manifest.py",
"--check"]`, `cwd=REPO`) — the faithful CI shape, which also
side-steps the in-process collision entirely (a fresh interpreter has
a clean `sys.modules`). *Enforced at:* T3 — the stanza is present in
`test_engine_manifest_in_sync.py`; a meta-assertion
`test_collision_preemption_stanza_present` greps the SP4 test files
for the eviction loop (symmetric to how SP3 pinned its own).

**H-S4-10 — doc-closure must be code-ACCURATE, not aspirational (the
operator hard-deliverable + the truthfulness mandate).** *Risk:* a doc
edit claiming a command/behavior/state the shipped code does not have
(e.g. an `ops.engine_sdlc` flag that doesn't exist, a `LifecycleState`
value that isn't real, "engine X graduated" when all five FAIL the
gate) is a defect that no per-doc-task reviewer can catch without
cross-checking code. *Mitigation:* SP4 adds a lightweight **"docs
match code" gate** —
`scripts/tests/test_sdlc_docs_match_code.py` — asserting, against the
real modules: (a) `python -m ops.engine_sdlc` and `python -m ops.lab`
entrypoints import-resolve (`importlib.util.find_spec("ops.engine_sdlc.
__main__")` / `"ops.lab.__main__"` not None) — with the H-S4-9
collision stanza; (b) the documented lifecycle states in
`docs/glossary.md`/`CLAUDE.md` (`LAB→PAPER→LIVE→RETIRED`) ==
`{s.name for s in LifecycleState}` exactly; (c) the roster line any
doc states == `roster_for_dispatch()` (parse the doc's
`reversion → vector → momentum → sentinel → canary` mention; assert
equality with the SoT join — SoT-derived, not hand-frozen); (d) the
accuracy guard: `CLAUDE.md` SDLC text contains the "all five engines
currently FAIL the DSR/credibility gate" honesty statement (a literal
substring assert — prevents a future edit from implying a graduation);
(e) the SP3 known-limitations (a)/(b) recorded in the spec still match
the shipped code (`_ENGINE_DEFAULT_CONSTS` has exactly the
`reversion` key — `set(planner._ENGINE_DEFAULT_CONSTS) == {"reversion"}`;
`_validate_modify` still contains the `type(want)(v)` coercion line) —
so the known-limitations section is provably truthful and a future
accidental fix/regress fails this gate. *Enforced at:* T8 — this test
lands in the SAME task as the last doc edit it validates (after T6/T7
content exists), so the doc tasks are CI-green individually and the
gate has real content to check.

**H-S4-11 — the OPERATIONS.md stale `search_parameters.py`-as-prod
reframe must not delete still-true content.** *Risk:* over-aggressively
rewriting OPERATIONS.md lines 713/719/738/755-757 could remove the
still-accurate description of `scripts/search_parameters.py` as the
underlying walk-forward engine the Lab wraps. *Mitigation:* the
reframe (T7) *adds* the `python -m ops.lab` canonical-entrypoint
framing and *re-roles* (does not delete) the search-script prose:
`scripts/search_parameters.py` is documented as the *underlying*
walk-forward harness the Lab now wraps, not the operator entrypoint;
the `scripts/run_*_search.sh` descriptions are kept where the scripts
still exist (verified at T7 time with `ls`), only their *role* is
corrected. *Enforced at:* T7 — `test_sdlc_docs_match_code` (H-S4-10)
clause asserting `docs/OPERATIONS.md` contains both `python -m
ops.lab` AND still references `scripts/search_parameters.py` (the
re-role, not a delete); plus the T7 task explicitly `ls`-verifies the
named search scripts before describing them.

**H-S4-12 — lane discipline: SP4 is engine-lane ONLY; the 8
data-lane-owned files + the data-SDLC spec/checklist are untouched;
SP3 carry-forwards are RECORDED not implemented; a final
scope-confinement gate (symmetric to SP3's T9, SP4's own allowlist).**
*Risk:* an SP4 edit straying into a data-lane file, or "helpfully"
fixing the SP3 (a)/(b) carry-forwards (turning a docs+manifest SP into
a MODIFY-rollout — out of scope), is a scope breach a per-task
reviewer cannot see holistically. *Mitigation:* a new
`scripts/tests/test_sp4_scope_confined.py` (reusing the proven SP3 T9
pattern: read-only `git diff --name-only` against the SP4 base,
skip-not-fail if no base ref) with SP4's OWN allowlist (`scripts/
gen_engine_manifest.py`, `scripts/tests/test_engine_manifest_in_sync.
py`, `scripts/tests/test_sp4_scope_confined.py`, `scripts/tests/
test_sdlc_docs_match_code.py`, `CLAUDE.md`, `docs/OPERATIONS.md`,
`docs/superpowers/checklists/engine_readiness.md`, `docs/glossary.md`,
`docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`,
`docs/superpowers/plans/2026-05-18-engine-sdlc.md`, the four shadow
files `scripts/run_smoke_test.sh`/`scripts/run_all_engines.sh`/
`ops/platform_pipeline.py`/`pyproject.toml`,
`tpcore/tests/test_engine_lifecycle_consistency.py`,
`ops/engine_sdlc/planner.py`,
`tpcore/tests/test_engine_sdlc_planner.py`) and a FORBIDDEN list that
includes the 8 data-lane files (`tpcore/calendar.py`, `tpcore/risk/`,
`tpcore/risk/governor.py`, `ops/engine_supervisor.py`,
`ops/engine_service.py`, `ops/engine_ladder.py`,
`tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`) AND the
data-SDLC spec/checklist/registry (`tpcore/providers.py`,
`tpcore/feeds/`, `tpcore/selfheal/`,
`docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`,
`docs/superpowers/checklists/data_feed_change_request.md`). The SP3
carry-forwards are only *recorded* (§13.2, already in this spec) and
their non-fix is the H-S4-10(e) assertion. *Enforced at:* Tn — the
SP4 scope-gate test is GREEN (full diff confined to the allowlist; no
forbidden prefix) and the H-S4-10(e) "carry-forwards unchanged" clause
is GREEN.

### 14.3 Final ordered T0–Tn TDD task decomposition

TDD-correct sequencing: the renderer (T1) + fences (T2) land before
the `--check` gate (T3), before the no-redundancy fold + reverse-leg
(T4), and before the SP3 executor migration (T5). The doc tasks
(T6–T8) are independent of the manifest core but the "docs match
code" gate (T8) lands after the doc content exists. The two scope/
suite gates are last (Tn). Every task is individually CI-green; the
pinning test ships in the same task as its behavior.

- **T0 — spec + expert-harden + plan (this).** *Files:* this spec
  (§14 filled). *Behavior:* the H-S4-\* register + decomposition +
  DDF-1 fix recorded. *Pin:* spec self-review (placeholder scan /
  internal consistency / scope / ambiguity) clean; this commit.

- **T1 — the pure renderer + generator skeleton.** *Create:*
  `scripts/gen_engine_manifest.py` exposing the pure
  `render_region(file_text, region, roster, archived) -> str` /
  `render_all(file_text, file_kind, roster, archived) -> str` (NO
  filesystem I/O, NO journal), the fence parser
  (matched-pair-or-raise `ManifestFenceError`), and a `--write`
  (default) / `--check` argparse CLI shell (the `--check` body wired
  in T3 — T1 ships `--write` working + `--check` returning a
  not-yet-fenced no-op-clean for files with no fences so T1 is
  CI-green standalone). Imports `from tpcore.engine_profile import
  roster_for_dispatch, archived_engines` + stdlib ONLY. *Create test:*
  `scripts/tests/test_gen_engine_manifest_render.py` (collision stanza
  H-S4-9). *Pin (H-S4-1/2/4):* `test_renderer_is_pure_no_filesystem_io`
  (grep the module: no `open(`/`.write_text`/`.write_bytes`/`os.write`
  outside the explicit `--write` CLI function which is the ONE
  intentional writer; assert `render_*` functions touch no fs — call
  with a string, assert pure return), `test_unmatched_sentinel_raises`,
  `test_duplicate_sentinel_raises`, `test_missing_close_sentinel_raises`,
  `test_text_outside_fence_is_never_touched`,
  `test_generator_imports_no_engine`.

- **T2 — sentinel-fence the four shadows + migrate the SP3
  synthetic-tree builder (DDF-1).** *Modify:* `scripts/run_smoke_test.sh`
  (fence the step-3 `for engine in` loop + the line 7-8 docstring
  listing), `scripts/run_all_engines.sh` (fence the "Engines
  dispatched: …" line), `ops/platform_pipeline.py` (fence the
  docstring engine listing), `pyproject.toml` (re-express the
  `include` array multi-line; fence the engine `testpaths` rows + the
  engine `include` globs — `"tpcore*"` + trailing comment stay
  outside, H-S4-6); **and `tpcore/tests/test_engine_sdlc_planner.py::
  _make_synthetic_engine_tree`** — re-express the `throwaway`-shadow
  injection against the fenced form (DDF-1: the simplest correct
  form is to compute the synthetic shadows by calling the T1 renderer
  with a `roster + ("throwaway",)` argument rather than `str.replace`
  on now-stale literals). *Behavior:* the committed shadows carry the
  fences with the SoT-correct content; the generator round-trips them
  byte-identical; the SP3 synthetic tree still builds a green
  pre-REMOVE tree. *Pin (H-S4-3/6 + DDF-1):*
  `test_generator_is_idempotent`, `test_check_clean_after_write` (per
  file), `test_smoke_sh_still_parses` (`bash -n`),
  `test_run_all_engines_sh_still_parses`,
  `test_pyproject_still_valid_toml` (`tomllib.loads` + parsed
  testpaths/include == SoT), `test_platform_pipeline_docstring_still_
  valid` (`ast.get_docstring` non-None + contains the roster line),
  **AND `pytest tpcore/tests/test_engine_sdlc_planner.py -q` GREEN**
  (the DDF-1 cross-task regression caught inside T2), plus the
  existing `test_structurally_parseable_shadows_match_sot` /
  `test_retired_engine_absent_from_structural_shadows` still GREEN
  (leg 6 not yet folded — T4 owns that; T2 keeps it passing against
  the fenced-but-equivalent content).

- **T3 — the `--check` CI-divergence gate + the in-sync test.**
  *Modify:* `scripts/gen_engine_manifest.py` (`--check`: regenerate
  every fenced region in memory, diff vs on-disk, exit 0 iff every
  region byte-identical, else non-zero + unified diff naming the
  file/region; expose a pure `divergences() -> str | None` for the
  T4 in-process delegation). *Create:* `scripts/tests/
  test_engine_manifest_in_sync.py` (collision stanza H-S4-9; invokes
  the generator as a **subprocess** `--check`, asserts rc 0). *Pin
  (H-S4-5/9):* `test_check_clean_on_committed_tree` (subprocess
  `--check` rc 0 on the real repo), `test_hand_edit_in_fence_fails_
  check`, `test_hand_edit_out_of_fence_passes_check`,
  `test_collision_preemption_stanza_present`.

- **T4 — fold clockwork leg 6 (no-redundancy §10.5) + close the
  reverse `roster ⊆ ENGINE_TABLES` leg (§10.4).** *Modify:*
  `tpcore/tests/test_engine_lifecycle_consistency.py` — replace leg
  6's body with a one-line delegation to the generator's **in-process
  pure** `divergences()` (assert it returns `None`, message = the
  diff); extend leg 5
  (`test_engine_tables_keys_are_known_engines`) / add a sibling with
  the grounded reverse predicate `set(roster_for_dispatch()) <=
  (set(ENGINE_TABLES) - {"allocator"})`; **delete the SP1 deferred
  comment lines 112-113**. Legs 1–5/7–11 bodies unchanged. *Pin
  (H-S4-7/8):* `test_leg6_fails_on_roster_drift` (fixtured drift →
  RED with file/region named), `test_leg6_green_on_clean_tree`,
  `test_clockwork_imports_no_ops`, the new reverse-leg test GREEN
  today + RED on a synthetic missing-`ENGINE_TABLES`-row fixture, and
  full `pytest tpcore/tests/test_engine_lifecycle_consistency.py -q`
  GREEN (legs 1–5/7–11 unregressed).

- **T5 — migrate the SP3 executor's inline shadow edits to the ONE
  renderer (atomicity-preserving, H-S4-1).** *Modify:*
  `ops/engine_sdlc/planner.py` — `_shadow_edit_remove`,
  `_maybe_rewrite_frozen_literal`, and the `_apply_add` `_PROFILE`/
  shadow insert recompute the new file text via the T1 renderer
  (`render_all`/`render_region`) *instead of* the inline
  `re.sub`/`str.replace`; the journal+write+rollback ordering is
  **byte-for-byte unchanged** (`jn.record_file(p)` BEFORE write; write
  the renderer's returned text; rollback restores the recorded OLD
  bytes). The renderer is NEVER given a path and NEVER writes. *Pin
  (H-S4-1 — the make-or-break task):* the FULL SP3 atomicity suite
  stays GREEN —
  `test_apply_red_consistency_rolls_back_to_byte_identical`,
  `test_apply_mid_move_loop_failure_byte_identical`,
  `test_apply_modify_edits_default_const_and_rolls_back_byte_identical`,
  `test_add_red_consistency_rolls_back_to_byte_identical`,
  `test_add_readiness_miss_rolls_back_to_byte_identical`,
  `test_apply_move_failure_restores_text_edits`,
  `test_remove_rostered_engine_updates_frozen_literal`,
  `test_remove_throwaway_engine_end_to_end` — re-run as the T5 gate;
  plus a new `test_planner_shadow_edit_uses_renderer_not_inline_regex`
  (assert `_shadow_edit_remove` calls the renderer; assert the renderer
  is import-clean of engines) and `test_renderer_never_called_with_a_
  path` (the renderer signature is `str → str`; a guard test that it
  has no `Path`/`open` in its body). Full
  `pytest tpcore/tests/test_engine_sdlc_planner.py
  tpcore/tests/test_engine_lifecycle_consistency.py -q` GREEN.

- **T6 — doc-closure: `CLAUDE.md` (§11.1).** *Modify:* `CLAUDE.md` —
  Architecture/Conventions: the Engine SDLC entry (LAB→PAPER→LIVE→
  RETIRED, `tpcore.engine_profile` SoT, all five PAPER, the
  `sigma`(RETIRED)/`lab`(LAB) sentinels); Session Rules: the canonical
  `python -m ops.engine_sdlc --ecr <file>` / `python -m ops.lab …`
  commands + the "ECR/Lab only, never hand-edit `_PROFILE`/shadows"
  rule; the engine-build shortlist cross-ref to `engine_readiness.md`
  + the `scripts/gen_engine_manifest.py` generated-manifest discipline;
  the accuracy guard ("all five engines currently FAIL the DSR/
  credibility gate"). *Pin:* content lands; validated by T8's gate
  (clauses b/c/d) — T6 alone is CI-green (no test added here; doc
  edit only, lane-confined).

- **T7 — doc-closure: `docs/OPERATIONS.md` (§11.2 + the stale
  search-script reframe, H-S4-11).** *Modify:* `docs/OPERATIONS.md` —
  new "Engine SDLC" section (ECR workflow, ADD/REMOVE y/n vs MODIFY/
  promote automated, snap-out → archive/EULOGY, the clockwork, the
  manifest gate); the Lab runbook (`python -m ops.lab` args, the
  `docs/lab/…` + `.json` sidecar, recommendation-only / never
  daemon-wired / isolation); re-role (NOT delete) the lines
  713/719/738/755-757 `search_parameters.py`-as-prod framing — `python
  -m ops.lab` is the canonical entrypoint, `search_parameters.py` the
  underlying walk-forward harness it wraps; `ls`-verify the named
  `run_*_search.sh` scripts before describing them. *Pin:* content
  lands; validated by T8's gate (the OPERATIONS.md clause); T7 alone
  CI-green.

- **T8 — doc-closure: `engine_readiness.md` + `docs/glossary.md` +
  the "docs match code" gate (H-S4-10).** *Modify:*
  `docs/superpowers/checklists/engine_readiness.md` (header note: this
  IS the ADD-path build gate §8; a `new_scaffold` ADD machine-checks
  the `planner._check_readiness` subset — scaffold dir, `tests/`,
  importable `.scheduler`, exactly 5 `BaseEnginePlug` subclasses —
  the rest operator-verified; bidirectional cross-link to the SDLC
  spec + the ECR checklist; mark items "enforced by the ECR" vs
  "operator-verified" accurately to the shipped `_check_readiness`),
  `docs/glossary.md` (the 8 engine-domain terms §11.4, symmetric in
  form to the existing Data-lane entries). *Create:* `scripts/tests/
  test_sdlc_docs_match_code.py` (collision stanza H-S4-9) — the
  lightweight code-accuracy gate per H-S4-10 clauses (a)–(e),
  validating T6/T7/T8 content against the shipped modules. *Pin
  (H-S4-10):* the gate test GREEN — entrypoints import-resolve,
  documented lifecycle states == `LifecycleState`, the documented
  roster line == `roster_for_dispatch()`, the CLAUDE.md FAIL-the-gate
  honesty substring present, OPERATIONS.md contains both `python -m
  ops.lab` and the re-roled `search_parameters.py`, the SP3
  carry-forwards (a)/(b) provably unchanged
  (`set(planner._ENGINE_DEFAULT_CONSTS) == {"reversion"}`;
  `type(want)(v)` line still present).

- **Tn — SP4 scope-confinement gate + full-suite + CI-exact
  ruff/check_imports + finish-branch (H-S4-12).** *Create:*
  `scripts/tests/test_sp4_scope_confined.py` (reuse the proven SP3 T9
  read-only `git diff --name-only` pattern; SP4's own allowlist +
  the data-lane/data-SDLC FORBIDDEN list per H-S4-12; collision
  stanza if it imports `ops`; skip-not-fail on no base ref). *Pin:*
  the SP4 scope-gate GREEN (the full SP4 diff confined to the
  allowlist, zero forbidden prefix); full `pytest -q` GREEN;
  CI-exact `ruff check .` clean; `check_imports` (tpcore∌engine)
  clean (the generator imports no engine — H-S4-4); the spec
  known-limitations still match shipped code (the H-S4-10(e) clause);
  `finishing-a-development-branch` (single CI-green-mergeable branch
  to `main`).

---

*Lane: ENGINE. Data-SDLC files are READ-ONLY symmetry reference,
untouched. This is the LAST sub-project of the operator-approved
4-chain (SP1→SP2→SP3→SP4). The spec is the deliverable; the
doc-closure edits and the generator are SP4 implementation tasks,
planned after the expert-harden pass.*
