# Engine SDLC — SP2: The Lab — Design Spec

**Status:** approved design (operator chose D-SP2-4 **Option A / two-tier**, 2026-05-18). **Epic:** Engine SDLC (4-chain: SP1 roster SoT ✅ → **SP2 The Lab** → SP3 ECR+transitions → SP4 docs). **Lane:** ENGINE. Base `310ea6e` (post-SP1). FORMALIZE-AND-UNIFY (~80% compose-existing).

## 1. Problem & intent

Operator: *"I need to be able to backtest new engines and engine configurations WHILE the engines continue to run."* The Lab is an isolated shadow/candidate harness, run **concurrently with live dispatch**, **read-only**, **zero live side-effects**, scored against the SAME DSR/credibility gate, with two graduation exits: (1) promote-to-new-engine, (2) fold-into-existing-engine config. `LifecycleState.LAB` (shipped in SP1) is the SDLC state. SP2 ships the Lab RUN + the enforced isolation contract + scoring + the LAB registry seam + a two-exit graduation **recommendation dossier**. SP2 does NOT perform the LAB→PAPER transition (that is SP3).

## 2. Lane discipline (hard)

ENGINE lane only. DATA-SDLC files are READ-ONLY symmetry reference, NEVER edited: `tpcore/providers.py`, `tpcore/ladder/`, `ops/weekly_digest.py`, `ops/data_repair_service.py`, `tpcore/selfheal|feeds|ingestion|datasupervisor`, `scripts/run_data_operations.sh`, `tpcore/parity/` (the data EVALUATE analog — reference only). Never local-merge into shared main. Typed, pydantic v2, structlog; canonical entrypoints (no one-off scripts); no private-attr access on tpcore classes.

## 3. Scope — what SP2 ships

A new `tpcore/lab/` package composing the verified substrate behind ONE enforced isolation contract and ONE canonical entrypoint:

1. **`LabContext` — enforced isolation** (§4): a typed async context manager providing (a) a server-side **read-only** asyncpg pool (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` via the asyncpg `init=` callback) so any write to any table is rejected by Postgres regardless of code bugs; (b) one narrowly-scoped `max_size=1` read-write `credibility_pool` used SOLELY by `write_credibility_score`; (c) a process-level `contextvars` `_LAB_ACTIVE` reentrancy guard.
2. **`LabRun` / `LabResult`** (`tpcore/lab/run.py`): extract the walk-forward orchestration from `scripts/search_parameters.py`'s `amain()` (windows → per-window `load_<e>_window_context`+`run_<e>_with_context` → `rank_candidates` → final held-back `compute_dsr_for_verdict` + credibility rubric) into a reusable typed entrypoint; `scripts/search_parameters.py` becomes a thin delegating shim (its existing CLI + tests preserved). Behavior-preserving extract — the search suite is the oracle.
3. **Two-tier LAB registry** (D-SP2-4 Option A): exactly ONE durable LAB *sentinel* `_PROFILE` entry (`lifecycle_state=LAB`, reserved unique `dispatch_order=50`, `allocator_eligible=False`, not a runnable engine) + an ephemeral `tpcore/lab/registry.py` `LabCandidate(BaseModel, frozen=True)` overlay (`name`, `target_engine`, `param_overrides`, `intent: Literal["promote_new","fold_existing"]`, `notes`). `_PROFILE` stays the unpolluted dispatch SoT; experiments live in the lighter overlay.
4. **`python -m tpcore.lab` CLI** (`tpcore/lab/__main__.py`): on-demand, never in `dispatch_once`/`engine_dispatch`/any daemon. Canonical entrypoint (no one-off script).
5. **Two-exit graduation dossier** (`tpcore/lab/dossier.py`): render `LabResult` → a deterministic markdown Lab Dossier `docs/lab/<YYYY-MM-DD>-<candidate>-<verdict>.md` (structural twin of `tpcore/forensics/dossier.py`, idempotent) + the reused `data_quality_log` credibility row (unchanged from `search_parameters.py`). Recommends promote-new vs fold-existing with the exact winning-param diff. **Recommendation only — SP2 never applies it.**

**SP2↔SP3 boundary (precise):** SP2 stops at the dossier artifact + the LAB SoT seam + the frozen `LabResult` schema. SP3 owns the transition machinery (Engine Change Request checklist, the `LAB→PAPER` `_PROFILE` mutation, Exit-1 scaffold-from-template automation, Exit-2 constant-patch + re-gate, the consistency-oracle legs that fire on promotion). SP2 MUST NOT mutate `lifecycle_state` programmatically anywhere, scaffold a package, patch an engine constant, or re-gate.

## 4. The isolation contract — the make-or-break (D-SP2-1/3)

"Incidental" side-effect-freedom (the search path merely doesn't call live paths today) is NOT acceptable — a Lab bug writing `risk_state`/`open_orders`/`aar_events` or constructing an Alpaca client concurrently with live paper trading would corrupt the live risk ledger or double-submit. Three defense layers:

- **L1 server-side read-only pool (the unbypassable floor):** `tpcore/lab/pool.py` wraps `tpcore.db.build_asyncpg_pool` with an asyncpg `init=` callback running `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` on every connection. Any INSERT/UPDATE/DELETE through it raises `asyncpg.ReadOnlySQLTransactionError` at the server. Engine context loaders only SELECT, so they work unchanged. (Genuinely new — no `READ ONLY` precedent in the repo; verify with grep.)
- **L2 single allowlisted write:** a separate `credibility_pool` (`min_size=1,max_size=1`, normal RW) used ONLY by `write_credibility_score` (`tpcore/backtest/statistical_validation.py`) — never reaches engine code.
- **L3 fail-closed reentrancy guard:** `tpcore/lab/context.py` `_LAB_ACTIVE: contextvars.ContextVar[bool]` set True inside `LabContext.__aenter__`. SP2 adds an additive `assert_not_in_lab()` call to the `__init__` of each live side-effect class — `tpcore.risk.RiskGovernor`, `tpcore.aar.AARWriter`, `tpcore.order_management.BaseOrderManager`, the Alpaca broker constructor, and `DBLogHandler.startup` — raising `LabIsolationViolation` if `_LAB_ACTIVE` is set. Additive + inert outside a Lab run; per CLAUDE.md "never modify tpcore without checking all engines" — verified by the full suite staying green.

**Binding test** `tpcore/lab/tests/test_lab_isolation.py`: (a) a real `LabRun` (reversion, tiny universe, 1 window) yields ZERO row-delta on `risk_state`/`open_orders`/`aar_events`/`application_log WHERE event_type='STARTUP'` and exactly one new `data_quality_log` credibility row; (b) an INSERT through the read pool raises `ReadOnlySQLTransactionError`; (c) constructing `RiskGovernor`/`AARWriter`/`BaseOrderManager`/broker inside an active `LabContext` raises `LabIsolationViolation`. Symmetry ref (not clone): canary's paper-only-by-construction + the data-lane `tpcore/parity` isolation shape; mechanism diverges (server read-only + reentrancy guard).

## 5. LAB lifecycle integration (D-SP2-4 Option A, D-SP2-5)

Verified from post-SP1 code: `roster_for_dispatch()`→`_roster_sorted()` filters `lifecycle_state in _DISPATCHABLE={PAPER,LIVE}` → a LAB entry is already excluded from dispatch/allocator/package-names/the should_fire guard (no change needed; re-confirm file:line in the plan). `test_engine_lifecycle_consistency.py::test_live_engine_is_wired` skips non-{PAPER,LIVE} → LAB never trips package/tests/scheduler legs. BUT `test_no_half_state` appends `dispatch_order` for every non-RETIRED profile and asserts uniqueness → a LAB sentinel needs a globally-unique `dispatch_order` (reserved `50`). SP2 adds a consistency leg `test_lab_sentinel_is_not_wired`: the LAB sentinel has no top-level package, is absent from `roster_for_dispatch()`/`allocator_eligible_engines()`, and LAB is the only non-{PAPER,LIVE,RETIRED} state — closing the half-state gap symmetrically to the RETIRED leg (engine-lane SoT oracle, in scope). Ephemeral experiments are `LabCandidate` records in `tpcore/lab/registry.py`, NOT `_PROFILE` entries — `_PROFILE` remains the single dispatch authority (SP1's invariant honored).

## 6. Concurrency-with-live safety (D-SP2-6)

`python -m tpcore.lab --candidate <name>` — operator-on-demand, a separate OS process, ZERO call sites in `ops/engine_dispatch.py`/`engine_service`/any daemon/`dispatch_once`. `should_fire` already fail-closes LAB. Read pool `min_size=1,max_size=2` (tighter than the default 4) bounds Supabase-pooler contention (dashboard precedent: read-mostly concurrent with live dispatch). All Lab reads are append/slow-changing tables (prices_daily/fundamentals/liquidity_tiers) → MVCC consistent snapshot, zero locking vs concurrent live writers. The only write is the concurrent-safe `data_quality_log` append. No `mkdir`/any lock taken — cannot block or be blocked by the data-ops lock or the engine sweep. A Lab crash cannot affect live dispatch (separate process, no shared memory/lock/IPC).

## 7. The two exits — SP2 deliverable boundary (D-SP2-7/8/9)

`LabResult` (frozen pydantic-v2 — the SP2→SP3 contract, D-SP2-9): `candidate`, `target_engine`, `intent`, `verdict: Literal["SURVIVED","FAILED"]` (dsr≥0.95 ∧ cred≥60 ∧ n_trades≥3), `dsr: float`, `credibility_score: int`, `credibility_rubric: CredibilityScore`, `held_metrics`, `winning_params: dict`, `param_diff: list[ParamDelta]` (winning vs the engine's current default constants), `recommended_exit: Literal["promote_new","fold_existing","none"]`, `ranked_alternatives`, `walk_windows`, `n_trials`, `seed`, `generated_at` (UTC). Recommendation logic is a pure deterministic function of the numbers (FAILED→"none"; SURVIVED∧intent→that exit) — no LLM. Persistence (D-SP2-7): BOTH a new markdown dossier (`docs/lab/...`, forensics-pattern twin, idempotent) AND the reused `data_quality_log` credibility row (so `graduation_ready`/capital-gate can read it). SP2 stops here — no `_PROFILE` mutation, no scaffold, no constant patch, no re-gate (D-SP2-8).

## 8. Decisions D-SP2-1..9

| ID | Decision |
|----|----------|
| D-SP2-1 | Enforced (not incidental) isolation: server read-only pool + reentrancy guard + zero-live-write test. |
| D-SP2-2 | Compose, don't rewrite: extract walk-forward from `scripts/search_parameters.py` → `tpcore/lab/run.py`; script delegates (behavior-preserving; search suite is the oracle). Do NOT touch `BacktestHarness.run()` (still NotImplementedError — not the real engine). |
| D-SP2-3 | Read-only pool via `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (asyncpg `init=`); one `max_size=1` RW credibility pool. |
| D-SP2-4 | **Two-tier (operator-chosen):** one durable LAB sentinel in `_PROFILE` (reserved `dispatch_order=50`) + ephemeral `tpcore/lab/registry.py` LabCandidate overlay. |
| D-SP2-5 | Add `test_lab_sentinel_is_not_wired` consistency leg (closes the LAB half-state gap symmetric to RETIRED). |
| D-SP2-6 | `python -m tpcore.lab`, on-demand, never in dispatch/daemon. |
| D-SP2-7 | Dual persistence: new markdown Lab Dossier + reused data_quality_log credibility row. |
| D-SP2-8 | SP2 produces recommendation, never applies it (the SP2↔SP3 cut). |
| D-SP2-9 | `LabResult` frozen pydantic-v2 = the SP2→SP3 contract. |

## 9. Reuse-vs-new (compose vs minimal-new)

REUSE: walk-forward/sampling/ranking/DSR (`scripts/search_parameters.py`), per-engine context+run (`<engine>/backtest.py`), credibility rubric+persistence (`tpcore/backtest/credibility.py`, `statistical_validation.py`), LAB state+dispatch exclusion (SP1 `engine_profile.py`), pool builder (`tpcore/db.py`), dossier pattern (`tpcore/forensics/dossier.py`). NEW (minimal): `LabContext`/read-only pool/reentrancy guard, `LabRun`/`LabResult`/`LabCandidate`, `python -m tpcore.lab`, the LAB sentinel `_PROFILE` entry + consistency leg, `assert_not_in_lab()` guards. ~80/20.

## 10. Out of scope (SP2)

The LAB→PAPER transition machinery / Engine Change Request / scaffold-from-template / constant-patch / re-gate (all SP3); the SDLC docs + bash/pyproject shadow-closure (SP4); implementing `BacktestHarness.run()` (the search path is the real engine — untouched); any `should_fire`/dispatch logic change; any DB/application_log schema change; any LLM in the control path.

## 11. Self-review

Covered: problem/intent §1; lane discipline + data-SDLC read-only §2; the 5 SP2 deliverables + the precise SP2↔SP3 boundary §3; the enforced 3-layer isolation contract + binding test §4; LAB lifecycle integration with the verified-from-code dispatch exclusion + the half-state-gap fix §5; concurrency-with-live safety §6; the frozen LabResult + dual persistence + recommendation-not-application boundary §7; all D-SP2-1..9 incl. the operator-chosen two-tier §8; reuse/new ledger §9; explicit out-of-scope (no SP3/SP4 bleed) §10. No placeholders; the one operator decision (D-SP2-4) resolved (two-tier). Ready for expert hardening then writing-plans.
