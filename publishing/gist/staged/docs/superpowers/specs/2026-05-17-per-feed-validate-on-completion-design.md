# Per-Feed Validate-on-Completion + Self-Heal-on-Fail — Design

**Status:** **BUILT 2026-05-17** (DATA lane). Brainstorm → spec → plan
→ **phased build complete** (Phases 1–4, PRs #21/#22/#23 + this doc
update). Generalises the producer self-validation (#1) from coarse
bespoke guards into one canonical mechanism.

**Build record:**
- Phase 1 (PR #21): `tpcore/selfheal/per_feed.py` — `validate_one` /
  `validate_feed` / `heal_one` / `validate_and_heal_feed`, drift-guarded
  check registry. Landed dark.
- Phase 2 (PR #22): `on_stage_complete` + the fail-safe
  `_per_feed_tripwire` wired into `cmd_update` for **leaf** feeds.
- Phase 3 (PR #23): `upstream_feeds` + `cycle_green` in-cycle state →
  derived feeds (fear_greed) validate on their own stage once every
  upstream went green this cycle.
- Phase 4 (this doc + guard docstrings): coarse guards **KEPT** as
  cheap non-authoritative pre-filters — see §6 row 4 / §8 resolution.

Operator directives captured verbatim: *"validation should run per feed
or data point and a final just checks referential integrity between
them"*; *"if a per-feed validation fails, it self heals"*.

## 1. Problem

Two gaps:

1. **Late detection.** The validation suite is per-feed by
   construction (one `check_<feed>` per feed) but is *executed*
   monolithically at the end of the daily cycle. A feed that ingests
   broken at stage 3 of 14 is not detected until the end-of-cycle
   `data_validation` stage — then self-heal fires a whole cycle later.
2. **Drift.** #1's producer guards are *bespoke per stage* — the
   `daily_bars` coverage-collapse raise (its own threshold) and the
   `shrinkage` 20% hard-stop. They are not the canonical check, so a
   guard can silently diverge from the suite's definition of "good"
   (the exact fake-green class this session fought).

## 2. Design — the check IS the validator, run on completion

After an ingest stage **S** that produces feed **F** completes:

1. **Resolve F's canonical check(s)** via the registry SoT
   (stage→feed→check: `HealSpec.source` ↔ `tpcore.feeds.FEED_PROFILES`
   ↔ `tpcore/quality/validation/checks/<feed>.py`). No new mapping —
   reuse what exists.
2. **Run only F's check(s).** Green → proceed; the end-of-cycle suite
   re-running it is then a cheap confirmation.
3. **On red → self-heal F immediately**, bounded, *targeted to F's
   HealSpec only* (not the whole-layer `run_self_heal`). Re-validate
   F. Still red after the HealSpec's `max_attempts` → **honest
   escalation** (the existing escalation semantics + alarm). No
   flapping: bounded per feed per cycle, then escalate.

This makes #1's coarse guards a *special case* the canonical-check
path subsumes — the check is the single source of "is F good", so the
producer guard and the suite cannot drift.

## 3. Leaf vs derived feeds (reuses the #2 dependency graph)

- **Leaf feed** (`HealSpec.depends_on == ()`): validate on its own
  stage completion.
- **Derived feed** (`depends_on` non-empty — e.g. `fear_greed →
  [macro_indicators, prices_daily]`, `liquidity_tiers → [...]`):
  validating on a single upstream's completion would false-fail.
  Validate a derived feed only **after all its `depends_on` feeds have
  completed and are green** (topological, driven by the existing
  `HealSpec.depends_on` SoT from the fake-healable audit). If an
  upstream is red, the derived feed's per-feed validation is
  **deferred to its own producing stage / the final pass**, not run
  early.

## 4. The final monolithic pass STAYS (not replaced)

Per the operator directive ("…and a final just checks referential
integrity between them"): the end-of-cycle `data_validation` +
cross-table referential audit remain the **whole-green +
cross-feed-referential gate** that authorises
`DATA_OPERATIONS_COMPLETE`. Per-feed-on-completion is an **early
tripwire** (fail fast, heal a cycle earlier), explicitly *not* a
replacement. Re-running an already-green check at the end is cheap and
confirms nothing regressed cross-feed.

## 5. Honest constraints / design decisions to lock in the plan

- **No source-subset API on `run_self_heal`** (verified — it is
  whole-layer). Per-feed heal therefore runs the feed's HealSpec
  `stage`+`params` directly with bounded retry + re-validate
  (mirroring exactly what the orchestrator does for one check),
  *not* a new orchestrator parameter. Decision: a small
  `heal_one(pool, check)` helper that reuses the HealSpec + the
  canonical runner; orchestrator untouched.
- **Pooler-contention lock.** Per-feed heal must acquire/respect the
  same `${TMPDIR:-/tmp}/ste-data-operations.lock` Step-4 self-heal and
  the cutover/data-repair agents use — never two concurrent
  `daily_bars` repairs ("connection was closed"). Defer (don't loop)
  on contention.
- **Bounded + idempotent.** HealSpec `max_attempts` per feed per
  cycle, then escalate. A feed already green on completion is a no-op.
- **`healable=False` feeds** (corruption / source-of-truth class):
  per-feed validate still fires (fail fast) but does NOT self-heal —
  it escalates immediately (honest, matches the registry).
- **Derived/upstream ordering** must consult the live `depends_on`
  graph, not an import snapshot (the cutover-agent lesson).

## 6. Phasing (each independently testable; final gate unchanged)

| Phase | Deliverable |
|---|---|
| 1 | ✅ `heal_one` + `validate_one`/`validate_feed`/`validate_and_heal_feed` helpers — reuse the canonical checks + HealSpec + runner; unit-tested; landed dark. (PR #21) |
| 2 | ✅ `on_stage_complete` + fail-safe `_per_feed_tripwire` wired into `cmd_update` for **leaf** feeds. (PR #22) |
| 3 | ✅ Derived-feed ordering via the live `depends_on` graph + `cycle_green` in-cycle state (defer derived validation until upstreams green). (PR #23) |
| 4 | ✅ **DECIDED: keep, non-authoritative.** The `coverage` raise (`ops.py _stage_daily_bars`) and `shrinkage` detector (`tpcore/ingestion/csv_archive.py`) are retained as cheap fail-fast PRE-FILTERS. The canonical per-feed `check_<feed>` (now run on-completion via Phases 2/3 + the end-of-cycle monolithic gate) is authoritative. The coverage guard already imports the canonical `COVERAGE_COLLAPSE_PCT` (cannot diverge); both guard docstrings now state "do not accrete bespoke validity logic — extend the canonical check instead". Documented, not duplicated logic — no code retired. |

## 7. Non-goals

- Not removing the final monolithic referential/whole-green gate.
- Not an LLM (deterministic, like every data agent).
- Not auto-healing `healable=False` classes (escalate, honest).
- Operator interaction unchanged: this is internal data-layer
  hardening; the operator's only touchpoints remain the
  ADD/REMOVE Data Feed Change Request + the weekly digest ack.

## 8. Open questions — RESOLVED in the build

- **Stage-runner hook point → RESOLVED:** the single chokepoint inside
  `cmd_update`'s `_STAGE_SPECS` loop (after each `_run_stage` returns
  OK), via the fail-safe `_per_feed_tripwire` → `on_stage_complete`.
  Chosen over a `run_data_operations.sh` step: one in-process call site
  covers every cycle path with minimal blast radius, zero bash edits,
  and runs inside the existing data-operations lock (spec §5). The
  standalone single-stage path does NOT trigger it (lock-safety). The
  hook is fail-safe by construction — it never raises into / aborts the
  cycle; the end-of-cycle `data_validation` + Step-4 whole-layer
  self-heal remain the authoritative 100%-green gate (§4).
- **Phase 4 coarse guards → RESOLVED: keep, non-authoritative** (see
  §6 row 4). No code retired; defense-in-depth, documented.
