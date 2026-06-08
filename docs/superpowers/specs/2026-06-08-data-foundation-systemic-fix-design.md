# Data-Foundation Systemic Fix — Design Spec

> **Status:** DRAFT for operator spec-read (2026-06-08). Heavy-lane (`platform/migrations/**`, `tpcore/quality/validation/**`, `scripts/ops.py`, identity-path). No implementation until this spec is approved + a plan is written.

**Goal (one sentence):** Convert the data layer from "enforce identity + scope by convention and after-the-fact cleanup" to "enforce by structure and prevention at the write boundary," so no writer can introduce an identity-blind or mis-scoped row, and add a real "is the database correctly wired" validation.

**Operator directive (2026-06-08):** "system fixes, not patch fixes… get the data fixed first," then (later, separate efforts) a denormalized+normalized macro feature layer, then machine learning. This spec covers ONLY the foundation fix. Macro feature layer + ML are explicitly out of scope (see §9).

---

## 1. Problem — one root cause wearing many masks

A multi-agent trace of the writer surface, the identity/entity model, and the validation layer (2026-06-08) converged on a single pattern: **the data layer relies on convention + post-hoc cleanup instead of structural prevention.** Every recurring symptom this cycle is the same disease:

| Symptom (live) | Mechanism |
|---|---|
| 30,411 rows with `classification_id IS NULL` across prices_daily (24,575), sec_periodic_filings (4,212), corporate_actions (738), fundamentals_quarterly (558), earnings_events (328) | Identity triggers **NULL-soften** on no-match; 7 tables allow NULL to persist |
| FB→Meta / SBNY / FISV reused-symbol bars under the delisted symbol | FMP fetch is **not clamped** to the entity's active window / delisting date |
| 14,775 etf/etn/fund rows in a "stock/reit-only" fundamentals table | Entity-type scope is a copy-pasted `WHERE asset_class='stock'` **convention**, omitted on the fundamentals + prices writers |
| Fundamentals NULLs persisted even after re-anchoring (only 2/558 resolved) | `ticker_history` (the SCD-2 spine) is **incomplete** (218 entities with no window, 450 gaps, 4,933 guessed Jan-1 starts) and has **no foreign key** |
| The hung SEC-metadata backfill | Some bulk stages accumulate-then-trailing-`executemany` with **no incremental commit / resume** |

### The three structural root causes

- **RC-1 — Triggers NULL-soften, never reject.** All 16 `BEFORE INSERT` classification_id triggers end "if no `ticker_history` window matches → leave NULL, insert anyway." NULL is a *valid* write-time outcome. 7 tables (incl. prices_daily, fundamentals_quarterly) are NULLable so the bad row persists; 8 are NOT NULL only by historical accident, not design. The identity-path rule says a NULL `classification_id` is a critical defect — nothing structurally enforces that.
- **RC-2 — `ticker_history` is incomplete and unconstrained.** The spine every write resolves against has no FK to `ticker_classifications`, 218 entities with zero windows, 450 inter-window gaps, and 4,933 synthetic Jan-1 `valid_from`s. This is the *cause* behind most NULLs — re-anchoring a trigger can't help when the window doesn't exist.
- **RC-3 — Entity-type scope and FMP fetch-window are conventions, not constraints.** No CHECK/trigger keeps etf/fund out of fundamentals or enforces satellite membership; no fetch path clamps FMP requests to `[lifetime_start, delisting_date]`. The clamp data exists (`ticker_history`, `lifetime_end`, `KNOWN_DELISTINGS`) but is never threaded into the request bound.

### Why the validation layer didn't catch it

The 32-check data-acceptance suite is **detect + cleanup, not prevent**, and has holes: `ticker_history` has no FK, there is **no referential-integrity/relationship test**, 3 child tables (fundamentals_quarterly, corporate_actions, earnings_events) have **no null-completeness check**, and several integrity checks **pass vacuously on an empty table** (no row-count floor). So a malformed substrate can read green.

---

## 2. Goal & non-goals

**Goal:** A data substrate where (a) every ticker-bearing row provably carries a correct `classification_id`, (b) every row falls within its entity's active `ticker_history` window, (c) entity-type scope is structurally enforced, (d) FMP cannot contaminate with reused-symbol bars, and (e) a single validation answers "is the database correctly wired" — referential integrity, relationship completeness, and non-vacuous coverage.

**Non-goals (this spec):** the macro feature layer, predictive analytics, machine learning, any engine/signal change, any new data source.

---

## 3. Target end-state (the structural contract)

1. **One unified resolver.** A single `resolve_classification_id(ticker, as_of) → classification_id` SQL function. All identity triggers call it (replacing 16 copy-pasted bodies). One place defines the half-open predicate, the anchor rule, and the no-match behavior.
2. **Reject, don't NULL.** On no-match the resolver raises (or routes to an explicit, monitored quarantine — design decision §7-A). After cleanup, `classification_id` is `NOT NULL` + validated FK on **all** ticker-bearing tables.
3. **Complete, constrained spine.** `ticker_history` gets the missing FK to `ticker_classifications`; every active classification has ≥1 window; windows are gap-free over each entity's active range (synthetic Jan-1 starts replaced with evidence-based first dates); a completeness gate enforces this going forward.
4. **Scope as constraint.** A scope guard (trigger/CHECK) rejects a fundamentals row for an etf/etn/fund entity, and enforces satellite membership (`etf_attributes` etf-only, issuer graph stock/reit-only).
5. **FMP window-clamp at the transport.** The single FMP fetch funnel clamps requests to the entity's `[lifetime_start, delisting_date]`; the universe selectors share one asset_class-scoped helper.
6. **SEC-authoritative `asset_class`.** Where SEC `sec_document_type_primary` exists, it is authoritative for the operating/non-operating distinction the scope guard keys on (today `asset_class` is ~85% Alpaca-name-derived, unbacked).
7. **Real "DB is wired" validation.** A referential-integrity + relationship-completeness + non-vacuous-coverage check set: every FK present + valid, no orphan classification_ids, `ticker_history` complete, no degenerate-green (row-count floors on integrity checks), null-completeness on every ticker-bearing table.
8. **Resumable bulk stages.** Stages that write the substrate use the per-ticker `application_log` resume idiom (no all-or-nothing trailing commit).

---

## 4. Phasing (precondition → cleanup → enforcement)

The ordering is forced: you cannot reject-not-NULL until the spine can resolve legit writes, and you cannot add NOT NULL / scope constraints until the existing violators are cleaned.

- **Phase A — Spine completeness (precondition).** Fill `ticker_history` windows for the 218 windowless entities + the gaps; replace synthetic Jan-1 starts with evidence-based first-trade/rename dates (SEC-first, price-bar + FMP evidence). Add the FK `ticker_history.classification_id → ticker_classifications.id` (and on `ticker_lifecycle_events`). **Design decision §7-B: completion strategy.**
- **Phase B — Cleanup the existing violators.** Re-resolve the 30,411 NULL-cls rows against the now-complete spine (those that still can't resolve are genuine cross-entity contamination → remove with evidence, e.g. the FB→Meta bars). Clean/reclassify the 14,775 etf/fund fundamentals rows (the 532 contradiction entities get SEC-checked: real operating companies reclassified, true funds' fundamentals removed-with-evidence). Fix the `state_by_ticker` dup-ticker bug.
- **Phase C — Structural enforcement.** Unified resolver + reject-not-NULL + flip the 7 nullable columns to NOT NULL. Scope guards. FMP window-clamp. SEC-authoritative asset_class. Each is a migration; each is no-op-safe only because Phase B cleared the violators first.
- **Phase D — Validation hardening.** The "DB is wired" check set (§3.7) + degenerate-green floors + the missing null-completeness checks + the FMP-reuse / out-of-window detectors. This is also the deliverable answering the operator's "test that the DB is configured properly."
- **Phase E — Resumable stages.** Convert the trailing-commit bulk stages to the per-ticker resume idiom.

Each phase is its own gated PR (heavy-lane split-review). Phases A→C must land in order; D can develop in parallel and lands last as the standing guard.

---

## 5. The validation deliverable (operator's "is the DB right" test)

A single invocable check (and CI/ops gate) that asserts, with no vacuous passes:
- every declared FK exists and is `VALIDATED` (incl. the new `ticker_history` FK);
- zero `classification_id IS NULL` on every ticker-bearing table (null-completeness for all, not just prices);
- `ticker_history` completeness (every active classification has gap-free windows over its active range);
- zero rows outside their entity's active window; zero reused-ticker cross-entity rows;
- entity-type scope holds (no fundamentals for etf/fund; satellite membership);
- row-count floors so an empty/truncated table fails instead of passing vacuously.

This becomes part of the `DATA_OPERATIONS_COMPLETE` predicate.

---

## 6. Risk & rollback

- This touches the live trading data substrate. Every migration is **cleanup-first, then constrain**, so each constraint addition is no-op-safe at apply time.
- Reject-not-NULL is the highest-risk flip (a too-aggressive resolver could block legitimate ingest). Mitigation: land the unified resolver in **log-only mode first** (resolve + log would-reject), confirm zero false-rejects against a full ingest cycle, then flip to hard-reject.
- All changes go through the heavy-lane pipeline (spec → plan → subagent-driven → split-review + silent-failure-hunter → whole-suite + order-flip → gated PR). The migrations rule's schema-rationale gate applies to each new constraint/table.

---

## 7. Design decisions to resolve (operator / expert)

- **§7-A — No-match behavior: hard-reject vs quarantine.** Raise on the insert (loud, blocks the batch) vs route the row to a monitored `*_identity_quarantine` table for review. Recommendation: **hard-reject in log-only → then hard mode** (§6), with quarantine only if a real recurring legitimate-but-unresolvable class appears. *Route the modeling choice to a db-architect expert.*
- **§7-B — `ticker_history` completion strategy.** How to fill 218 windowless entities + 450 gaps + 4,933 synthetic starts: SEC submissions first-filing dates, first price bar, FMP listing dates — in what authority order, and what to do when sources disagree. *This is the hardest sub-problem; route to an expert before Phase A.*
- **§7-C — etf/fund fundamentals disposition.** For the 532 contradiction entities: reclassify (SEC says operating) vs remove-with-evidence (true fund). Confirm the asset_class SEC-authority rule (§3.6) lands before or with this cleanup.

---

## 8. Success criteria

- The §5 validation passes with zero vacuous checks.
- `classification_id IS NULL` count = 0 across all ticker-bearing tables, enforced by NOT NULL.
- `ticker_history` has its FK + completeness gate; 0 windowless active entities.
- 0 etf/fund rows in fundamentals; satellite membership constraint-enforced.
- 0 FMP reused-symbol / out-of-window bars; fetch clamped at the transport.
- The substrate is trustworthy enough to build the macro feature layer + (later) ML on top without learning defects.

---

## 9. Explicitly out of scope (later, separate specs)

- **Macro feature layer** — pivot the tall macro table to a wide, one-row-per-day / one-column-per-macro derived feature table + statistical normalization (z-score). Derived view on top of the tall substrate (the tall table stays the SoT); no substrate denormalization; no data redo. Its own spec once the foundation lands.
- **Predictive analytics** on the normalized feature matrix (classical first).
- **Machine learning** — only after the foundation + feature layer exist and the signal warrants it. ML on an unvalidated foundation would learn the defects.
