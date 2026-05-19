# SP-B — Roster-Driven Plug-and-Play Lab Targeting (Hardened Design Spec)

**Status:** DESIGN (skeptical-staff-engineer hardened, pre-expert-harden).
**Epic:** `docs/superpowers/specs/2026-05-19-lab-front-half-epic.md` §1 SP-B.
**Lane:** engine lane only. Data-SDLC files are read-only symmetry references, never edited here.
**Dependency:** after SP-A (SHIPPED — `tpcore/lab/ledger.py`, wired in `ops/lab/run.py:745-759,867-872`).
**Pipeline:** `docs/DEV_PIPELINE_STANDARD.md` (brainstorm→expert-harden→spec/plan gated PRs→subagent exec→split review→whole-suite+order-flip→squash-merge).

---

## §0 Context — verified in code (2026-05-19)

The Lab hardwires a stale 3-tuple `(reversion, vector, momentum)` across **six** surfaces (the harden pass found a sixth the skeptical-staff pass missed — see §0.1); the SP1 roster SoT (`tpcore.engine_profile._PROFILE`) is the contradicted authority.

**The hardwired surfaces (verified):**

1. `ops/lab/run.py:95-131` — `PARAM_RANGES: dict[str, dict[str, tuple]]` with literal keys `reversion`/`vector`/`momentum`. Consumed by `sample_parameters(engine, n, seed)` at `ops/lab/run.py:146-149` (`ranges = PARAM_RANGES[engine]` — raw `KeyError` on an unknown engine, no clear message). **Verified called at `_run_lab_core:730` (`candidates = sample_parameters(args.engine, ...)`) BEFORE the SP-A `record_trial_spend` block at `:752-759`** — load-bearing for the ledger-ordering invariant (§4.2, §8-A4).
2. `ops/lab/run.py:316-328` — `_runner_for(engine)`: an `if engine == "...":` ladder lazily importing `<engine>.backtest.run_for_search`; `raise ValueError(f"unknown engine: {engine}")` fallthrough. Called at `_run_lab_core:778`.
3. `ops/lab/run.py:331-342` — `_context_loader_for(engine)`: same ladder, lazily imports the per-engine-named `load_<engine>_window_context` (`load_reversion_window_context` etc.). Called at `:779`.
4. `ops/lab/run.py:345-356` — `_context_runner_for(engine)`: same ladder, lazily imports the per-engine-named `run_<engine>_with_context`. Called at `:780`.
5. `ops/lab/__main__.py:50-53` and `ops/lab/run.py:620` — argparse `--target-engine` / `--engine` `choices=("reversion","vector","momentum")` (a second, independent hardcoding of the same literal tuple, in two files).
6. **`ops/engine_sdlc/default_params.py:13-22` — `default_params(engine)`: a SIXTH `if engine == "...":` ladder, byte-identical in shape to `_runner_for`, lazily importing `<engine>.backtest.default_params`; same `raise ValueError(f"unknown engine: {engine}")` fallthrough.** Called from `_build_lab_result` (`ops/lab/run.py:1057`, `_live_defaults = default_params(args.engine)`) on the **SURVIVED *and* FAILED success path of `run_lab()`** — i.e. the *primary* SP-B path (`python -m ops.lab --target-engine …`). The skeptical-staff §0 (and epic §1 SP-B) enumerated only five surfaces and explicitly scoped `default_params` out as SP-D; **the harden pass rejects that scoping** (§0.1, §8-B6): it is pure name→module dispatch (`param_diff` provenance, not ranking), structurally a `_runner_for` clone, and a *latent forward footgun* — a future SP-F engine that declares `LAB_TARGET` but is not added to *this* ladder spends a full walk-forward + a real cumulative-ledger increment, then dies with a raw `unknown engine: <new>` `ValueError` inside `_build_lab_result`, AFTER the irreversible SP-A spend. Folding it into the same SoT contract is squarely SP-B's "the dispatch ladder exists solely to translate engine-name → module + symbol" concern.

### §0.1 The sixth surface — why it is in scope (harden-pass addition)

The skeptical-staff spec's "five surfaces / NO `default_params`" framing was a **scope error, not a deferral**. `default_params` is not scoring (SP-D) — SP-D is `_score_for_ranking`/`rank_candidates`/the declared *primary metric* (epic §1 SP-D). `default_params` supplies the `current` side of the `LabResult.param_diff` (`run.py:1057-1061`), a pure dispatch concern in the exact `_runner_for` family. Leaving it as a separate hand-ladder while SP-B deletes the other five **re-creates the very Sigma-22-site drift class SP-B exists to delete**, just one file over, and converts SP-F's failure mode from "clear pre-flight reject" into "full Lab run + ledger spend then crash". Decision: `default_params` becomes the **fourth callable in the `LabTarget` contract** (`§2.2`), resolved by the same `_lab_target_for` resolver; `ops/engine_sdlc/default_params.py` becomes a thin delegating shim to preserve its existing importers/oracle (`§2.3`, §8-B6).

**The roster SoT (verified, `tpcore/engine_profile.py`):**

- `_PROFILE` (`:61-91`): reversion/vector/momentum/sentinel/canary = `LifecycleState.PAPER`; `allocator` = PAPER (dispatch_order=0, structurally-separate `_dispatch_allocator` path, D-SDLC1-4); `sigma` = `RETIRED`; `lab` = `LAB` (the durable sentinel proving `LifecycleState.LAB` is exercised — NOT a runnable engine, `:81-91`).
- `_DISPATCHABLE = frozenset({PAPER, LIVE})` (`:45-46`). `_roster_sorted` (`:99-109`) filters non-RETIRED, non-allocator, and (via `_DISPATCHABLE`) non-LAB. `roster_for_dispatch()` (`:112-115`) returns `(reversion, vector, momentum, sentinel, canary)`.
- **There is NO accessor for "LAB ∪ PAPER ∪ LIVE"** (the Lab-targetable set). `roster_for_dispatch()` is PAPER/LIVE only and *excludes* LAB; `archived_engines()` is RETIRED only. SP-B must add exactly one new accessor (decided in §1).

**The per-engine backtest contract is already uniform (verified):**

- `reversion/backtest.py:1084`, `vector/backtest.py:959`, `momentum/backtest.py:535` — `run_for_search` has the **byte-identical** keyword-only signature `(*, db_url, start, end, universe=None, overrides=None, trade_log_path=None) -> BacktestRunResult`.
- `load_<engine>_window_context` (`reversion:955`, `vector:823`, `momentum:429`) — uniform `(*, db_url, start, end, universe=None) -> *WindowContext`.
- `run_<engine>_with_context` (`reversion:996`, `vector:868`, `momentum:459`) — uniform `(context, *, overrides=None, trade_log_path=None) -> BacktestRunResult`.
- The **only** per-engine variance is the *function name* (`load_reversion_window_context` vs `load_vector_window_context`) and the module (`<engine>.backtest`). The dispatch ladder exists *solely* to translate engine-name → module + symbol-name. This is the entire problem surface.

**SP-A ledger is already engine-agnostic (verified — critical for the dependency invariant):**

- `tpcore/lab/ledger.py:40-46` `ledger_source(target)` → `f"lab_trial_ledger.{target}"`; `record_trial_spend(... target=...)` (`:49`) and `cumulative_n_trials(pool, target, before_ts)` (`:97`) are keyed on a **free `str` target**, no enum, no allow-list.
- `ops/lab/run.py:752-759` records spend with `target=args.engine`; `:867-872` reads `cumulative_n_trials(_ledger_pool, args.engine, spend_ts)` and computes `effective_n_trials = cumulative + args.trials`.
- **Therefore: the cumulative n_trials ledger applies to any roster-resolved target identically with ZERO ledger change.** A new target's *first ever* Lab run reads `cumulative_n_trials == 0` (no prior `lab_trial_ledger.<newtarget>` rows) ⇒ `effective_n_trials = 0 + args.trials` — the exact SP-A floor SP-A's own tests pin (T6/T9: SP-A reduces to per-run when cumulative == 0). SP-B inherits this for free. **SP-B MUST NOT touch any ledger code path.**

**SP4 consistency clockwork (verified, `tpcore/tests/test_engine_lifecycle_consistency.py`):**

- `test_dispatch_order_invariant_is_the_frozen_literal` (`:43-47`) pins `roster_for_dispatch() == (reversion,vector,momentum,sentinel,canary)`.
- `test_structurally_parseable_shadows_match_sot` (`:133-147`) + `test_leg6_*` (`:241-283`) delegate **all** non-Python shadow drift detection to `scripts.gen_engine_manifest.divergences(repo)` — ONE pure in-process regenerate-and-diff mechanism (`scripts/gen_engine_manifest.py:152-172`). Adding a *second* independent parsed-roster assertion was explicitly rejected by SP4 §10.5 ("a SECOND shadow mechanism that can disagree").
- `gen_engine_manifest.py` `_FILE_REGIONS` (`:132-137`) covers `run_smoke_test.sh`, `run_all_engines.sh`, `ops/platform_pipeline.py`, `pyproject.toml`. **It does NOT cover `ops/lab/__main__.py` or `ops/lab/run.py`.**

**Characterization oracle (verified, `scripts/tests/test_search_parameters_characterization.py`):**

- `:201-203` and `:289-291` monkeypatch **by name**: `ops.lab.run._context_runner_for`, `ops.lab.run._context_loader_for`, `ops.lab.run._runner_for` (each `lambda e: _fake`).
- `:73-79` `test_sample_parameters_is_seed_deterministic` asserts `set(combo) == set(sp.PARAM_RANGES["reversion"])` — reads `PARAM_RANGES` **as a subscriptable dict by name**.
- The module docstring (`:25-26`) **explicitly anticipates SP-B**: "monkeypatch targets (`sp._runner_for`, `sp._context_runner_for`, `sp._context_loader_for`) MUST be retargeted to ..." — i.e. the oracle authors expect SP-B to keep these as named, monkeypatchable callables OR to update the oracle in the same change. **This is the single sharpest compatibility constraint and dictates the chosen mechanism.**

---

## §1 Verdict — chosen mechanism

**CHOSEN: (i-refined) Convention-based importlib resolver, keyed on the engine package name, with a per-engine `LAB_TARGET` declaration object (carrying **all four** dispatch callables — `run_for_search`/`load_window_context`/`run_with_context`/`default_params` — plus `param_ranges`) exported by `<engine>.backtest`. The hardwired `ops/lab/run.py` functions (`PARAM_RANGES`-as-dict, `_runner_for`, `_context_loader_for`, `_context_runner_for`) AND the sixth surface `ops/engine_sdlc/default_params.py::default_params` (§0.1, harden-pass addition) are KEPT as named module-level callables but their bodies become a single SoT-driven lookup (no `if engine ==` ladder). `--target-engine`/`--engine` `choices` are generated from a new `tpcore.engine_profile.lab_targetable_engines()` accessor.**

### The per-engine declaration contract

Each runnable engine's `<engine>.backtest` module exports **one** module-level constant:

```python
# in <engine>/backtest.py — engine-OWNED, lives WITH the engine, not in ops/lab
from tpcore.lab.target import LabTarget   # engine-free contract layer (see §2)

LAB_TARGET = LabTarget(
    param_ranges={
        "z_threshold": (2.0, 4.0, "float"),
        ...,
    },
    run_for_search=run_for_search,
    load_window_context=load_reversion_window_context,
    run_with_context=run_reversion_with_context,
    default_params=default_params,   # the SIXTH surface, §0.1 — param_diff `current` side
)
```

All four callables already exist uniformly in every engine's `backtest.py` (`default_params` verified present in reversion/vector/momentum `backtest.py`; absent in sentinel — the §4.1 undeclared-engine reject covers it identically to the other three callables). The contract is engine-name-free: the engine references its own already-defined symbols.

`LabTarget` is a frozen pydantic-v2 model (config `frozen=True, extra="forbid", arbitrary_types_allowed=True`) in a NEW engine-free module `tpcore/lab/target.py`. It carries the param-range dict + the four already-uniform callables (`run_for_search`/`load_window_context`/`run_with_context`/`default_params`). The engine declares it once; `ops/lab/run.py` resolves it by `importlib.import_module(f"{engine}.backtest").LAB_TARGET` **inside the existing lazy-import bodies** (legal in `ops/`, H-S2-1; the resolver lives in `ops/lab/run.py`, NOT `tpcore/`).

### Why this mechanism (the binding-requirement scorecard)

| Requirement | (i) convention importlib + `LAB_TARGET` (CHOSEN) | (ii) explicit per-engine `LabTarget` registered into an `ops/lab` registry on import | (iii) `engine_profile`-attached capability descriptor |
|---|---|---|---|
| **Live-path byte-identity** | ✅ `LAB_TARGET` is a module-level constant referencing functions that already exist; defining it adds zero call into the live trading path. The scheduler/order-manager never import `backtest.LAB_TARGET`. | ✅ same | ⚠️ would push Lab-specific param-range data into `tpcore.engine_profile`, a module imported by the live dispatch path (`should_fire`) — pollutes a live-critical SoT with Lab concerns. **Rejected.** |
| **`ops/`-only lazy-import legality (H-S2-1)** | ✅ resolution is `importlib.import_module` inside the existing lazy bodies in `ops/lab/run.py`; no engine import at `ops.lab` module top; `tpcore/lab/target.py` is engine-free (only pydantic + stdlib). | ⚠️ a registry "populated on engine import" needs *something* to import every engine to populate it → either eager engine import (illegal pattern, breaks `import ops.lab.__main__` no-eager-import, `__main__.py:18-20`) or a lazy walk that is just mechanism (i) with extra state. **Rejected as strictly-worse (i).** | ✅ resolution in `ops/` but see live-path row. |
| **Oracle compatibility (`scripts/tests/test_search_parameters_characterization.py:201-203,289-291` monkeypatch by name; `:73-79` reads `PARAM_RANGES` as a dict)** | ✅ `_runner_for`/`_context_loader_for`/`_context_runner_for` stay as named module-level callables (bodies changed, names+signatures `(engine)->callable` unchanged) ⇒ the by-name monkeypatch still binds. `PARAM_RANGES` is **kept as a name** but becomes a *lazy mapping object* (see §2.4) so `PARAM_RANGES["reversion"]` and `set(PARAM_RANGES["reversion"])` still work ⇒ `:73-79` passes unmodified. | ⚠️ a registry replaces the three named functions ⇒ oracle monkeypatch targets vanish ⇒ oracle must be rewritten (larger blast radius, the docstring's "MUST be retargeted" path). Acceptable but more churn. | ⚠️ same as (ii). |
| **momentum/sentinel are batch (no per-trade)** | ✅ irrelevant to the contract: `LabTarget` carries the four `run_*`/`load_*`/`default_params` callables which already exist uniformly for *all* engines incl. batch momentum (`momentum/backtest.py:429,459,535` + its `default_params`). Sentinel does NOT yet export them — handled as the §4 "undeclared roster engine" hard-reject (SP-E/SP-F forward dep), NOT a silent absence. | ✅ same | ✅ same |
| **YAGNI** | ✅ minimal: one new engine-free model file + one constant per engine + dict→lazy-mapping + **5 ladder bodies** collapsed (the 4 in `run.py` + the 6th-surface `default_params` shim, §0.1) + 1 accessor + 1 clockwork test + 1 parity-test message-pin update. No registry singleton, no import-time side effects, no new SoT. `LabTarget` is a pydantic model (not a `NamedTuple`/dataclass) **only because** `model_post_init` fail-loud validation of the `param_ranges` (low,high,kind) tuple contract at *declaration* time is load-bearing (§2.2) — a plain tuple would defer the error to sample time on a live-money-adjacent path; this is justified, not gold-plating (rejection of the "use a NamedTuple" criticism — §8-B5). | ❌ a registry is a second mutable SoT-shaped object (the parallel-SoT anti-pattern the project explicitly rejects, cf. defect-register ADR). | ❌ widens a frozen live SoT for a Lab-only need. |

**Mechanism (i-refined) is chosen.** It is the smallest diff that (a) removes the stale shadow rather than adding a new one, (b) preserves every oracle monkeypatch/dict-access by name, (c) keeps the engine as the owner of its own Lab declaration (engine add/remove = a SoT `_PROFILE` edit + the engine declaring `LAB_TARGET`, never Lab surgery), and (d) keeps `tpcore` engine-free and the live path byte-identical.

### Consistency-clockwork choice: pure runtime-derived set + test, NOT a generated shadow

The Lab target set is **NOT** added to `gen_engine_manifest.py`'s `_FILE_REGIONS`. Rationale:

- The `gen_engine_manifest` shadows exist because their files (`run_smoke_test.sh`, `pyproject.toml`, …) **cannot import Python at parse time** — they are bash/TOML/docstring text that must *physically contain* the roster, so the only drift-defence is regenerate-and-diff bytes.
- The Lab target set, post-SP-B, is **runtime-derived** (`lab_targetable_engines()` resolved at call time) — there is no frozen text copy to drift. Generating a shadow of an already-runtime-derived set would *reintroduce* a redundant copy — the exact anti-pattern SP-B exists to delete (epic §1 SP-B "the stale-shadow contradiction ... removed"; CLAUDE.md Sigma 22-site-drift rule: "must REMOVE a stale shadow, not add another").
- Therefore the clockwork is a **pure consistency test** asserting `set(<Lab-targetable resolved set>) == set(lab_targetable_engines())` and that the CLI `choices` are generated from (not a literal copy of) the accessor. This mirrors `test_provider_lifecycle_consistency.py` / SP4's *intent* (a roster change reds the build until the Lab follows) without adding a second byte-shadow mechanism. **Argued and decided.**

---

## §2 Architecture

### §2.1 New roster accessor — `tpcore.engine_profile.lab_targetable_engines()`

**Decision: add exactly ONE new public accessor; do NOT reuse `roster_for_dispatch()`** (it excludes LAB, and the Lab targeting a LAB candidate is the entire point of the SDLC LAB state — epic §1 SP-B explicitly: `LifecycleState.{LAB,PAPER,LIVE}`).

```python
_LAB_TARGETABLE: frozenset[LifecycleState] = frozenset(
    {LifecycleState.LAB, LifecycleState.PAPER, LifecycleState.LIVE})

def lab_targetable_engines() -> tuple[str, ...]:
    """Engines the Lab MAY fish against: LAB/PAPER/LIVE, non-allocator,
    EXCLUDING the durable `lab` sentinel (it is not a runnable engine —
    no package/backtest, test_lab_sentinel_is_not_wired). RETIRED and
    allocator are excluded. Ordered by dispatch_order for stable diffs."""
```

**Predicate decisions (each justified — these are load-bearing and a sharp-attack target):**

| Engine | lifecycle | Lab-targetable? | Why |
|---|---|---|---|
| reversion/vector/momentum | PAPER | ✅ yes | current real targets; declare `LAB_TARGET`. |
| sentinel | PAPER | ✅ **eligible by predicate**, but **undeclared** until SP-E | epic §1 SP-E *requires* Sentinel become a roster-driven Lab target. The accessor includes it (PAPER); it has no `LAB_TARGET` yet ⇒ §4 hard-reject with a clear message ("eligible but has not declared LAB_TARGET"). Including-but-rejecting (not excluding) is correct: it makes "Sentinel is targetable once it declares" a *visible, tested* state, and SP-E's deliverable is exactly "declare `LAB_TARGET`". |
| canary | PAPER | ❌ **excluded by an explicit predicate clause** | canary is the documented non-graduating heartbeat (CLAUDE.md; spec §4b; `canary/tests/test_backtest.py::test_backtest_deliberately_never_writes_credibility`). A Lab run's whole purpose is a graduation-track DSR/credibility verdict; running the Lab against an engine that by-construction never writes credibility is a category error (it would always produce `credibility==0` → always FAILED — a meaningless trial that still *spends cumulative n_trials against canary*, polluting the SP-A ledger for no possible edge). Exclude it with a **named predicate clause + a dedicated test pinning canary∉lab_targetable_engines()**, so the exclusion is intentional and regression-proof, not incidental. |
| allocator | PAPER | ❌ excluded | not a top-level engine package (D-SDLC1-4, no `allocator.backtest`); already excluded by the existing `_ALLOCATOR_ENGINE` filter pattern — reuse it. |
| sigma | RETIRED | ❌ excluded | RETIRED ∉ `_LAB_TARGETABLE`. |
| lab | LAB | ❌ excluded | the durable sentinel — LAB-state proof, NOT a runnable engine (`test_lab_sentinel_is_not_wired`, `:150-164`: no `lab/` package). Excluded by an explicit name clause mirroring how `_ALLOCATOR_ENGINE` is special-cased. |

So the accessor's predicate is: `lifecycle ∈ _LAB_TARGETABLE  ∧  engine != _ALLOCATOR_ENGINE  ∧  engine != _LAB_SENTINEL  ∧  engine != "canary"`. Today this resolves to `(reversion, vector, momentum, sentinel)`. **canary and the lab sentinel are excluded structurally; sentinel is included-but-undeclared.**

> **§4b note (canary exclusion is a policy seam, attack-surface flagged):** hardcoding the string `"canary"` in a `tpcore` predicate is itself a mini-shadow of the canary spec §4b decision. Mitigation: the exclusion clause carries an inline comment citing spec §4b + the canary credibility test, AND a dedicated forcing test (`test_canary_not_lab_targetable`) makes the policy explicit. A *general* "engines that never write credibility" predicate is rejected as YAGNI/over-abstraction for an N=1 case (only canary is non-graduating by construction). This is the chosen, documented trade-off — flag for the harden pass.

### §2.2 The engine-free contract — `tpcore/lab/target.py`

```python
class LabTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid",
                              arbitrary_types_allowed=True)
    param_ranges: dict[str, tuple]          # {param: (low, high, kind)}
    run_for_search: Callable[..., Awaitable[Any]]
    load_window_context: Callable[..., Awaitable[Any]]
    run_with_context: Callable[..., Any]
    default_params: Callable[[], dict[str, Any]]   # §0.1 sixth surface — param_diff `current`
```

- Lives in `tpcore/lab/` next to `ledger.py`/`context.py`/`models.py` (the established engine-FREE Lab contract layer, H-S2-1) — imports only pydantic + `collections.abc` + stdlib. `tpcore.scripts.check_imports` stays green (no `tpcore→engine` import; the *engine* imports *this*, the legal direction).
- `arbitrary_types_allowed=True` is required to carry bare callables in a pydantic model; the model is otherwise frozen/extra-forbid. A lightweight `model_post_init` validates `param_ranges` values are 3-tuples whose `[2]` is `"float"|"int"|"choice:..."` (the exact `_sample_value` contract, `ops/lab/run.py:134-143`) — fail-loud at declaration time, not at sample time.

### §2.3 The dispatch indirection — `ops/lab/run.py`

A single private resolver replaces all the ladders' bodies (the three in `run.py` here, plus the sixth-surface `default_params` shim below):

```python
def _lab_target_for(engine: str) -> LabTarget:
    """Resolve the engine's declared LabTarget via the roster SoT.
    Engine-import is LAZY (legal in ops/, H-S2-1). Hard-rejects an
    engine that is not roster-Lab-targetable OR has not declared
    LAB_TARGET — clear message, never a raw KeyError/ImportError."""
    from tpcore.engine_profile import lab_targetable_engines
    if engine not in lab_targetable_engines():
        raise ValueError(
            f"engine {engine!r} is not Lab-targetable; choose one of "
            f"{lab_targetable_engines()} (roster SoT: tpcore.engine_profile)")
    import importlib
    try:
        mod = importlib.import_module(f"{engine}.backtest")
    except ModuleNotFoundError as exc:
        raise ValueError(f"engine {engine!r} has no importable "
                         f"{engine}.backtest module: {exc}") from exc
    target = getattr(mod, "LAB_TARGET", None)
    if target is None:
        raise ValueError(
            f"engine {engine!r} is roster-Lab-eligible but has not "
            f"declared a module-level LAB_TARGET in {engine}.backtest "
            f"(see tpcore/lab/target.py:LabTarget). This is the SP-E/SP-F "
            f"forward step: the engine must declare its Lab contract.")
    return target
```

The three named seams become thin views over the resolver (names + `(engine)->callable` signatures **unchanged** so the oracle monkeypatch by name still binds, §0):

```python
def _runner_for(engine):          return _lab_target_for(engine).run_for_search
def _context_loader_for(engine):  return _lab_target_for(engine).load_window_context
def _context_runner_for(engine):  return _lab_target_for(engine).run_with_context
```

**The sixth surface — `ops/engine_sdlc/default_params.py` becomes a thin SoT-delegating shim** (NOT deleted — it has its own non-Lab importers and is pinned by SP3 oracle behaviour; deleting it is out of SP-B scope). Its `default_params(engine)` body's `if engine ==` ladder is replaced by a single delegate to the same resolver, preserving its public signature, its lazy-import-in-`ops/` legality, and its raised-exception *type* (`ValueError`) while upgrading the message to the clear roster-aware one:

```python
# ops/engine_sdlc/default_params.py — body only; signature/exports unchanged
def default_params(engine: str) -> dict[str, Any]:
    from ops.lab.run import _lab_target_for      # lazy, ops→ops, legal
    return _lab_target_for(engine).default_params()
```

This removes the sixth hand-ladder and makes a future SP-F engine that declares `LAB_TARGET` *automatically* covered for `param_diff` too — the `_build_lab_result` crash-after-ledger-spend footgun (§0.1) is structurally impossible. The reverse import (`ops.engine_sdlc.default_params` → `ops.lab.run`) is `ops`→`ops`, fully legal, and lazy (no new eager import); a focused test pins no import cycle and that `default_params("sentinel")` raises the clear undeclared-`LAB_TARGET` `ValueError` (§5).

### §2.4 `PARAM_RANGES` — kept as a name, becomes a lazy mapping (with a hard `KeyError`-contract clause)

**Verified consumers of `PARAM_RANGES` (the skeptical pass under-counted — full grep, harden pass):**

1. `ops/lab/run.py:147` `sample_parameters`: `ranges = PARAM_RANGES[engine]` — subscript.
2. `scripts/tests/test_search_parameters_characterization.py:79`: `set(sp.PARAM_RANGES["reversion"])` — subscript + `set()`.
3. `tpcore/tests/test_engine_default_params_parity.py:16`: `set(PARAM_RANGES[engine])` for the literal triple — subscript.
4. **`ops/engine_sdlc/planner.py:694`: `ranges = PARAM_RANGES.get(ecr.engine, {})` — `.get()` with a default.** This is a **MODIFY-ECR validation path** — it gates whether a re-tuned param may reach a *live* engine (`no-smuggle H-S3-6c`). The skeptical pass did not enumerate it. **This is the load-bearing one.**

`collections.abc.Mapping.get(k, default)` is implemented *exactly* as `try: return self[k]; except KeyError: return default` — it catches **`KeyError` only**. Therefore the lazy `Mapping.__getitem__` **MUST raise `KeyError` (never `ValueError`)** for any engine `_lab_target_for` rejects (non-targetable / undeclared / RETIRED / lab / canary). If `__getitem__` let the `_lab_target_for` `ValueError` propagate, `planner.py:694`'s `.get(ecr.engine, {})` would **not** catch it and the live-adjacent MODIFY-ECR validator would crash with an unhandled `ValueError` instead of cleanly treating the engine as "no ranges → reject the param" — a real, live-path-adjacent behavior regression the skeptical design glossed.

**Exact contract (binding for the plan):**

- `_LazyParamRanges.__getitem__(engine)`: `try: return _lab_target_for(engine).param_ranges` `except ValueError as exc: raise KeyError(engine) from exc`. Bare `KeyError(engine)` — `Mapping.get`/`in`/`dict()`-coercion semantics are byte-preserved vs. the old literal dict; `planner.py:694` keeps cleanly returning `{}` for a non-targetable/undeclared engine (then its existing `param … not in ranges` reject fires — the *correct* behavior, unchanged).
- `__iter__`: yields `lab_targetable_engines()` order (dispatch_order: reversion, vector, momentum, sentinel) **filtered to declared targets** (sentinel skipped — no `LAB_TARGET`). Today yields exactly `(reversion, vector, momentum)` — **same membership and order as the old literal dict's insertion order** (verified `run.py:99,111,123`). `__len__` == count of that filtered iterable (3 today; was 3). No consumer calls `len(PARAM_RANGES)` (verified by the grep above) — `__len__` is provided only to satisfy the `Mapping` ABC.
- The *clear* operator-facing message: `sample_parameters` (`:146`) wraps the subscript so an operator who somehow reaches it with a bad engine sees the `_lab_target_for` `ValueError` text, not a bare `KeyError`. (The argparse `choices` gate, §2.5, rejects bad engines far earlier on every real path; this wrap is defence-in-depth for the programmatic `run_lab()` call and the legacy shim.)
- `__contains__` is derived from `__getitem__` by the `Mapping` ABC (`KeyError`→`False`) — `"sentinel" in PARAM_RANGES` is `False` (matches the old literal dict; `test_sentinel_canary_have_no_accessor` intent preserved).

> **Hardening note — decision RE-AFFIRMED with the `KeyError` clause:** the lazy `Mapping` is cleverness kept solely so the byte-frozen characterization oracle (`:73-79`) and the two other subscript consumers pass unmodified. The skeptical pass's "keep the lazy Mapping, minimum blast radius" verdict **stands** — *but only with the explicit `ValueError`→`KeyError` re-raise clause above*, which the skeptical draft omitted and which is non-optional (without it, `planner.py`'s live-adjacent ECR validator regresses). The alternative (rename `PARAM_RANGES`→function + rewrite the oracle) was reconsidered under this finding and still rejected: it would force editing the byte-frozen oracle AND `planner.py:694` AND the parity test in the same PR — *larger* blast radius on a live-money-adjacent path, weaker regression signal. The lazy Mapping with the pinned `KeyError` contract + its own unit test (subscript, `.get()`-returns-default-for-undeclared, `in`, iteration order, `set()` parity vs. a captured pre-refactor snapshot of the old literal) is the smaller, better-fenced change. **Eager-vs-lazy import-failure timing (attack #2):** today `PARAM_RANGES` is a literal built at `ops.lab.run` import; a malformed engine `backtest.py` surfaces only when `_runner_for` is *called* (already lazy). Post-SP-B the `param_ranges` resolution is *also* call-time-lazy (inside `_lab_target_for`) — **failure timing is unchanged for the engine-import failure** (it was always lazy via `_runner_for`); only the `PARAM_RANGES` *value* moves from import-time-eager to first-access-lazy, and the only access is inside `sample_parameters`/`planner`/oracle which are all themselves call-time. No live-money path imports `PARAM_RANGES` at all (grep-verified: zero scheduler/order-manager/plug reference). Net import-failure-timing delta on any live-adjacent path: **none**. Attack #2 resolved — see §8-A2.

### §2.5 CLI choices generated from the roster

`ops/lab/__main__.py:51-53` and `ops/lab/run.py:620`: replace the literal `choices=("reversion","vector","momentum")` with `choices=lab_targetable_engines()` (imported from `tpcore.engine_profile` — `tpcore` import in `ops/` is always legal; the accessor is engine-free, no eager engine import, preserving `__main__.py:18-20`'s no-eager-import contract). argparse `choices` containing an *eligible-but-undeclared* engine (sentinel today) is **intentional**: the operator sees `sentinel` as a choice, and if selected gets the clear §2.3 "has not declared LAB_TARGET" message — strictly better than silently hiding a roster engine (the SP-E forward-dep is then a *visible TODO*, not an invisible gap).

### §2.6 Consistency clockwork — `tpcore/tests/test_lab_targeting_consistency.py`

A pure runtime-derived consistency test (NOT a `gen_engine_manifest` shadow — argued §1). Assertions:

1. `set(lab_targetable_engines()) == { n for n,p in _PROFILE.items() if p.lifecycle_state in {LAB,PAPER,LIVE} and n not in {"allocator","lab","canary"} }` — the accessor *is* the roster predicate, not a hand-list.
2. `_parse_args(["--target-engine","reversion",...]).target_engine` works and a non-targetable choice (`canary`/`sigma`/`lab`) raises `SystemExit` from argparse (proves `choices` is the accessor, not a literal).
3. **Red-proof (the make-or-break, mirrors `test_leg6_fails_on_roster_drift`):** a synthetic `_PROFILE` mutation (add a fake `phantompaper` PAPER engine via `monkeypatch`/local dict) makes `lab_targetable_engines()` include it ⇒ assertion (1)'s LHS/RHS recompute consistently, BUT a second assertion proves the CLI `choices` and `_lab_target_for` *also* see it ⇒ a real roster change propagates to the Lab with zero Lab edits; conversely a roster engine removed (RETIRED) drops out of `choices` automatically. The non-vacuous proof: assert that with the synthetic engine present, `"phantompaper" in <CLI choices>` AND `_lab_target_for("phantompaper")` raises the clear undeclared-LAB_TARGET `ValueError` (not `KeyError`/`unknown engine`) — i.e. the new engine is *recognised as a roster Lab target awaiting declaration*, exactly the SP-F path.
4. A dedicated `test_canary_not_lab_targetable` and `test_lab_sentinel_not_lab_targetable` pinning the two explicit exclusions (regression-proof the §2.1 policy clauses).

This lives in `tpcore/tests/` alongside `test_engine_lifecycle_consistency.py` and carries the same `pytestmark = pytest.mark.xdist_group("ops_shadow")` (it touches `ops.lab` import surface; the ops-package-shadow single-process invariant, per the existing file `:40`).

---

## §3 Component / interface breakdown

| Unit | What it does | How used | Deps |
|---|---|---|---|
| `tpcore/lab/target.py::LabTarget` (NEW) | Frozen pydantic-v2 model: `param_ranges` + 4 callables (`run_for_search`/`load_window_context`/`run_with_context`/`default_params`); `model_post_init` validates the tuple/kind contract fail-loud at declaration time. | Each `<engine>.backtest` constructs one `LAB_TARGET`. `ops/lab/run.py` resolves+reads it. | pydantic, `collections.abc`, stdlib. Engine-FREE (check_imports green). |
| `<engine>/backtest.py::LAB_TARGET` (NEW constant, ×3 today: reversion/vector/momentum) | Engine-owned declaration of its Lab contract. Moves the `PARAM_RANGES[engine]` literal from `ops/lab/run.py` to the engine that owns those params. | Read by `_lab_target_for` via importlib. Never imported by the live scheduler/order-manager (live path byte-identical). | `tpcore.lab.target.LabTarget`; references functions already defined in the same module. |
| `tpcore.engine_profile.lab_targetable_engines()` (NEW accessor) + `_LAB_TARGETABLE` frozenset | The single roster authority for "which engines the Lab MAY fish against". | `ops/lab/run.py` resolver gate; CLI `choices` (both files); the consistency test. | stdlib only (same module as `roster_for_dispatch`). |
| `ops/lab/run.py::_lab_target_for` (NEW private resolver) | engine-name → `LabTarget` via roster gate + lazy importlib + clear `ValueError`s. | The 3 seam funcs + `sample_parameters`/`PARAM_RANGES` delegate to it. | `tpcore.engine_profile`, `importlib` (lazy engine import — legal in `ops/`). |
| `ops/lab/run.py::_runner_for/_context_loader_for/_context_runner_for` (REFACTORED bodies, names+sigs unchanged) | Thin views: `_lab_target_for(engine).<callable>`. | `_run_lab_core:778-780` (unchanged call sites); oracle monkeypatches by name (still binds). | `_lab_target_for`. |
| `ops/lab/run.py::PARAM_RANGES` (REFACTORED: literal dict → lazy `Mapping`) + `sample_parameters` (clear-error wrap) | `PARAM_RANGES[engine]` and `set(PARAM_RANGES[e])` keep working; unknown engine → clear `ValueError` not bare `KeyError`. | `sample_parameters:147`; oracle `:73-79`. | `_lab_target_for`. |
| `ops/lab/__main__.py` + `ops/lab/run.py` argparse (REFACTORED) | `choices=lab_targetable_engines()` (×2 sites). | CLI parse. | `tpcore.engine_profile` (engine-free, no eager engine import — `__main__.py:18-20` invariant preserved). |
| `tpcore/tests/test_lab_targeting_consistency.py` (NEW) | The SP-B clockwork: target set == roster SoT; CLI choices generated; red-proof on synthetic roster drift; canary/lab-sentinel exclusion pins. | CI (full single-process suite). | `tpcore.engine_profile`, `ops.lab.run`, `ops.lab.__main__`, pytest. `xdist_group("ops_shadow")`. |
| `tpcore/templates/engine_template/backtest.py` (AUGMENTED — forward dep for SP-F) | The 48-line stub gains a commented `LAB_TARGET = LabTarget(...)` skeleton + the 3 uniform `run_for_search`/`load_*`/`run_*` signatures, so a new engine (SP-F Catalyst) is Lab-targetable by construction. | Copy-paste start for new engines. | doc/scaffold only; not executed in tests beyond import-safety. |

| `ops/engine_sdlc/default_params.py::default_params` (REFACTORED: 3-tuple `if` ladder → thin `_lab_target_for(engine).default_params()` delegate; signature/`__all__` unchanged) | The SIXTH surface (§0.1). Supplies the `current` side of `LabResult.param_diff`. | `_build_lab_result:1057`; `ops/engine_sdlc/planner.py` (MODIFY-ECR `default_params` consumers, unchanged callers). | `ops.lab.run._lab_target_for` (lazy, ops→ops). |
| `tpcore/tests/test_engine_default_params_parity.py:35-38` (ONE assertion updated — beneficial message delta) | `test_dispatcher_rejects_unknown_engine` message pin moves from `"unknown engine: nope"` to the clear roster-aware `ValueError` text; exception *type* unchanged. | CI. | — |

**NON-component (explicitly NOT touched):** `tpcore/lab/ledger.py`, `tpcore/lab/context.py`, `compute_dsr_for_verdict`, `_run_lab_core`'s SP-A ledger block (`:745-759`, `:867-872`), `survived` gate (`:977-981`), every `<engine>` scheduler/order-manager/plug, `gen_engine_manifest.py` `_FILE_REGIONS`, `tpcore/lab/models.py::LabCandidate` (free-`str` `target_engine` intentionally left; §4.10), `tpcore/tests/test_engine_default_params_parity.py:8` literal triple (test-fixture stale-shadow, SP-E/SP-F territory; §4.11), `_score_for_ranking`/`rank_candidates` (SP-D).

---

## §4 Edge cases & failure modes

1. **Undeclared roster engine (sentinel today; any SP-F new engine pre-declaration).** Eligible by `lab_targetable_engines()` (PAPER) but `getattr(mod,"LAB_TARGET",None) is None` ⇒ **hard `ValueError` with the precise SP-E/SP-F-pointing message** (§2.3), surfaced before any DB/ledger work. **Decision: hard-reject, NOT silently absent.** Silent absence (hiding sentinel from `choices`) would re-create an invisible drift between "what the roster says is targetable" and "what the Lab will run" — the exact failure class SP-B deletes. Visible-eligible-but-must-declare is the SP-E deliverable made into a tested, self-documenting state. Tested by §2.6(3).
2. **`canary` selected.** `lab_targetable_engines()` excludes it by the explicit named clause (§2.1) ⇒ argparse `choices` rejects it at parse time (`SystemExit`), and `_lab_target_for("canary")` (if reached via the legacy `scripts/search_parameters.py` shim path bypassing argparse) raises the clear "not Lab-targetable" `ValueError`. No canary `lab_trial_ledger.canary` row is ever written (the resolver rejects *before* `_run_lab_core`'s `record_trial_spend`, `:752-759`) — the SP-A ledger stays uncontaminated by a structurally-impossible-to-graduate target.
3. **`lab` sentinel selected.** Excluded by the explicit `_LAB_SENTINEL` name clause (§2.1) — `test_lab_sentinel_is_not_wired` already guarantees no `lab/` package, so even absent the name clause `importlib.import_module("lab.backtest")` would `ModuleNotFoundError`; the name clause makes the rejection a *clear roster message* not an import traceback. Tested by §2.6(4).
4. **RETIRED (sigma) / allocator.** `sigma` ∉ `_LAB_TARGETABLE` (RETIRED); `allocator` excluded by the reused `_ALLOCATOR_ENGINE` filter. Both produce the clean "not Lab-targetable" `ValueError`. `archived_engines()`/`roster_for_dispatch()` unchanged ⇒ `test_engine_lifecycle_consistency.py` stays green.
5. **SP-A ledger interaction (the dependency invariant — non-negotiable).** A newly-roster-targetable engine's *first* Lab run: `cumulative_n_trials(pool, "<new>", spend_ts)` returns 0 (no prior `lab_trial_ledger.<new>` rows) ⇒ `effective_n_trials = 0 + args.trials` — byte-identical to SP-A's own T6/T9 floor. Every subsequent run accumulates. **SP-B changes zero ledger code; the ledger's free-`str` target key (§0, verified `ledger.py:40-46`) means roster widening is automatically ledger-covered.** A make-or-break test (§5) asserts: for a synthetic new target, the SP-A cumulative-deflation path is invoked with `target=<new engine>` and `effective_n_trials` grows monotonically across runs — i.e. SP-B did not re-open SP-A's hole for the newly-targetable set.
6. **Oracle monkeypatch survival.** The oracle patches `ops.lab.run._runner_for` etc. *by name* with `lambda e: _fake`. Because the names+`(engine)->callable` signatures are unchanged, the patch still fully shadows `_lab_target_for` (the fake never calls the resolver). `set(PARAM_RANGES["reversion"])` still works via the lazy `Mapping`. **The characterization oracle passes unmodified — the strongest available proof that `_run_lab_core`/`amain` observable behaviour is byte-identical post-refactor.** (If, contrary to design, an oracle change proves unavoidable, it is allowed per the oracle docstring `:25-26` — but the design target is zero oracle churn.)
7. **`importlib.import_module` failure for a declared-eligible engine** (e.g. a syntax error in `<engine>.backtest`). Caught → clear `ValueError` naming the engine+module (§2.3) — never a bare `ImportError` traceback to the operator; fail-loud, never silent.
8. **Two `choices` sites drift from each other** (`__main__.py` vs `run.py`). Both now reference `lab_targetable_engines()` ⇒ they cannot drift; §2.6(2) pins both resolve from the accessor.
9. **`run.py` `--engine` is the *legacy operator* path (candidate is None), `__main__.py` `--target-engine` is the Lab path (candidate set).** Both must accept exactly the roster-targetable set. The legacy `scripts/search_parameters.py` shim (re-export of `ops.lab.run`) inherits the new `choices` automatically — its characterization test (`scripts/tests/test_search_parameters_characterization.py`) is the §6 guard that the legacy CLI contract is preserved.
10. **`LabCandidate.target_engine` is a free `str`, NOT a `choices`-bounded field** (verified `tpcore/lab/models.py:14` — `target_engine: str`, no validator/enum). The skeptical pass implied the argparse `choices` is the only target-name gate; verified true — `_amain` (`__main__.py:128-134`) builds `LabCandidate(target_engine=ns.target_engine)` *after* argparse already validated `ns.target_engine ∈ choices`, and `_run_lab_core` keys everything off `args.engine` (never `candidate.target_engine` for resolution; `candidate.target_engine` is only echoed into the `LabResult`/dossier). **So there is NO independent enumeration in the model to desync** — the model intentionally trusts the CLI gate. SP-B does NOT add a validator to `LabCandidate` (that would be a *second* targetable-set authority — the parallel-SoT anti-pattern; the argparse `choices=lab_targetable_engines()` is the single gate, and a programmatic `run_lab(candidate=…)` caller that hand-builds a bad `target_engine` is caught by `_lab_target_for` at `sample_parameters`/`_runner_for` time with the clear `ValueError`, before any ledger spend). Tested: a `run_lab()` invoked with a hand-built `LabCandidate(target_engine="canary")` hard-rejects with the clear `ValueError` and writes **no** `lab_trial_ledger.canary` row.
11. **`ops/engine_sdlc/default_params.py` hard-pinned oracle (`tpcore/tests/test_engine_default_params_parity.py:35-38`).** `test_dispatcher_rejects_unknown_engine` asserts `default_params("nope")` raises `ValueError, match="unknown engine: nope"` — a **hard message pin** (no docstring sanction, unlike the characterization oracle). The §2.3 shim changes that message to the clear roster-aware text. **This is a deliberate, beneficial behavior delta and SP-B MUST update this single assertion in the same change** (the new message is strictly more informative; the exception *type* `ValueError` is preserved). Recorded as a known oracle-update in §5 + §8-B6. `test_each_param_ranges_engine_default_keyset_equals_param_ranges` (parametrized over the literal `("reversion","vector","momentum")`) stays GREEN (subscript still resolves for the declared three); `test_sentinel_canary_have_no_accessor` stays GREEN (sentinel/canary still expose no `backtest.default_params` until SP-E). The literal triple in that test (`:8`) is a stale-but-non-breaking shadow — **explicitly left for SP-E/SP-F** (it is a *test fixture*, not a production SoT, and widening it now without a declared engine to widen *to* would be premature; SP-F's "first roster-driven new target" deliverable updates it then). Flagged, not fixed — §8-B7.

---

## §5 Test strategy

**Characterize-before-refactor (the dispatch core):**

- The existing `scripts/tests/test_search_parameters_characterization.py` **IS** the characterization oracle for `_runner_for`/`_context_*_for`/`PARAM_RANGES`/`sample_parameters`/`amain`. T0 of phasing runs it RED-free on the unmodified tree (baseline), then GREEN-unmodified post-refactor is the pass criterion. **Design intent: this file is not edited.**
- Add focused unit tests for the new units: `LabTarget` model validation (good + bad tuple/kind → fail-loud at construction; the 4th `default_params` callable present/typed); `_lab_target_for` (resolves each declared engine incl. `.default_params`; clear `ValueError` for undeclared/non-targetable/RETIRED/lab/canary); the lazy `PARAM_RANGES` `Mapping` — **specifically pin the `ValueError`→`KeyError` re-raise contract (§2.4)**: `PARAM_RANGES["sentinel"]` raises `KeyError` (not `ValueError`), `PARAM_RANGES.get("sentinel", {}) == {}` (the `planner.py:694` live-adjacent path), `"sentinel" in PARAM_RANGES is False`, iteration order == `(reversion,vector,momentum)`, and `set(PARAM_RANGES["reversion"])` == a snapshot of the *old literal dict's* `reversion` keyset captured at T0 (byte-parity vs. the deleted literal).
- **`default_params` shim tests:** `default_params("reversion"/"vector"/"momentum")` byte-equal to pre-refactor; `default_params("sentinel")` raises the clear undeclared-`LAB_TARGET` `ValueError`; no `ops.engine_sdlc.default_params`↔`ops.lab.run` import cycle (import each first; assert clean). **Update `test_engine_default_params_parity.py:35-38` `test_dispatcher_rejects_unknown_engine`** in the SAME change: the `match=` regex moves from `"unknown engine: nope"` to the new clear-message substring (e.g. `"is not Lab-targetable"`); exception type stays `ValueError`. This is the one sanctioned oracle/test delta (§4.11, §8-B6) — call it out explicitly in the plan T-step + commit message.

**The clockwork red-proof (make-or-break, mirrors `test_leg6_fails_on_roster_drift`):**

- `test_lab_targeting_consistency.py` §2.6(3): synthetic roster mutation ⇒ Lab target set + CLI choices + resolver all track it with zero Lab-file edits; a removed (RETIRED) engine drops automatically. Assert non-vacuous (the synthetic engine is genuinely seen as a roster Lab target awaiting `LAB_TARGET`).
- §2.6(4): canary ∉ targetable, lab-sentinel ∉ targetable (policy-clause regression pins).

**SP-A non-regression (the dependency invariant):**

- A test that for a synthetic newly-targetable engine, `_run_lab_core` (with a fake ledger pool) calls `record_trial_spend(target=<new>)` then `cumulative_n_trials(target=<new>)` and `effective_n_trials` is `cumulative + args.trials`, growing monotonically across two runs — proving SP-B did not re-open SP-A's hole for the widened set. Reuse the SP-A test harness shape from `tpcore/tests/test_lab_ntrials_ledger.py` (do not duplicate ledger logic).

**Import-layering / lane:**

- `tpcore.scripts.check_imports` green (`tpcore/lab/target.py` engine-free; the engine imports it, not vice-versa). ruff exact. The data lane is untouched (assert no `tpcore/quality|providers|selfheal|...` diff).
- Confirm `import ops.lab.__main__` still eager-imports NO engine (`__main__.py:18-20` invariant) — a test importing it and asserting no `reversion`/`vector`/`momentum`/`sentinel`/`canary` in `sys.modules` (mirrors `test_clockwork_imports_no_ops` shape).

**Authoritative gate (CI-green boundary):**

- The full **single-process** suite + the **order-flip** rerun (memory: subset/parallel green ≠ CI green; ops/ package-shadow is single-process — the new test carries `xdist_group("ops_shadow")`). Real gate = `gh pr checks`.
- `python scripts/gen_engine_manifest.py --check` stays green (SP-B adds NO new fenced shadow — proves §1's "remove a shadow, don't add one").

---

## §6 Scope boundary / NON-GOALS

- **NO live trading path change.** No `<engine>` scheduler/order-manager/plug edit. `LAB_TARGET` is a module-level constant the live path never imports. A test asserts the live import surface is unchanged.
- **NO SP-A re-touch.** Zero edits to `tpcore/lab/ledger.py`, the `_run_lab_core` ledger block (`:745-759`,`:867-872`), `compute_dsr_for_verdict`, or the `survived` gate (`:977-981`). SP-B is *dispatch indirection across all six surfaces + one accessor + one clockwork test + the bounded `default_params` shim & its one parity-test message-pin update* — no SP-A code path, no scoring (`_score_for_ranking`), no `_PROFILE` write. The disjoint-concern boundary the epic §"Decomposition risk notes" mandates is preserved (the §0.1 sixth surface is *dispatch*, not SP-D scoring — argued §0.1).
- **NO SP-C (readiness checklist), SP-D (pluggable scoring), SP-E (Sentinel candidate), SP-F (Catalyst engine), SP-G (LLM emitter).** Sentinel/Catalyst becoming actual Lab targets is SP-E/SP-F's deliverable (declaring `LAB_TARGET`); SP-B only makes the *mechanism* roster-driven and makes "eligible-but-undeclared" a clear, tested state. `_score_for_ranking` is untouched (SP-D).
- **NO new SoT / no registry singleton / no `engine_profile` capability descriptor.** The roster `_PROFILE` stays the single SoT; `lab_targetable_engines()` is a *derived view*, not a parallel SoT. No import-time registry population.
- **NO `gen_engine_manifest` shadow for the Lab target set** (argued §1 — it is runtime-derived; a byte-shadow would reintroduce drift).
- **YAGNI:** no generalized "non-graduating engine" predicate (canary is N=1 — explicit clause + test); no per-engine universe/window declaration in `LabTarget` (the existing uniform `(*, db_url,start,end,universe,...)` signature already covers it — verified §0; do not invent fields no caller needs).
- **Engine roster edits remain ECR-only.** SP-B does not add/remove any `_PROFILE` entry; it adds a *read accessor* over the existing SoT.

---

## §7 Phasing hint for writing-plans (T0..Tn shape)

- **T0 — Characterization baseline (no prod code).** Run `scripts/tests/test_search_parameters_characterization.py` + `tpcore/tests/test_engine_lifecycle_consistency.py` green on the untouched tree; record the byte-baseline (the oracle is the regression contract for T3–T5).
- **T1 — `tpcore/lab/target.py::LabTarget`** + its unit tests (model validation, fail-loud tuple/kind contract). Engine-free; `check_imports` green. Mergeable-inert (nothing imports it yet).
- **T2 — `tpcore.engine_profile.lab_targetable_engines()` + `_LAB_TARGETABLE`** + accessor unit tests (predicate table §2.1, canary/lab exclusions, sentinel included). No consumer yet.
- **T3 — Engine `LAB_TARGET` declarations** (reversion/vector/momentum `backtest.py`) — all **four** callables (`run_for_search`/`load_*`/`run_*`/`default_params`) + the `param_ranges` dict, moving the `PARAM_RANGES[engine]` literals to the owning engine. Assert each engine's live import surface unchanged (no scheduler/order-manager edit).
- **T4 — `ops/lab/run.py` dispatch indirection + the sixth-surface shim:** `_lab_target_for` (resolving all 4 callables) + the 3 seam-view rebodies + lazy `PARAM_RANGES` `Mapping` **with the pinned `ValueError`→`KeyError` re-raise contract (§2.4)** + `sample_parameters` clear-error wrap + `ops/engine_sdlc/default_params.py` body→thin delegate (§2.3) + the ONE `test_engine_default_params_parity.py:35-38` message-pin update (§4.11). **Gate: the characterization oracle passes UNMODIFIED; the `default_params` parity-test changes by exactly one `match=` regex; `planner.py:694` `.get()` path unit-test green.**
- **T5 — CLI choices from the roster** (`ops/lab/__main__.py` + `ops/lab/run.py` argparse) + the no-eager-import invariant test.
- **T6 — The SP-B clockwork** `tpcore/tests/test_lab_targeting_consistency.py` (target-set==roster, CLI-generated, red-proof on synthetic drift, canary/lab pins) + the SP-A non-regression test (cumulative deflation for a synthetic widened target).
- **T7 — Forward-dep scaffold:** augment `tpcore/templates/engine_template/backtest.py` with the `LAB_TARGET` skeleton + uniform signatures (so SP-F Catalyst is Lab-targetable by construction). Doc/scaffold only.
- **T8 — Authoritative gate:** full single-process suite + order-flip + `gen_engine_manifest.py --check` green; `gh pr checks`. Squash-merge; handoff.

> Suggested split: T1–T3 (contract + declarations) as review-unit A; T4–T6 (dispatch + clockwork, the risk core) as review-unit B; T7 trivial. Each unit gets a separate spec-compliance reviewer then a fresh-context code-quality reviewer (split-review discipline).

---

## Self-review pass (placeholders / contradictions / ambiguity / scope)

- **Contradiction check — "remove a shadow, don't add one":** PASS. The lazy `PARAM_RANGES` `Mapping` and the engine `LAB_TARGET` constants are *the SoT-derived data*, not a copy; the *deleted* literal `PARAM_RANGES` dict + the two literal `choices` tuples were the stale shadow. The clockwork is a runtime equality test, not a byte-shadow (§1). No new `gen_engine_manifest` region.
- **Ambiguity resolved — "any engine in `LifecycleState.{LAB,PAPER,LIVE}`" (epic §1 SP-B verbatim) vs canary/lab being PAPER/LAB:** the epic's lifecycle-set phrasing, read literally, would include canary (PAPER) and the `lab` sentinel (LAB). Resolved (§2.1, §4b) by: canary excluded with an explicit policy clause + test (it is non-graduating by construction — a Lab graduation verdict against it is a category error that would still spend SP-A ledger budget); the `lab` sentinel excluded (it is not a runnable engine — no package, `test_lab_sentinel_is_not_wired`). Both exclusions are *narrow, named, tested* — they refine the epic's lifecycle-set into the *operationally correct* Lab-targetable set without contradicting it (LAB-state targetability is preserved for a *real* future LAB-graduated candidate; the only LAB entity today is the non-runnable sentinel). **This is the primary epic-ambiguity and the resolution is explicitly surfaced for the harden pass.**
- **Ambiguity resolved — "param-range registration declared by/derived from the engine":** chosen as a per-engine `LAB_TARGET` constant *in the engine's own `backtest.py`* (engine-owned), resolved by importlib — not an `ops/lab` registry, not an `engine_profile` field. Justified §1.
- **Scope check:** every NON-GOAL in §6 maps to a sibling SP (A/C/D/E/F/G) or an explicit YAGNI. The §0.1 sixth surface (`default_params`) is *in* SP-B scope (dispatch, not SP-D scoring — argued §0.1; the epic's "five surfaces" framing was a scope error, corrected here). No SP-A code path appears in any T-step. The data lane is untouched.
- **Placeholder scan:** no TODO/TBD/`<fill>` remain; every `file:line` cite was read in-repo (§0; the harden pass re-verified `default_params.py:13-22`, `planner.py:694`, `models.py:14`, `test_engine_default_params_parity.py:8,35-38`).
- **Internal-consistency re-check (§1↔§2↔§4↔§6, post-harden):** §1 "all six surfaces / four callables" ↔ §2.2 four-callable `LabTarget` ↔ §2.3 `default_params` shim ↔ §3 component rows ↔ §4.10/4.11 edge cases ↔ §6 "across all six surfaces … no scoring" — all aligned, no residual "five surfaces"/"three callables"/"pure dispatch indirection" contradiction (grep-swept, fixed inline).
- **Sharpest residual risk (post-harden, handed to the plan):** the lazy `PARAM_RANGES` `Mapping`'s `ValueError`→`KeyError` re-raise contract (§2.4) — it is the single clause standing between the design and a real regression of the live-adjacent `planner.py:694` MODIFY-ECR validator. The plan must gate T4 on a dedicated `PARAM_RANGES.get("sentinel", {}) == {}` test, not just the characterization oracle. See §8.

---

## §8 Adversarial hardening record (attack → resolution)

**Verdict: SOUND-AFTER-HARDENING.** The core mechanism (convention importlib resolver + engine-owned `LAB_TARGET` + roster accessor + runtime clockwork) was correct. One material gap (a missed sixth ladder), one missing binding clause (the `KeyError` re-raise), and several under-counted consumers were found and fixed in the design above.

**Accepted criticisms — design changed:**

- **§8-B6 (material — the single sharpest finding): missed sixth hardwired ladder `ops/engine_sdlc/default_params.py`.** The skeptical pass + epic both said "five surfaces" and scoped `default_params` to SP-D. Verified false: `default_params` is pure name→module dispatch (a `_runner_for` clone) feeding `param_diff` provenance, reached on the *success path* of `run_lab()` (`run.py:1057`) — the primary SP-B path. Left un-refactored it would convert SP-F's failure into "full walk-forward + irreversible SP-A ledger spend, then `unknown engine` crash in `_build_lab_result`". **Resolution:** added §0.1; `default_params` is now the 4th `LabTarget` callable; `ops/engine_sdlc/default_params.py` becomes a thin `_lab_target_for(...).default_params()` delegate (§2.3); its one hard-pinned oracle assertion (`test_engine_default_params_parity.py:35-38`) gets a sanctioned one-line message-pin update (§4.11, §5, T4).
- **§8-A4 (attack #3, ledger-mid-resolution): partial ledger write on hard-reject?** Verified the ordering in `_run_lab_core`: `sample_parameters(args.engine,…)` (`:730`) → (now) `_lab_target_for` hard-reject fires *here* → `record_trial_spend` is `:752-759`, strictly *after*. **No partial ledger write is possible**: every reject path (non-targetable / undeclared / RETIRED / lab / canary / bad import) raises inside `sample_parameters`→`PARAM_RANGES[engine]`→`_lab_target_for`, before the ledger block and before any DB work. The argparse `choices` gate rejects even earlier on every real CLI path. Pinned by an explicit test (§4.2, §4.10). Design unchanged but the invariant is now *verified and stated load-bearing in §0(1)*.
- **§8-A2 (attack #2, lazy `Mapping` behavior change): the `ValueError`→`KeyError` re-raise was unspecified and is non-optional.** `collections.abc.Mapping.get()` catches `KeyError` only; `planner.py:694` does `PARAM_RANGES.get(ecr.engine, {})` on a *live-adjacent MODIFY-ECR validation path* the skeptical pass did not enumerate. Without the re-raise clause that path crashes with an unhandled `ValueError`. **Resolution:** §2.4 rewritten with a binding `__getitem__: except ValueError → raise KeyError(engine) from exc` contract, full verified-consumer list, `in`/`get`/iteration-order/`len` semantics pinned vs. the old literal, and a dedicated test mandated (§5). Eager-vs-lazy import-failure timing analyzed: **no delta on any live-adjacent path** (engine import was already lazy via `_runner_for`; no live-money code imports `PARAM_RANGES`).
- **§8-B?: `LabCandidate.target_engine` free-`str` enumeration check.** Verified `tpcore/lab/models.py:14` has no validator — *intentionally* trusts the argparse `choices` gate; adding a validator would create a second targetable-set authority (parallel-SoT anti-pattern). **No model change** (correct as-is); added §4.10 documenting the verified single-gate invariant + a programmatic-`run_lab()`-with-bad-target hard-reject-before-spend test.

**Criticisms rejected — with rationale:**

- **§8-B1 (attack #1: replace the `"canary"` string with an `EngineProfile.lab_targetable: bool` SoT field).** *Rejected.* Putting `lab_targetable` in `_PROFILE` would (a) push a Lab-only concern into a module the live dispatch path imports (`should_fire`), polluting a live-critical frozen SoT for an N=1 need; (b) make every ECR ADD/REMOVE now also reason about a Lab flag, widening the ECR's blast radius for no roster-level benefit; (c) the SP4 clockwork (`gen_engine_manifest`) shadows `roster_for_dispatch()`, not arbitrary profile fields — a new bool field is *not* mechanically drift-checked by SP4, so it would be *weaker* than a named predicate clause + a dedicated forcing test. The named `if engine != "canary"` clause **is** the SoT predicate (it lives in the one accessor over `_PROFILE`, with an inline §4b citation + `test_canary_not_lab_targetable`), is N=1, and is *more* drift-proof than a profile bool. The "hardcoded string is itself a shadow" objection is answered: it is a *single* derivation site with a forcing test, not a copy of a list — the Sigma 22-site-drift class requires ≥2 copies; this is 1. The canary spec §4b decision is referenced, not duplicated.
- **§8-B5 (`LabTarget` should be a `NamedTuple`/dataclass, not pydantic; `tpcore/lab/target.py` is an unjustified new file).** *Rejected.* `model_post_init` fail-loud validation of the `(low,high,kind)` tuple contract at *declaration time* is load-bearing (a plain container defers the malformed-range error to sample time on a live-money-adjacent path). The file sits with the established engine-free Lab contract layer (`tpcore/lab/{ledger,context,models}.py`) — consistent placement, not a new pattern; folding it into `models.py` would mix the SP2→SP3 frozen result contract with the SP-B dispatch contract (distinct concerns). YAGNI line of §1 amended to record this.
- **§8-B7 (the `_PARAM_RANGES_ENGINES = ("reversion","vector","momentum")` literal triple in `test_engine_default_params_parity.py:8` is a 7th shadow SP-B must delete).** *Rejected for SP-B (deferred to SP-E/SP-F, flagged not fixed).* It is a *test fixture*, not a production SoT; it stays GREEN post-SP-B (subscript resolves for the declared three). Widening it now is premature — there is no new declared engine to widen *to* until SP-F; SP-F's "first roster-driven new target" deliverable is exactly where it gets parametrized off `lab_targetable_engines()`. Documented in §4.11 + §6 NON-component so it cannot be silently forgotten.
- **§8-clockwork (does the runtime clockwork reintroduce a second "which engines exist" source vs. `gen_engine_manifest`?).** *Rejected — no reintroduction.* `gen_engine_manifest` shadows exist only because bash/TOML/docstring files *cannot* import Python at parse time and must physically contain the roster. The Lab target set is runtime-derived from the same `_PROFILE` via one accessor — there is no second *frozen text* copy to drift, so a byte-shadow would be the anti-pattern. `gen_engine_manifest.py --check` stays green precisely because SP-B adds *no* fenced region (verified `_FILE_REGIONS` does not and must not cover `ops/lab/*`). The clockwork is an equality test (`accessor == predicate-over-_PROFILE`), the same *intent* as `test_provider_lifecycle_consistency.py`, not a parallel manifest. §1 argument re-affirmed; no change.

**Highest residual risk handed to the implementation plan:** the §2.4 lazy-`Mapping` `ValueError`→`KeyError` re-raise contract. It is one clause, but it is the only thing preventing a regression of `planner.py:694` — a path that gates re-tuned params reaching a *live* engine via MODIFY-ECR. The plan MUST make T4's gate include a dedicated `PARAM_RANGES.get("<undeclared>", {}) == {}` + `"<undeclared>" in PARAM_RANGES is False` + `KeyError`-not-`ValueError`-on-subscript test, independent of (and in addition to) the characterization oracle — the oracle only exercises declared engines and would not catch a `ValueError`-leak regression on the undeclared/`.get()` path.
