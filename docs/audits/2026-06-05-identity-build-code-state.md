# Identity-build code-state discovery — 2026-06-05 (Plan 3 Phase 0)

Read-only discovery note. **No DB mutation, no stage execution, no migration, no push.** This note finalizes the Plan-3 Phase-1 build tasks by pinning, with `file:line`, exactly what code each identity table's population path is today and whether it can build a CORRECT identity substrate from source on the now-empty clean schema.

- **Plan:** `docs/superpowers/plans/2026-06-04-data-layer-rebuild-3-identity-first-reingest.md` (Phase 0 + 1).
- **Spec:** `docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md` v1.4 §4 (identity model), §5.2 (per-source roster), §5.3 (identity-first order), §5.5 (population fixes).
- **Root audit:** `docs/audits/2026-06-03-identity-substrate-data-flow.md` (the rot the rebuild cures).
- **DB state at discovery:** clean schema at alembic head `20260604_0600`; ticker graph EMPTY (Plan 2 wiped it); `macro_data` / SACRED intact.

## THE QUESTION, answered

> With the DB empty, can the EXISTING `scripts/ops.py` stages — run identity-first — build a CORRECT identity substrate from source?

**No.** The existing stages are **enrichers/repairers of an already-populated, Alpaca-seeded, prices-derived substrate** — not source-of-truth minters of a survivorship-free, SEC-first, FPFD-anchored identity layer. Three structural blockers:

1. **No universe/issuers source-minter exists.** The only writer that *mints* `ticker_classifications` rows is `tpcore/data/classify_tickers.py::classify_all_tickers` (`scripts/ops.py:6351`→`tpcore/data/classify_tickers.py:322`), and its source is **Alpaca `/v2/assets` `status=active`** (`tpcore/data/classify_tickers.py:191`) — active-only (survivorship-VIOLATING) and NOT SEC∪FMP. The `_UPSERT_SQL` (`tpcore/data/classify_tickers.py:202-213`) writes `ticker, asset_class, etf_*, source` only — never `id` (TKR-14), `cik`, `lifetime_start`, or `first_public_filing_date`. There is **no whole-universe `issuers` bulk loader** from `submissions.zip`; the 4 `INSERT INTO platform.issuers` sites (`scripts/ops.py:4890, 7982, 7998, 8346`) all enrich CIKs that already exist in `ticker_classifications`.

2. **The current `--update` registry order is CHILD-FIRST — the inverse of §5.3.** `_STAGE_SPECS` runs `daily_bars` (`scripts/ops.py:11387`) and `fundamentals_refresh` (`:11399`) BEFORE the identity stages `classify_tickers` (`:11441`), `tkr14_backfill` (`:11509`), `ticker_history_backfill` (`:11596`). On an empty DB this loads prices into a substrate with no/partial identity, so the 14 `BEFORE INSERT` triggers stamp NULL or wrong `classification_id` — exactly the rot in audit §2.6 (6.06% pre-FPFD, 92,318 mis-windowed).

3. **`tkr14_backfill` and `ticker_history_backfill` depend on rows that don't exist yet on an empty DB.** `tkr14_backfill mint` reads `WHERE id IS NULL` over EXISTING classification rows (`scripts/ops.py:6426-6435`) and seeds history `valid_from = updated_at::date` (`:6538`). `ticker_history_backfill` derives `valid_from = MIN(prices_daily.date)` / `valid_to = MAX(date) WHERE ever_delisted` (`:8537-8585`) — it requires `prices_daily` already loaded. Both are circular with identity-first and neither uses SEC FPFD / Form 25 boundaries.

**Conclusion: Phase 1 is "build new code FIRST," not "run existing stages."** A new identity-build orchestration + a universe/issuers source-minter must be written. The existing FPFD extractor is fixable in place; the existing enrichers (`symbol_history_evidence_backfill`, `corp_history_edgar_backfill`, `corporate_events_seed`) are reusable as *post-mint* fan-out steps.

---

## Per-table verdicts (file:line + evidence)

### 1. Universe (survivorship-free SEC∪FMP) — **WRITE-NEW**

- **Mint path today:** `classify_all_tickers` (`tpcore/data/classify_tickers.py:322`); source = Alpaca `/v2/assets status=active` (`:191`); persist via `_UPSERT_SQL` (`:202`) / `upsert_classifications_with_source_snapshot` (`:238`).
- **Evidence it's wrong for the rebuild:**
  - **Active-only** → survivorship-violating (spec §1.2 decision 3, invariants G1/G3). Delisted + delisted-then-reused tickers never enter.
  - **Alpaca-sourced**, not SEC full company list ∪ FMP symbol+delisting history (spec §5.2 Identity row; §5.3 step 1; audit moratorium #7 forbids FMP-primary identity for US CIK issuers, and Alpaca is not even in the §5.2 identity roster).
  - The skip-guard SELECTs unclassified tickers FROM `prices_daily` (`scripts/ops.py:6317-6322`) — assumes prices already loaded (child-first).
- **No entrypoint assembles the survivorship-free universe.** It is assembled *implicitly* from whatever Alpaca returns active today.
- **Verdict: WRITE-NEW** — a new `scripts/ops.py` stage (e.g. `universe_build`) that unions SEC `company_tickers` + full company list with FMP symbol list + FMP `/symbol-change` + delisting history into a survivorship-free roster, CSV-first (C2), bulk-first (C1). It mints the WHOLE universe's `ticker_classifications` rows (active + delisted), each with `id` (TKR-14), `cik`, `discovery_source`, `current_ticker`, `lifetime_start` (= FPFD, see #3) — NOT an incremental top-up of Alpaca-active unknowns.

### 2. issuers + issuer_history (SEC-first) — **WRITE-NEW (bulk minter) + RUN-EXISTING (enrichers)**

- **Existing writers (all enrichers, none a whole-universe minter):**
  - `corp_history_edgar_backfill` (`scripts/ops.py:8166`; INSERTs at `:8346` issuers, `:8357` issuer_history) — walks **every CIK already in `ticker_classifications`**, reads `submissions.zip` `formerNames`. BULK-file (good pattern) but it only UPSERTs `legal_name` on existing CIKs; it does not set FYE / `sec_document_type_primary` / FPFD and does not mint issuers for the universe.
  - `corporate_events_seed` (`scripts/ops.py:7881`; INSERTs `:7982/7998` issuers, `:8092/8103` issuer_history) — loads a **hand-curated truth-set CSV** (`scripts/seed/corporate_events_seed.csv`). Narrow, not universe-wide.
  - `gleif_lei_backfill` (`scripts/ops.py:6838`) — fills `issuers.lei` for issuers that already exist (`:6884`).
- **Evidence:** there is **no stage that bulk-loads `issuers` from `submissions.zip` for the full universe** (cik, legal_name, FYE, `sec_document_type_primary`, FPFD) as §5.3 step 2 requires. `submissions_bulk_reader.py` exists (`tpcore/sec/submissions_bulk_reader.py`) and the bulk-iterate pattern is proven in `corp_history_edgar_backfill` — but no consumer mints the issuer master.
- **Verdict: WRITE-NEW** for the issuers/issuer_history bulk minter (can reuse `submissions_bulk_reader.py` + the `corp_history_edgar_backfill` ZIP-iterate scaffold). **RUN-EXISTING** for `corp_history_edgar_backfill` (formerNames → issuer_history) and `corporate_events_seed` as *post-mint* enrichers.

### 3. ticker_classifications — TKR-14 mint + lifetime_start=FPFD — **WRITE-NEW (mint) + FIX-EXISTING (FPFD source) + WRITE-NEW (lifetime_start writer)**

- **TKR-14 mint primitive EXISTS and is correct:** `tpcore/identity/tkr14.py::mint` (`:208`). RUN-EXISTING — call it from the new universe-build minter (not from `tkr14_backfill`).
- **`classify_tickers` / `_stage_tkr14_backfill` were the incremental/polluted path, NOT a full-universe minter:**
  - `_stage_tkr14_backfill` mint mode (`scripts/ops.py:6376`) reads `FROM ticker_classifications WHERE id IS NULL` (`:6426-6435`) — backfills TKR-14 onto pre-existing rows; never mints new tickers. Seeds a single open-ended `ticker_history` row with `valid_from = updated_at::date` (`:6538`), NOT FPFD. **Verdict: do NOT use for the rebuild mint** (FIX-or-bypass; superseded by the new universe-build minter).
  - `classify_tickers` (`scripts/ops.py:6298`) — incremental Alpaca-active refresh; see #1.
- **FPFD source is WRONG (the §5.5/A5 defect, still in code):** `tpcore/sec/companyfacts_adapter.py:385` computes `first_public_filing_date = min(primary_report_dates)` — i.e. `min(reportDate)` (earliest fiscal-PERIOD-END), reintroducing look-ahead. Spec §5.5/A5 requires earliest `filingDate` across the FULL submission index. **Verdict: FIX-EXISTING** (`companyfacts_adapter.py:355-403`): compute `first_filing = min(filingDate)` over the full submission index, not `min(reportDate)`. The DB write site is `backfill_sec_metadata` (`scripts/ops.py:3590-3595`), which UPDATEs FPFD onto rows already present (`:3187-3189` drops tickers with no row) — reusable as the FPFD enricher once the source is fixed, but it must run AFTER the mint, not as the minter.
- **`lifetime_start` has NO writer anywhere.** Repo-wide grep for a `lifetime_start` INSERT/UPDATE site returned ZERO matches in `tpcore/`+`scripts/`. The live `1900-01-01` 72.8%-sentinel contamination (spec §1.2 5a) is precisely because nothing populates it and the column DEFAULTed. **Verdict: WRITE-NEW** — the universe-build minter must set `lifetime_start = FPFD` (or earliest `ticker_history.valid_from`) at insert, never the sentinel (the §3.1 schema drops the DEFAULT and makes it NOT NULL, so an unpopulated load now errors instead of silently sentineling — but a writer must still supply the value).

### 4. ticker_history — SCD-2 multi-row for delisted-then-reused — **WRITE-NEW (multi-row build) ; partial reuse of FMP-rename evidence**

- **`_stage_ticker_history_backfill` (`scripts/ops.py:8481`) is SINGLE-ROW + prices-derived:** INSERTs one row per (classification_id, ticker) with `valid_from = MIN(prices_daily.date)`, `valid_to = MAX(date) WHERE ever_delisted` (`:8537-8585`). It does NOT build the multi-row timeline from SEC Form 25 predecessor `valid_to` + successor FPFD (spec §3.1 "Reused-ticker boundary source"). It is a price-window repair, requires prices loaded (child-first), and only ever produces one window per ticker. **Verdict: do NOT use for the reuse cohort.**
- **`_stage_symbol_history_evidence_backfill` (`scripts/ops.py:4351`) covers FMP RENAMES only:** reads FMP `/stable/symbol-change` (`:4487`) cross-walked against `submissions.zip` (`:4495`). Audit §2.4 confirms it built 5,164 predecessor rows but "**not delisted-then-reused tickers**." Spec §3.1 says FMP `/symbol-change` is INSUFFICIENT for cross-CIK reuse (it only covers same-entity renames) — relying on it silently merges two entities' histories. **Verdict: RUN-EXISTING for the same-entity rename case only; WRITE-NEW for the cross-CIK reuse multi-row build** (SEC Form 25 predecessor `valid_to` + successor FPFD `valid_from`).
- The half-open `[)` EXCLUDE constraint is already in the clean schema (Plan 1); the SCD-2 trigger/dispatcher/resolver half-open predicate FIX (spec §1.2 5d, §4.2/§4.3) is a separate heavy-lane code change (triggers + `dispatcher.py:68-69` + `corp_history/__init__.py:42-43,53-55`), tracked in the plan but not in this Phase-1 substrate-build scope.

### 5. issuer_securities — M:N share-class fan-out — **WRITE-NEW (universe fan-out) + RUN-EXISTING (enrichers)**

- **Existing writers are narrow enrichers:** `symbol_history_evidence_backfill` (`scripts/ops.py:4920`, only `different_issuer_reuse`), `corporate_events_seed` (`:8027/8040`, hand-curated CSV). Audit §2.5: live `issuer_securities` had **89 rows / 76 issuers — 99.94% of classifications lack a link.** No stage fans out the full universe's issuer↔security M:N (share-class GOOG/GOOGL) from source.
- **Verdict: WRITE-NEW** for the universe-wide issuer↔security fan-out (links every minted classification to its issuer, share-class column), run as §5.3 step 4 after issuers + classifications exist. RUN-EXISTING `corporate_events_seed` for the curated merger-transfer rows on top.

---

## 6. ORCHESTRATION verdict — exact Phase-1 ordered sequence

There is **no identity-first orchestrator today** (confirmed: `--update` runs `_STAGE_SPECS` in registry order, which is child-first; `cmd_update` at `scripts/ops.py:12455-12490`). Phase 1 must build the substrate via an explicit ordered run. Recommended: a thin new wrapper script (`scripts/run_identity_build.sh`) invoking the stages below in order, OR a documented manual run-order. Either way the stages are:

| # | Stage | Invocation | Verdict |
|---|---|---|---|
| 1 | **universe_build** (NEW) | `python scripts/ops.py --stage universe_build --param dry_run=false` | **WRITE-NEW** — SEC `company_tickers`+full list ∪ FMP symbols + `/symbol-change` + delisting; mints WHOLE-universe `ticker_classifications` (TKR-14 via `tpcore/identity/tkr14.py::mint`), cik, discovery_source, current_ticker, **lifetime_start=FPFD**; survivorship-free. |
| 2 | **issuers_build** (NEW) | `python scripts/ops.py --stage issuers_build --param dry_run=false` | **WRITE-NEW** — bulk-load issuers + issuer_history from `submissions.zip` (cik, legal_name, FYE, sec_document_type_primary, FPFD); reuse `tpcore/sec/submissions_bulk_reader.py` + the `corp_history_edgar_backfill` ZIP scaffold. Add `cik` FK NOT VALID. |
| 2b | corp_history_edgar_backfill | `--stage corp_history_edgar_backfill --param dry_run=false` | **RUN-EXISTING** — formerNames → issuer_history enrichment on top of #2. |
| 3a | backfill_sec_metadata (FPFD/doc-type/FYE enrich) | `--stage backfill_sec_metadata --param dry_run=false` | **FIX-EXISTING** — only after the `companyfacts_adapter.py:385` `min(reportDate)→min(filingDate)` fix; enriches FPFD on the already-minted rows (it cannot mint — `scripts/ops.py:3187`). |
| 3b | **ticker_history reuse build** (NEW) | `python scripts/ops.py --stage ticker_history_reuse_build --param dry_run=false` | **WRITE-NEW** — multi-row SCD-2 for cross-CIK delisted-then-reused from SEC Form 25 `valid_to` + successor FPFD `valid_from`. |
| 3c | symbol_history_evidence_backfill | `--stage symbol_history_evidence_backfill --param use_bulk_zip=true --param dry_run=false` | **RUN-EXISTING** — same-entity FMP renames only (NOT cross-CIK reuse). |
| 4 | **issuer_securities fan-out** (NEW or fold into #2) | `python scripts/ops.py --stage issuer_securities_build --param dry_run=false` | **WRITE-NEW** — universe-wide M:N issuer↔security links + share-class. |
| 4b | corporate_events_seed | `--stage corporate_events_seed --param dry_run=false` | **RUN-EXISTING** — curated merger/transfer rows. |
| GATE | Identity gate (Task 1.4) | re-run boundary-oracle sentinel + assertions | BLOCKING — 0 rows `lifetime_start='1900-01-01'`, 0 NULL `lifetime_start`, multi-row history for the reuse cohort, no EXCLUDE overlap. If not clean, STOP before any child load. |

**Stages that must NOT run as the rebuild minter (would re-introduce the rot):** `classify_tickers` (Alpaca-active incremental), `_stage_tkr14_backfill` (id-IS-NULL backfill + non-FPFD history seed), `_stage_ticker_history_backfill` (single-row, prices-derived). They are the incremental/polluted paths the audit named; the rebuild replaces them with #1/#3b above.

**Then (Phase 2+, child loads): `daily_bars` → `corporate_actions`/`adjusted_close` → `fundamentals_refresh`+`sec_fundamentals_fallback` → signals → derived**, all RUN-EXISTING, in the §5.3 order — triggers attribute correctly because the substrate is now correct-first.

### WRITE-NEW list (scope estimate)
1. `universe_build` stage + survivorship-free SEC∪FMP roster assembler + whole-universe TKR-14 mint with `lifetime_start=FPFD` — **largest item** (~1 new handler module + stage; TDD the mint-from-source + lifetime_start-no-sentinel).
2. `issuers_build` bulk minter from `submissions.zip` (medium; reuses `submissions_bulk_reader.py`).
3. `ticker_history_reuse_build` cross-CIK multi-row SCD-2 from SEC Form 25 + successor FPFD (medium; research-heavy per audit §5 step 5 — "200-500 system-wide").
4. `issuer_securities_build` universe fan-out (small-medium; could fold into #1/#2).
5. Identity-first run wrapper `scripts/run_identity_build.sh` or a documented run-order (small).

### FIX-EXISTING list
1. `tpcore/sec/companyfacts_adapter.py:355-403` — FPFD = `min(filingDate)` over the full submission index, not `min(primary_report_dates)`/`min(reportDate)` (§5.5/A5). (heavy-lane `ingestion_or_backfill_change`; gate via CIC.)

---

## 7. §5.5 population-fix audit (in-code today vs Phase-1-must-add)

| §5.5 fix | In code today? | file:line | Verdict |
|---|---|---|---|
| **FPFD = earliest `filingDate`** (A5) | **NO** — uses `min(reportDate)` | `tpcore/sec/companyfacts_adapter.py:385` | **FIX-EXISTING** |
| **lifetime_start from FPFD, no `1900-01-01` sentinel** (A6) | **NO** — no `lifetime_start` writer exists at all (repo-wide grep = 0 sites); schema DEFAULT drop is Plan-2 DDL but the *writer* is absent | (none) | **WRITE-NEW** (set in universe_build mint) |
| **current_ticker is canonical survivor; drop `ticker` / repoint UPSERTs** | **NO** — `_UPSERT_SQL` keys `ON CONFLICT (ticker)`; mint writes `ticker`, not `current_ticker` | `tpcore/data/classify_tickers.py:206`; `scripts/ops.py:6526-6531` (tkr14 UPSERT on `tc.ticker`) | **WRITE-NEW/FIX** — the new minter writes `current_ticker`; repoint/justify per spec §3.1 (plan-PR CIC decision) |
| **multi-row ticker_history for delisted-then-reused** (A4/G3) | **NO** — `_stage_ticker_history_backfill` is single-row, prices-derived; `symbol_history_evidence_backfill` is renames-only | `scripts/ops.py:8537-8585`; `:4351` | **WRITE-NEW** (cross-CIK reuse build from SEC Form 25) |
| **predecessor-classification backfill** (A4) | PARTIAL — renames only via FMP | `scripts/ops.py:4351` | RUN-EXISTING (renames) + WRITE-NEW (cross-CIK reuse) |
| **identity-aware validators** (F6) | NO (audit §2.9) | `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` | out of Phase-1 substrate scope (Phase 6 / separate) |
| **read-side `as_of`** (A2) | NO (audit §2.9) | `tpcore/identity/dispatcher.py:68-69`; `tpcore/corp_history/__init__.py:42-43,53-55`; reader violators per §4.3 | out of Phase-1 substrate scope (separate heavy-lane) |

---

## 8. SWV 10-point trace — "build the identity substrate from source"

1. **Writers.** Minters: `classify_all_tickers` (Alpaca-active, `tpcore/data/classify_tickers.py:322`) — WRONG source. TKR-14 onto existing rows: `_stage_tkr14_backfill` (`scripts/ops.py:6376`). issuers: enrichers only (`scripts/ops.py:4890,7982,7998,8346`). ticker_history: `_stage_ticker_history_backfill` (`:8481`, single-row) + `symbol_history_evidence_backfill` (`:4351`, renames). FPFD: `backfill_sec_metadata` (`:3590`). lifetime_start: **none**. → A source-of-truth minter for universe/issuers/lifetime_start does not exist.
2. **Readers.** Triggers attribute `classification_id` from `ticker_history` at insert (audit §2.8; 14 in the rebuild). Engine readers use `IdentityDispatcher` (`dispatcher.py`) + PricesRepo — the audit §2.9 bypass cohort (separate fix). Phase-1 substrate build is upstream of all readers; correct-first means readers inherit a clean substrate.
3. **Source authority (SEC-first).** Spec §5.2: identity = SEC EDGAR (`submissions.zip`, full company list) primary, FMP non-US fallback. Today's minter is **Alpaca** (`classify_tickers.py:191`) — not in the §5.2 identity roster; audit moratorium #7 forbids FMP-primary identity for US issuers and Alpaca-primary is worse. WRITE-NEW must be SEC-first.
4. **Existing controls.** 14 BEFORE INSERT triggers (correct write-side); the half-open SCD-2 predicate FIX (§4.2) pending; `ticker_history_no_overlap` EXCLUDE (live); the 32-check suite + `DATA_OPERATIONS_COMPLETE` 100%-green gate (Phase 6). The identity gate (Task 1.4) is the new BLOCKING control before child loads.
5. **Tests.** `tests/test_symbol_history_evidence_backfill_stage.py`, `test_p2_sec_lifecycle_events.py`, `test_p2b_lifecycle_evidence_wiring.py`, `test_fpfd_bulk_repair_closeout_documented.py`, `test_identity_substrate_audit_documented.py`. **Gap:** no test asserts a from-empty whole-universe mint, no test asserts `lifetime_start≠sentinel` at mint, no test asserts cross-CIK multi-row history. The new stages get TDD (factor-math style: mint-from-source, lifetime_start-no-sentinel, reuse multi-row).
6. **Config / env.** `SEC_EDGAR_USER_AGENT` (required by the SEC bulk stages, `scripts/ops.py:5209`), FMP key, `DATABASE_URL`/`DATABASE_URL_IPV4`. DFCR governs the provider roster — `tpcore/providers.py` not hand-edited. Bulk-zip cache `/tmp/sec_submissions.zip`.
7. **Blast radius.** Identity substrate is the root of the `ticker+date→classification_id→CIK` chain; every child table FK + every engine reader depends on it. A wrong mint poisons all child attribution at load (the rot the rebuild cures). Highest-blast-radius layer in the system → correct-first + BLOCKING gate is mandatory.
8. **Rollback.** Phases 4-9 are idempotent re-run-from-TRUNCATE (spec §8.3); the pre-wipe snapshot (Plan-2 step 1) + Supabase PITR backs the irreversible schema cutover. A bad identity build → TRUNCATE the substrate + re-run #1-#4; no child load proceeds past the Task-1.4 gate.
9. **Adjacent callers.** `gen_engine_manifest.py` sentinel fences; `run_data_operations.sh` lock; `ops/platform_pipeline.py` docstrings (sentinel-fenced). New stages must register in `_STAGE_SPECS` (`KNOWN_STAGES` sentinel `:11804`) + the stage-present tests.
10. **Verdict.** **DISCOVERY → resolved to WRITE-NEW-FIRST.** This note IS the discovery output. Phase 1 is "build new code first" (universe_build, issuers_build, ticker_history_reuse_build, issuer_securities fan-out, + the companyfacts FPFD fix), then RUN-EXISTING enrichers, then the BLOCKING identity gate. The new stages are heavy-lane `ingestion_or_backfill_change` — each goes through `/system-wide-verification` + `/change-impact-classification` per the discovery-first rule, citing the identity chain explicitly.

---

## STATUS

**COMPLETE — read-only; no DB mutation, no stage run, no migration, no push.**

Bottom line: **Phase 1 = BUILD NEW CODE FIRST, not run existing stages.** The existing `scripts/ops.py` identity stages are incremental enrichers/repairers over an Alpaca-seeded, prices-derived substrate; they cannot mint a survivorship-free, SEC-first, FPFD-anchored identity layer on an empty DB and several depend on `prices_daily` being pre-loaded (child-first). WRITE-NEW: universe_build (incl. whole-universe TKR-14 mint + lifetime_start=FPFD), issuers_build (submissions.zip bulk), ticker_history_reuse_build (cross-CIK multi-row), issuer_securities fan-out, + an identity-first run wrapper. FIX-EXISTING: `companyfacts_adapter.py:385` FPFD `min(reportDate)→min(filingDate)`. RUN-EXISTING (post-mint enrichers): corp_history_edgar_backfill, symbol_history_evidence_backfill (renames only), corporate_events_seed, backfill_sec_metadata (FPFD enrich, after the fix). The TKR-14 `mint` primitive (`tpcore/identity/tkr14.py:208`) is correct and reused.
