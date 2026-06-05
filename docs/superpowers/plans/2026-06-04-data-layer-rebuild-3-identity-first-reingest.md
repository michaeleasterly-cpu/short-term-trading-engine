# Data-Layer Rebuild — Plan 3: Identity-First Re-Ingest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> ⚠️ **Operates on a LIVE, EMPTY production DB** (Plan 2 wiped the ticker graph). This phase RE-POPULATES it from source. It is mostly additive (INSERTs into empty tables), but it is long-running and order-critical: **identity substrate MUST be built correct-first** so the 14 `BEFORE INSERT` triggers attribute `classification_id` at load time. Railway writers stay paused until the final restore step.

**Goal:** Re-populate the clean schema from authoritative source in identity-first order (universe → issuers → identity → prices → fundamentals → signals → derived), land the two deferred folds (`ticker_lifecycle_events`→`corporate_events`; `adjusted_close` factor model → drop `split_pre_image_log`), prove the re-attribution is clean (the six audit metrics all zero), VALIDATE the FKs, get the 32-check validation suite to 100% green (`DATA_OPERATIONS_COMPLETE`), and un-pause the writers.

**Architecture:** Re-use the existing `scripts/ops.py` ingest stages where they exist; the spec's identity-first ORDER (§5.3) is the new discipline. The identity substrate build (universe/issuers/classifications/history) is the part where the explore found gaps — **Phase 0 is a discovery to pin exactly what code exists vs. must be written** before any build task. Bulk loads follow the Supabase mechanics (CSV-first, chunked >100K, drop/recreate indexes, ANALYZE; spec §7).

**Tech Stack:** Python 3.11, `scripts/ops.py` stages, asyncpg, FMP (`/stable/historical-price-eod/full`, profiles, fundamentals), SEC EDGAR (`submissions.zip`, companyfacts XBRL, Form 4/8-K/25/15), FINRA/Finnhub/ApeWisdom/IBorrowDesk, `tpcore/identity/tkr14.py::mint`, the 32-check validation suite, Railway GraphQL.

**Source spec:** `docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md` v1.4 §5 (ingest contract), §5.3 (identity-first order), §5.5 (population fixes), §5.6 (adjusted_close), §6 (validation), §7 (Supabase mechanics), §8.2 (phases 4-13), §8.1 (restore). **Predecessors:** Plan 1 (predicate+FK) ✅, Plan 2 (clean-schema cutover) ✅ — DB at alembic head `20260604_0600`, ticker graph empty, macro/SACRED intact.

**Heavy-lane:** `scripts/ops.py` + `tpcore/ingestion/**` + `tpcore/quality/validation/**` + identity-path → full §1 pipeline; whole-suite + order-flip authoritative; the `discovery-first` SWV+CIC gates apply to every ingest/identity change.

---

## EXISTS / NEW legend (from the 2026-06-05 explore — Phase 0 confirms)

| Component | State | Where |
|---|---|---|
| `daily_bars` (prices), `fundamentals_refresh`, `sec_fundamentals_fallback`, `earnings_refresh`, `sec_filings`, `finra_short_interest`, `tier_refresh`, `data_validation` | **EXISTS** — re-run | `scripts/ops.py` stage registry |
| `classify_tickers`, `tkr14_backfill`, `symbol_history_evidence_backfill`, `ticker_history_backfill`, `backfill_sec_lifecycle` | **EXISTS but were the polluted path** — must run identity-FIRST + with the §5.5 fixes | `scripts/ops.py:~6298/11491/11509/11596/5477` |
| Coordinated **identity-first orchestrator** (enforce universe→issuers→identity BEFORE children) | **LIKELY NEW** (no single entrypoint today) | Phase 0 confirms |
| **issuers / issuer_history** bulk-loader from SEC `submissions.zip` | **LIKELY NEW/PARTIAL** | Phase 0 confirms |
| FPFD = earliest `filingDate` (not `min(period_end_date)`) population | **FIX** (spec §5.5/A5) | `tpcore/sec/companyfacts_adapter.py` |
| `adjusted_close` cumulative-factor model + drop `split_pre_image_log` | **NEW** module; `corporate_actions` reader exists | `tpcore/data/apply_splits.py` → new `adjusted_close_compute.py` |
| `ticker_lifecycle_events`→`corporate_events` fold | **NEW** (re-point writer) | `scripts/ops.py:~5810` |
| Re-attribution verify (6 metrics) | **EXISTS as audit scripts** | `docs/audits/data/2026-06-03/step*.py` |

---

### Phase 0 — Discovery: pin the identity-build code state (DISCOVERY-FIRST gate)

**Files:** none (read-only). Produces the input that turns Phase 1's tasks from "likely new" into exact tasks.

- [ ] **Run `/system-wide-verification` + `/change-impact-classification`** for "build the identity substrate (universe/issuers/ticker_classifications/ticker_history/issuer_securities) from source, identity-first." Required by the `discovery-first` rule before any ingestion/identity change.
- [ ] **Trace, with `file:line`, exactly what each identity table's population path is today** and whether it builds from source correctly or needs writing/fixing:
  - universe construction (SEC full company list ∪ FMP symbols + delisting/symbol-change) — is there a stage, or is it new?
  - `issuers` + `issuer_history` from SEC `submissions.zip` (CIK, legal_name, FYE, doc-type, FPFD) — exists, partial, or new?
  - `ticker_classifications` TKR-14 mint + `lifetime_start` from FPFD; `ticker_history` SCD-2 multi-row (delisted-then-reused); `issuer_securities` M:N — what's wired vs new?
- [ ] **Decide the orchestration:** a new `scripts/ops.py` identity-build stage(s) that runs phases 1–4 in strict order, OR a documented run-order using existing stages. Output: the exact stage list + order for Phase 1, with each marked RUN-EXISTING / FIX-EXISTING / WRITE-NEW.
- [ ] **Output a short discovery note** (`docs/audits/2026-06-05-identity-build-code-state.md`) so Phase 1 tasks are concrete. **Do not write build code until this note exists.**

---

### Phase 1 — Identity substrate build (correct-first; nothing else loads until this is clean)

> Tasks here are finalized from Phase 0's note. The invariants are fixed regardless: TKR-14 PK; `lifetime_start` from SEC FPFD (earliest `filingDate`), never the dropped sentinel; `ticker_history` SCD-2 with the half-open predicate (already shipped Plan 1); `issuer_securities` M:N; SEC-first authority, FMP fallback (spec §5.2/A7/A8). The triggers attribute `classification_id` automatically once the substrate is correct.

- [ ] **Task 1.1 — Universe build** (RUN/FIX/NEW per Phase 0). Bulk-load the survivorship-free universe (SEC CIK list ∪ FMP symbol+delisting history). Assert: every active+delisted ticker present; delisted-then-reused cohort has multiple `ticker_history` rows.
- [ ] **Task 1.2 — Issuers + issuer_history** (SEC-first). Populate `issuers` (`issuer_id` surrogate PK, `cik` nullable-unique, legal_name, FYE, `sec_document_type_primary`, FPFD) + `issuer_history` SCD-2. Assert: 0 issuers with NULL legal_name; CIK unique.
- [ ] **Task 1.3 — Classifications + history + securities.** Mint `ticker_classifications` (TKR-14) with `lifetime_start` = SEC FPFD; build `ticker_history` (SCD-2, multi-row reuse) + `issuer_securities` (M:N, share-class fan-out). Assert: 0 rows with `lifetime_start = '1900-01-01'`; 0 NULL `lifetime_start`; the half-open `ticker_history` covers every (ticker, active-range) with no overlap (the EXCLUDE constraint enforces this).
- [ ] **Task 1.4 — Identity gate (BLOCKING).** Re-run the boundary-oracle sentinel + assert the identity substrate is internally consistent before loading any child table. If not clean, STOP — child loads would inherit the defect.

---

### Phase 2 — Substrate: prices + adjusted_close + fundamentals

- [ ] **Task 2.1 — `daily_bars` (prices_daily).** `python scripts/ops.py --stage daily_bars --param force_refresh=true --param universe=active` (FMP full CTA tape, CSV-first → `prices_daily_staging` → promote; chunked to stay under the 3600s timeout). Triggers stamp `classification_id` at insert. Apply the bulk-load mechanics (§7): drop non-essential indexes before COPY, recreate + ANALYZE after.
- [ ] **Task 2.2 — `adjusted_close` cumulative-factor model (NEW).** Write `tpcore/data/adjusted_close_compute.py`: `adjusted_close = close × Π(factor for every corporate_actions row with action_date > date)` (split factor; `(1 − div/ex_date_raw_close)` for dividends; reverse-split = factor<1). Idempotent (function of immutable raw `close` + append-only `corporate_actions`). TDD: factor-math unit tests first. Wire it post-`daily_bars`. **Then DROP `split_pre_image_log`** (a new migration `20260605_xxxx`) — the in-place-mutation pre-image log is obsolete once `close` stays raw (spec §2.3/§5.6/OQ-3).
- [ ] **Task 2.3 — Fundamentals.** `fundamentals_refresh` (FMP) then `sec_fundamentals_fallback --param dry_run=false` (SEC XBRL gaps). 3-part-PK restatement-preserving dedup (already enforced by the schema). Assert: 0 NULL `classification_id`; the 267-amended-filing pattern preserved.

---

### Phase 3 — Signals + the lifecycle fold

- [ ] **Task 3.1 — `corporate_actions`** (splits/dividends — feeds 2.2). Ingest before/with prices so `adjusted_close` has its inputs.
- [ ] **Task 3.2 — Lifecycle fold (NEW).** Re-point `scripts/ops.py:~5810` (`_stage_backfill_sec_lifecycle`) from `INSERT INTO ticker_lifecycle_events` to `corporate_events` (Form 25 → `event_kind='delisting'`, Form 15 → bankruptcy kind), minting the bitemporal `event_id` SHA + predecessor/successor resolution. TDD: the mapping first. **Then DROP `ticker_lifecycle_events`** (migration) once the fold is verified.
- [ ] **Task 3.3 — Signal stages.** `earnings_refresh`, `sec_filings` (insider_transactions + sec_material_events; 6h timeout), `finra_short_interest` (+ borrow + insider/social sentiment). For `spread_observations`: **disable `spread_observations_retention_trg` during the bulk backfill, recreate after** (spec §8.2 — else the load self-prunes its own tail).

---

### Phase 4 — Derived

- [ ] **Task 4.1 — `tier_refresh`/`assign_tiers`** (liquidity_tiers from spread_observations) + **`universe_prescreener`** (universe_candidates). Run after signals; they depend on prices + spreads.

---

### Phase 5 — Re-attribution verify + FK VALIDATE (BLOCKING gates)

- [ ] **Task 5.1 — Re-attribution verify (all SIX metrics must be 0).** Run the audit queries (`docs/audits/data/2026-06-03/step4_prices_daily.py` + siblings): (1) 0 NULL `classification_id` across child tables; (2) 0 orphan classification_ids; (3) 0 pre-FPFD bars; (4) 0 post-`lifetime_end` bars; (5) 0 bars outside the `ticker_history` half-open window; (6) 0 bars whose `ticker_history`-implied cls ≠ stamped cls. **Any non-zero → STOP**, fix the upstream phase, re-run. This is the proof the rebuild cured the rot.
- [ ] **Task 5.2 — FK VALIDATE.** `SET LOCAL statement_timeout='15min'; ALTER TABLE … VALIDATE CONSTRAINT` for every `classification_id` FK (`prices_daily` + the child tables + `aar_events`). On a correctly-attributed load these validate clean.

---

### Phase 6 — Acceptance: 32-check green → DATA_OPERATIONS_COMPLETE

- [ ] **Task 6.1 — Run the validation suite** (`python scripts/ops.py --stage data_validation` or `run_data_operations.sh`). Drive every one of the 32 checks to 100% green — especially `prices_daily_completeness` (zero-tolerance, identity-windowed), `prices_daily_freshness` (CRITICAL_TICKERS), `fundamentals_quarterly_completeness` (per-form MAX_GAP, FPFD-clamped). Iterate on real gaps (NOT by weakening checks).
- [ ] **Task 6.2 — First `DATA_OPERATIONS_COMPLETE`.** Once 100% green, the emission gate fires (the "100% data or don't trade" invariant). Confirm the event in `application_log`.

---

### Phase 7 — Restore writers (un-pause)

- [ ] **Task 7.1 — Railway:** `serviceInstanceUpdate(input:{sleepApplication:false})` on engine-service/lane-service/trade-monitor; restore data-operations cron `30 21 * * MON-FRI` (per `reference_railway_access` memory; `numReplicas` is NOT the lever — `sleepApplication` is).
- [ ] **Task 7.2 — Local launchd:** `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.michael.trading.{allocator,pipeline-smoke-test}.plist` (the two I unloaded). Verify they load.
- [ ] **Task 7.3 — Push the new code** (the rewired engines run new code only after the Plan-2 push redeployed Railway — verify the deploy took). Confirm a clean cycle: data-ops runs → 100% green → `DATA_OPERATIONS_COMPLETE` → engines sweep.
- [ ] **Task 7.4 — Bring `docs/DATABASE_AND_DATAFLOW.md` §2/§3 current** (spec §9; Plan 4 may own this) + mark the arc complete in TODO/memory.

---

## Self-Review

**Spec coverage (Plan 3 = §5 + §8.2 phases 4-13 + §8.1 restore):** identity-first order (Phase 0-1), prices+adjusted_close (2), fundamentals (2.3), signals + lifecycle fold (3), derived (4), re-attribution verify (5.1), FK VALIDATE (5.2), 32-check green + DOC (6), restore (7). ✓ Deferred-from-Plan-2 items landed: `ticker_lifecycle_events` fold (3.2) + `split_pre_image_log` drop (2.2).

**Honest scope flags:** Phase 0 is a real discovery gate — the explore could not fully pin the identity-build code (no single orchestrator; issuers loader may be new), so Phase 1's tasks are finalized from Phase 0's note rather than written speculatively here. The NEW code (adjusted_close, lifecycle fold, any identity orchestrator/issuers loader) gets TDD; the EXISTING stages get gated invocation + assertions. This is the heavy-lane "discover before building" discipline, not a placeholder.

**Sequence integrity:** identity substrate (1) is a BLOCKING gate (1.4) before any child load (2-4); re-attribution verify (5.1) is a BLOCKING gate before FK VALIDATE (5.2) + acceptance (6); writers stay paused until (7). Each gate names its STOP condition.

---

## Note

Plan 3 is large and front-loaded with discovery because the rebuild's whole point was that the identity substrate was wrong. Recommend executing it phase-by-phase with an operator checkpoint after Phase 1 (identity built + the identity gate green) and again before Phase 7 (un-pause) — the same checkpoint discipline used for Plans 1-2. Plan 4 (validation hardening + doc refresh) folds into Phase 6-7 here, or stays a thin follow-up.
