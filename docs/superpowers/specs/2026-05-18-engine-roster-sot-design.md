# Engine SDLC — SP1: Unified Engine Roster SoT — Design Spec

**Status:** approved design. **Epic:** Engine SDLC (operator-approved 4-chain: **SP1 Roster SoT** → SP2 Lab → SP3 ECR+transitions → SP4 docs+shadow-closure). **Lane:** ENGINE. This is sub-project 1 — the #13 first brick. FORMALIZE-AND-UNIFY (~80% compose-existing), NOT a parallel build.

## 1. Problem

The engine roster is duplicated across ~10 sites with no mechanical link. `ops/engine_dispatch.py:28` `ROSTER = ("reversion","vector","momentum","sentinel","canary")` is the *runtime dispatch SoT* (an ordered tuple, hand-maintained). `tpcore/engine_profile.py:44-52` `_PROFILE` is the *cadence/gate SoT* (also has `allocator`). They must agree but are not linked — the Sigma archival (#170) exposed the brittleness (it lingered in shadows after removal). SP1 makes `tpcore.engine_profile` the single mechanically-enforced SoT for "what engines exist, in what dispatch order, with what cadence and lifecycle classification" and derives every *Python* shadow from it, **without changing live dispatch behavior**.

**Grounding correction (verified on `main` d07d6c0):** `scripts/check_imports.py` has NO `ENGINE_PACKAGES` symbol — that duplication point does not exist; do not invent a binding for it. The shadow set is ~10.

## 2. Lane discipline (hard)

ENGINE lane only. The DATA-SDLC files are a **READ-ONLY symmetry reference, never edited**: `tpcore/providers.py`, `tpcore/tests/test_provider_lifecycle_consistency.py`, `docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`, `docs/superpowers/checklists/data_feed_change_request.md`, `ops/weekly_digest.py`, `tpcore/ladder|selfheal|feeds|ingestion|datasupervisor`, `ops/data_repair_service.py|cutover_agent.py`, `scripts/run_data_operations.sh`. Never local-merge into the shared main checkout; never stomp the data session. Typed, pydantic v2, structlog; no private `_PROFILE` access outside `engine_profile` (public accessors only).

## 3. Target SoT shape

Extend the existing frozen model at `tpcore/engine_profile.py:35-42`:

```python
class LifecycleState(StrEnum):
    LAB     = "lab"      # SP2 territory; never dispatched/allocated
    PAPER   = "paper"    # graduated, paper-trading (current reality for all live engines)
    LIVE    = "live"     # reserved; no engine here yet (paper-only mandate)
    RETIRED = "retired"  # snap-out complete; archive/EULOGY exists; never dispatched

class EngineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    cadence: Cadence
    market_closed_required: bool = True
    dispatch_order: int                 # NEW — total order; the ROSTER tuple's authority
    lifecycle_state: LifecycleState     # NEW
    allocator_eligible: bool = False    # NEW — replaces the hand-typed allocator subset
```

Initial `_PROFILE` (behavior-preserving — `dispatch_order` 1-5 reproduces `engine_dispatch.ROSTER` order exactly; `allocator`=0 keeps its pre-loop path; `sigma` enters as `RETIRED`):

| engine | cadence | dispatch_order | lifecycle_state | allocator_eligible |
|--------|---------|---------------:|-----------------|--------------------|
| allocator | WEEKLY_FIRST_TRADING_DAY | 0 | PAPER | False |
| reversion | DAILY | 1 | PAPER | True |
| vector | DAILY | 2 | PAPER | True |
| momentum | MONTHLY_FIRST_TRADING_DAY | 3 | PAPER | True |
| sentinel | DAILY | 4 | PAPER | False |
| canary | DAILY | 5 | PAPER | False |
| sigma | DAILY | 99 | RETIRED | False |

(`sigma`'s `cadence`/`dispatch_order` are inert — `RETIRED` engines are filtered out of every dispatch/allocator accessor by construction; values chosen so the frozen model validates and `dispatch_order` stays unique among non-retired.)

Public accessors (the API consumers query — no private `_PROFILE` access elsewhere):
- `roster_for_dispatch() -> tuple[str, ...]` — engines with `lifecycle_state in {PAPER, LIVE}` AND `engine != "allocator"`, **sorted by `dispatch_order` ascending**. This is what `engine_dispatch.ROSTER` becomes.
- `allocator_eligible_engines() -> tuple[str, ...]` — `allocator_eligible == True` (sorted by `dispatch_order`). Replaces `tpcore/allocator/service.py:151` literal.
- `archived_engines() -> tuple[str, ...]` — `lifecycle_state == RETIRED` (sorted by name). Replaces `tpcore/allocator/service.py:87` `_ARCHIVED_ENGINES`.
- Existing `profile_for`, `should_fire`, `cadence_window_start`, `Cadence` — **untouched**.

## 4. Which shadows derive in SP1

| Shadow | SP1 action | Mechanism |
|--------|-----------|-----------|
| `ops/engine_dispatch.py:28` `ROSTER` | **DERIVE** | `ROSTER = roster_for_dispatch()` (module-level call at import) |
| `tpcore/allocator/service.py:151` engines default | **DERIVE** | default sourced from `allocator_eligible_engines()` |
| `tpcore/allocator/service.py:87` `_ARCHIVED_ENGINES` | **DERIVE** | `= archived_engines()` |
| `tpcore/quality/validation/capital_gate.py:60` `ENGINE_TABLES` | **KEEP — documented seam** | data-dependency map (frozenset of tables per engine), NOT a name list. SP1 adds a CI test: `set(ENGINE_TABLES) ⊆ non-retired engine names`. Not collapsed. |
| `scripts/run_all_engines.sh`, `scripts/run_smoke_test.sh:51`, `ops/platform_pipeline.py` docstring, `pyproject.toml` packages/testpaths | **DRIFT-DETECTION TEST ONLY** | non-Python; can't import at parse time. SP1 adds a read-only test asserting each list ⊆/== the SoT-derived live roster. Auto-regeneration deferred to SP4. |
| `tpcore/engine_profile._PROFILE` | **becomes the SoT** | no longer a shadow — the origin |

SP1 rule: every *Python* consumer derives; every *non-Python* shadow gets a **drift-detection test** in SP1 (auto-regeneration is SP4).

## 5. Behavior preservation (non-negotiable — roster changes are high-risk per Sub-C/DA-3)

- HARD invariant test: `roster_for_dispatch() == ("reversion","vector","momentum","sentinel","canary")` — asserted as a frozen literal so any future SoT edit that reorders/adds/drops a dispatched engine fails CI loudly.
- `allocator` remains dispatched ONLY via `_dispatch_allocator` (`engine_dispatch.py:242`), before the ROSTER loop — SP1 does NOT merge it into the loop. `roster_for_dispatch()` excludes `allocator` by construction.
- `_dispatch_engine`, `should_fire`, `cadence_window_start`, `dispatch_once`'s structure — **untouched**. SP1 changes only *where the engine list comes from*, never *how an engine is gated/dispatched*.
- Allocator: the resolved `_ARCHIVED_ENGINES` replacement must equal `("sigma",)` and the eligible-subset replacement must equal `("reversion","vector","momentum")` — both test-pinned to the literals they replace (byte-equivalent behavior).
- No DB schema change (lifecycle lives in the frozen Pydantic model, as `_PROFILE` does today). No `application_log`/migration touch.

## 6. The N-way CI consistency test (SP1 scope)

New `tpcore/tests/test_engine_lifecycle_consistency.py`, modeled on `tpcore/tests/test_provider_lifecycle_consistency.py`'s "half-retirement fails the build" discipline (symmetry-reference, not clone). Asserts:

1. **Order invariant:** `roster_for_dispatch()` equals the frozen literal `("reversion","vector","momentum","sentinel","canary")`.
2. **Live ⇒ wired:** every `PAPER`/`LIVE` engine has a top-level `<engine>/` package dir, an `<engine>/tests/` dir, and an importable `<engine>.scheduler` (the module `engine_dispatch._invoke_scheduler` spawns — confirm the exact attribute by reading `ops/engine_dispatch.py`).
3. **Retired ⇒ absent:** every `RETIRED` engine is NOT in `roster_for_dispatch()`, NOT in `allocator_eligible_engines()`, IS in `archived_engines()`, and has `archive/<engine>/EULOGY.md` present (partial archive leg — completed in SP3).
4. **No-half-state sanity:** no engine both `RETIRED` and `allocator_eligible`; engine names unique; `dispatch_order` unique among non-`RETIRED` engines.
5. **Shadow-drift detection:** `set(ENGINE_TABLES)`, the `run_smoke_test.sh` step-3 loop list, the `pyproject.toml` testpaths engine dirs, and the `run_all_engines.sh`/`platform_pipeline.py` dispatched list each ⊆ (or == where exact) the SoT live roster. Detection only; SP4 regenerates.

Same clockwork as the data 3-way test ("a new/removed engine fails the build until the SoT is updated"), engine-domain legs (EngineProfile ↔ package/tests/scheduler ↔ archive/EULOGY).

## 7. Archived-engine handling — D-SDLC1-2

`sigma` enters `_PROFILE` as a `RETIRED` entry; `archived_engines()` derives `("sigma",)`; `tpcore/allocator/service.py`'s risk_state-cleanup consumer reads `archived_engines()` instead of the `_ARCHIVED_ENGINES` literal. This is the data-SDLC `ProviderStatus.RETIRED` pattern (`tpcore/providers.py`, "offboarded; kept for provenance only") applied to engines — provenance-in-SoT, not a side allowlist. Net behavior identical (resolved tuple `("sigma",)`, test-pinned). Verify `sigma` is NOT a real importable package (it's in `archive/sigma/`) — the "Live ⇒ wired" test (§6.2) must only apply to `PAPER`/`LIVE`, never `RETIRED`, so a `RETIRED` sigma with no top-level package is correct, not a failure.

## 8. Decisions

| ID | Decision |
|----|----------|
| D-SDLC1-1 | SP1 derives only the 3 Python shadows (ROSTER, allocator subset, _ARCHIVED_ENGINES); ENGINE_TABLES keeps a documented seam + drift test; bash/pyproject get drift-detection tests only (regeneration → SP4). |
| D-SDLC1-2 | `RETIRED` lifecycle state replaces `_ARCHIVED_ENGINES`; resolved behavior test-pinned to `("sigma",)`. |
| D-SDLC1-3 | `lifecycle_state` field+enum ship in SP1; transition logic ships in SP3. Data in SP1, behavior where used. |
| D-SDLC1-4 | `allocator` stays a separate dispatch path; SP1 does NOT merge it into the ROSTER loop. |
| D-SDLC1-5 | The N-way test's archive/EULOGY leg is partial in SP1 (presence check), completed in SP3. |
| D-SDLC1-6 | `dispatch_order`/`cadence` for the `RETIRED` sigma entry are inert placeholders chosen so the frozen model validates and `dispatch_order` is unique among non-retired; never consumed. |

## 9. Symmetry-vs-divergence (SP1 scope)

ADOPT (parallel to data-SDLC): flat-SoT registry (`_PROFILE` formalized like `_BINDINGS`), status `StrEnum` (`LifecycleState` like `ProviderStatus`), N-way CI consistency test (engine legs, data test as the structural oracle). DIVERGE: no `CUTOVER` analogue (do not port); graduation = DSR/credibility (SP2/SP3, not SP1); archive/EULOGY is a physical code move (SP3), heavier than a status flip. SP1 ships ONLY the SoT+enum+accessors+derivations+N-way test — no Lab, no ECR, no transitions, no graduation logic.

## 10. Migration / rollback

Pure-code, no DB. One PR, isolated worktree. Rollback = revert the PR (ROSTER reverts to its literal tuple; `_PROFILE` to 6 entries; allocator literals restored). Zero persisted state. The §5 order-invariant test guarantees a botched SoT edit fails CI pre-merge, so rollback is a pre-merge concern. Lowest-risk of the 4 sub-projects (no runtime-path logic change, behavior test-pinned to literals).

## 11. Out of scope (SP1)

The Lab / LAB-state behavior (SP2); the Engine Change Request + transition logic (SP3); the full SDLC spec doc + bash/pyproject auto-regeneration + CLAUDE.md/OPERATIONS.md/glossary rewrites (SP4); DSR/credibility graduation machinery (exists in `tpcore/backtest/credibility.py`; SP1 does not touch it); any `should_fire`/dispatch logic change; any DB/`application_log` change.

## 12. Self-review

Covered: the problem (≤10 shadows, two unlinked SoTs) §1; lane discipline + data-SDLC read-only §2; the exact extended frozen model + accessors §3; precisely which shadows derive vs documented-seam vs drift-test-only §4; the non-negotiable behavior-preservation invariants (ROSTER literal, allocator separate path, untouched gate logic, test-pinned allocator equivalence) §5; the N-way CI test with engine-domain legs modeled on the data 3-way §6; sigma→RETIRED with the "Live⇒wired applies only to PAPER/LIVE" correctness note §7; all D-SDLC1-* decisions §8; symmetry/divergence bounding SP1 §9; pure-code rollback §10; explicit out-of-scope (no SP2/3/4 bleed) §11. No placeholders; no contradiction; single-implementation-plan scoped; the one operator decision (epic decomposition) already resolved (4-chain approved). Ready for expert hardening then writing-plans.
