# Identity substrate + data-flow audit — 2026-06-03

Read-only audit of identity, prices, fundamentals, lifecycle substrate, downstream consumers, and write triggers against the live DB. **No DB writes, no migrations, no code changes, no repair implementation in this PR.** Output of a multi-step audit; intended as a durable baseline before any repair work begins.

## 1. Verdict

The system's schema is **structurally capable** of the temporal-identity (SCD-2) model the operator intended. The write side **mostly works correctly** — 15 platform tables carry `BEFORE INSERT` triggers that resolve `classification_id` from `ticker_history` keyed on the row's date column. The problems are:

1. **Population gaps** — `ticker_history` is single-row for 96% of tickers; predecessor entities for delisted-then-reused tickers are not captured.
2. **Read-side bypass** — engine backtests + repos call `IdentityDispatcher.ticker_to_classification_id(ticker)` without passing `as_of`, getting current-entity attribution for all historical dates.
3. **Historical rows attached to whatever cls was active at insert time** — when `ticker_history` was sparse during insert, the trigger correctly assigned the only-cls-available; later predecessor backfills don't auto-update those rows.
4. **Identity-master coverage holes** — FPFD is only 16.6% populated; `lifetime_start` is the sentinel `1900-01-01` for 100% of active classifications; `issuer_securities` is essentially empty (89 rows).
5. **Recent arcs added speculative or polluted substrate** — `fundamentals_period_source_evidence` (506 rows, today) is date-key polluted and should be reset before reuse.

The schema can support what the operator asked. The data inside it cannot, today, without repair.

## 2. Synthesized baseline (verified against live DB on 2026-06-03)

### 2.1 Database structure

- **49 platform tables** (all have primary keys; zero PK-less tables).
- **22 foreign keys** (sparse — most identity relationships are by-convention only).
- **72 CHECK constraints**.
- **1 EXCLUDE constraint** (`ticker_history_no_overlap` on `ticker_history`).
- **154 indexes**.
- **17 triggers**, including:
  - **15 SCD-2 assignment triggers** that populate `classification_id` on insert by looking up `ticker_history` at the row's date column.
  - 2 housekeeping triggers (`updated_at` touch, spread-observations retention).

### 2.2 Identity model

The intended path:

```
(ticker, date)
   ↓ ticker_history (SCD-2: valid_from <= date < COALESCE(valid_to, ∞))
   ↓
classification_id (TKR-14, internal stable surrogate)
   ↓ ticker_classifications
   ↓
{cik, figi, cusip, isin, lei, current_legal_name, asset_class, …}
```

### 2.3 Identity master health — `ticker_classifications`

- Total rows: 19,004. Active (`lifetime_end IS NULL`): **12,344**. Retired: 6,660.
- Active identifier coverage:
  - CIK: **75.4%** (9,310). 3,034 active rows missing CIK.
  - FIGI: **93.5%** (11,545).
  - CUSIP: **83.3%** (10,286).
  - ISIN: **89.1%** (10,998).
  - **`first_public_filing_date` (FPFD): 16.6%** (2,050).
- **`lifetime_start = '1900-01-01'` (sentinel) on 100% of active rows.** Column is functionally null.
- 702 tickers have multiple historical classifications (ticker-reuse captured).
- 1,046 CIKs have multiple historical classifications (entity tied to multiple ticker eras).

### 2.4 `ticker_history` health

- Total rows: **19,013** for 18,264 distinct tickers.
- **17,554 tickers (96%) have exactly 1 history row.** Schema supports multi-row temporal history but most tickers have one open-ended row covering all dates.
- 710 tickers with multi-row history exist (mostly from yesterday's symbol-history populate of 5,164 predecessor classifications from FMP's `/stable/symbol-change` feed; covers SPAC mergers and related corporate actions but not delisted-then-reused tickers).

### 2.5 `issuer_securities` health

- Total rows: **89**, for 76 distinct issuers.
- **99.94% of active classifications lack any `issuer_securities` link.** Rank-3 substrate is functionally empty.

### 2.6 `prices_daily` attribution integrity

- Total bars: 21,383,303.
- NULL `classification_id`: **0**.
- Orphan `classification_id` (cls not in `ticker_classifications`): **0**.
- Date range: 1994-07-21 → 2026-06-02.
- **Bars attributed to entities that didn't exist yet per their assigned cls's FPFD: 1,296,359 (6.06%) across 1,149 tickers and 1,149 classifications.**
- Bars after assigned cls's `lifetime_end`: 35 (across 27 tickers).
- Bars **outside `ticker_history` valid window** for the assigned cls: **19,964**.
- Bars whose `(ticker, date)` would resolve to a *different* classification under existing `ticker_history` rows: **92,318 across 266 tickers**. These are bars where the substrate is now correct but historical rows weren't re-attributed after predecessor classifications were added.

### 2.7 `fundamentals_quarterly` attribution integrity

- Total rows: **183,421**.
- **PK is surrogate `(id)`** — *not* `(ticker, period_end_date)`.
- Has `classification_id` column: yes. NULL rows: **2,153 (1.2%)**.
- **Duplicate logical `(ticker, period_end_date)` quarters: 109.** Surrogate PK doesn't prevent this.
- Rows before assigned active cls's FPFD: **6,017 across 775 tickers (3.28%)**.
- Rows without active classification (orphan): **1,034**.
- Rows on tickers with multiple classifications: 9,361 across 327 tickers.
- 1,603 rows inserted today across 58 tickers (from the bounded `confirmed_data_gap_evidence_populator` live + the 10-ticker historical backfill).

### 2.8 Write-side: SCD-2 assignment triggers (good news)

Empirically, **15 platform tables carry `BEFORE INSERT` triggers** that auto-assign `classification_id` via:

```sql
SELECT classification_id INTO NEW.classification_id
FROM platform.ticker_history
WHERE ticker = NEW.ticker
  AND valid_from <= NEW.<date_column>
  AND (valid_to IS NULL OR valid_to >= NEW.<date_column>)
ORDER BY valid_from DESC LIMIT 1;
```

Per-table date column:
- `prices_daily.date`
- `fundamentals_quarterly.period_end_date`
- `corporate_actions.action_date`
- `earnings_events.event_date`
- (plus `insider_transactions`, `sec_material_events`, `short_interest`, `borrow_rates`, `liquidity_tiers`, `insider_sentiment`, `social_sentiment`, `spread_observations`, `universe_candidates`, `options_max_pain`, `aar_events`)

**This means the write-side identity-attribution architecture is correct.** The 6% mis-attribution surface is the **consequence of historical rows being inserted when `ticker_history` was sparse**, not of a broken trigger or a wrong writer.

**Practical implication for repair**: a one-time idempotent `UPDATE` pass re-running the trigger SELECT against existing rows would re-attribute the 92,318 prices_daily bars (and equivalent fundamentals_quarterly rows) without any code change.

### 2.9 Read-side: consumer bypass

`IdentityDispatcher.ticker_to_classification_id(ticker, as_of)` correctly implements SCD-2 — its docstring even spells out *"Backtests crossing renames pass the row-date here."* Then:

| Consumer | Call shape | Verdict |
|---|---|---|
| `tpcore/backtest/price_loader.py:61` | `await dispatcher.ticker_to_classification_id(t)` (no `as_of`) | **Bypass** |
| `tpcore/data/repositories/prices.py` (PricesRepo) | `WHERE classification_id = ANY($1)` (no date filter) | **Bypass** |
| `momentum/backtest.py:223,531` | `ticker_to_classification_id(t)` | **Bypass** |
| `catalyst/backtest.py:317,370,423` | `ticker_to_classification_id(t)` | **Bypass** |
| `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` | (zero refs to ticker_history / lifetime_start / FPFD) | **No identity awareness** |
| `tpcore/identity/dispatcher.py` | Native SCD-2 implementation | **Correct** |
| `tpcore/sec/companyfacts_adapter.py` | Writes FPFD | **Correct writer** |
| `tpcore/alpaca/broker_adapter.py::submit_order` | `symbol=ticker` (Alpaca uses ticker string) | **Order routing not contaminated** |
| `tpcore/risk/governor.py::check_trade` / `check_lifecycle` | Operates on ticker string | **Risk path not contaminated** |

Engine backtest content is contaminated for the affected ~1,400+ ticker cohort (1,149 pre-FPFD + 266 distinct-cls-via-ticker_history). Live order routing is keyed on ticker string and is *not* directly polluted by `classification_id` corruption.

### 2.10 Universe construction

- `universe_candidates`: 4,591 rows; 4,564 match active classifications.
- Tier-1 + tier-2 (engine-eligible) cohort includes:
  - 1,415 stocks (tier 1) + 99 stocks (tier 2)
  - 308 + 178 vanilla ETFs (tiers 1 + 2)
  - 70 leveraged ETFs (tier 1)
  - **47 SPAC units (tier 1) + 36 SPAC units (tier 2)** — engine universe filters by tier alone would include these
  - 106 sponsored ADRs (tier 1)
- **34 active tickers ending in `U` (units) or `W` (warrants) carry `asset_class='stock'`** — misclassification leak.

### 2.11 Identifier conflicts

- FIGI duplicated across active CIKs: 0.
- CUSIP duplicated across active CIKs: 4 (small).
- Active CIKs with multiple active classifications: top entries are legitimate ETF issuers (iShares 265 ETFs, ProShares 108, SPDR 78). Not a bug.
- Duplicate `(ticker, date)` in `prices_daily`: 0.
- Duplicate `(ticker, action_date, action_type)` in `corporate_actions`: 0.
- Duplicate `(ticker, period_end_date)` in `fundamentals_quarterly`: **109**.

### 2.12 Recent-arc side effects

- `alembic_version`: `20260602_0200`. Two migrations were applied during today's bounded operator run (`20260602_0100` archive/quarantine sidecar from PR #441 + `20260602_0200` evidence substrate from PR #452). Both forward-compatible; keep.
- **`fundamentals_period_source_evidence` (506 rows / 50 tickers): POLLUTED.** Today's bounded `confirmed_data_gap_evidence_populator` live run wrote evidence rows keyed on FMP's natural `period_end_date` (e.g., `2024-10-20`) rather than the validator's inferred period_end_date (e.g., `2024-10-31`). The validator's join requires both sources at the same date and never matches. **Reset before reuse.**
- `ticker_classifications` rows added by yesterday's `symbol_history_evidence_backfill` (5,164 predecessor rows, source `symbol_history_evidence_backfill.F` / `.S`): valid for what they cover. **Keep.**
- `ticker_history` rows added by same (5,164): valid. **Keep.**
- `issuer_securities` (64 SEC-resolved rows): thin but valid. **Keep.**
- `failed_alpha_ledger` (5 rows from Sunday's F1 closeout): valid. **Keep.**
- `fundamentals_quarterly_archive` + `fundamentals_quarterly_quarantine`: schemas applied (PR #441); both empty; arc was paused. **Keep schemas; do not delete.**

### 2.13 `DATA_OPERATIONS_COMPLETE` dependency graph

DOC is emitted at the end of `tpcore.quality.validation.suite.run_suite` only if **all 32 validation checks pass**. The 32 checks include all freshness/completeness/integrity checks on the substrate; there are no advisory checks. Per CLAUDE.md: **"DATA_OPERATIONS_COMPLETE is NEVER emitted unless self-heal returns 100% green ('100% data or don't trade', structural)."**

**Today's blocker**: `fundamentals_quarterly_completeness` reports **111 ticker-FAIL count**. Until that drops to 0 (or the failing tickers move to a legitimate exclusion bucket via repaired identity), DOC never fires, engine services don't dispatch, and the daily flow is stalled. Other checks may also currently fail; this audit doesn't enumerate the full failing set, only that `fundamentals_quarterly_completeness` is known-failing.

## 3. Table classification matrix

49 platform tables today; target working footprint is **~20 tables**. Classification (one row per current table):

| Table | Class | Note |
|---|---|---|
| `ticker_classifications` | KEEP | Identity master. Repair coverage. |
| `ticker_history` | KEEP | SCD-2 temporal map. Repair sparsity. |
| `issuer_securities` | MERGE_CANDIDATE (→ `ticker_classifications`) | 89 rows; identifier already on cls |
| `prices_daily` | KEEP | Substrate. Re-key via trigger UPDATE. |
| `fundamentals_quarterly` | KEEP | Substrate. Migrate to natural PK; resolve 109 dups; consolidate sidecars |
| `corporate_actions` | MERGE_CANDIDATE (→ `corporate_events`) | Just an `event_kind` enum extension |
| `corporate_events` | KEEP (consolidation target) | Absorbs lifecycle/history/actions tables |
| `ticker_lifecycle_events` | MERGE_CANDIDATE (→ `corporate_events`) | Form 25/15 events fit |
| `issuer_history` | MERGE_CANDIDATE (→ `corporate_events`) | `event_kind='name_only_change'` |
| `fundamentals_period_source_evidence` | POLLUTED_RESET_CANDIDATE (→ `data_quality_log`) | 506 polluted rows; semantic should fold into log |
| `fundamentals_quarterly_archive` | EMPTY_SPECULATIVE / MERGE_CANDIDATE | Cleanup arc paused; fold into FQ via `status` enum |
| `fundamentals_quarterly_quarantine` | EMPTY_SPECULATIVE / MERGE_CANDIDATE | Same |
| `failed_alpha_ledger` | MERGE_CANDIDATE (→ `data_quality_log`) | 5 rows; one-shot use |
| `data_quality_log` | KEEP (consolidation target) | Absorbs evidence/ledger/parity/forensics tables |
| `application_log` | KEEP | Single ops log; DOC emission stream |

Not classified here (but enumerated in the audit-extension synthesis): `liquidity_tiers`, `universe_candidates`, `earnings_events`, `macro_data`, `insider_transactions`, `tradier_options_chains`, `sec_material_events`, `aar_events`/`aar_deferred`, `short_interest`, `borrow_rates`, `social_sentiment`, `spread_observations`, `aaii_sentiment`, `daemon_heartbeats`, `series_catalog`, `insider_sentiment`, `risk_state`/`risk_close_ledger`, `open_orders`, `allocations`, `prices_daily_staging`, `ingest_manifest`/`ingest_quarantine`/`ingestion_metrics`, `forensics_triggers`, `parity_drift_log`, `provider_binding_state`, `split_pre_image_log`, `options_max_pain`, `borrow_rates`, `earnings_events_count_snapshot`, `sec_insider_row_counts_snapshot`, `ticker_classifications_source_count`, `alembic_version`.

Per-table verdicts for the unclassified set follow the same pattern: KEEP for substrate-bearing tables, VIEW_CANDIDATE for monotone snapshots, EMPTY_SPECULATIVE / DROP_CANDIDATE for never-populated machinery. Full matrix to be ratified in the repair-plan PR after operator review.

## 4. Moratorium rules (effective on merge)

1. **No new platform tables** without explicit operator-approved schema rationale documented in `docs/`. Default answer for any future arc proposing a new sidecar table: "could this be a column / `kind` enum on an existing table?"
2. **No validator patches before identity-substrate repair.** Symptoms in `fundamentals_quarterly_completeness` etc. trace upstream to identity attribution; patching the validator first masks the upstream defect.
3. **No `confirmed_data_gap_evidence_populator` runs** until the polluted `fundamentals_period_source_evidence` substrate is reset and the FMP-leg date-key bug is fixed.
4. **No broad fundamentals or price backfill** until `ticker_history` / `classification_id` attribution repair is planned and the SCD-2 trigger UPDATE pass is scoped.
5. **No `cleanup_ticker_reuse_fundamentals` / quarantine / delete** until this audit's repair order is approved.
6. **No schema consolidation migrations** until the dependency graph and repair plan are explicitly approved (consolidation candidates documented above; merge migrations are a separate arc).
7. **No FMP-primary identity repair for U.S. CIK-backed issuers.** SEC/CIK is authoritative for U.S. issuers; FMP is fallback only and must not override SEC identity without explicit divergence handling.

These moratoria are advisory documentation, not test-enforced. They become test-enforced (sentinel CI checks) only after the repair plan is approved.

## 5. Repair order

The repair sequence is deliberately **identity-first**. Validator fixes, backfill fixes, and evidence-substrate rebuilds all come *after* identity attribution is correct.

1. **Publish this audit baseline and freeze new table creation** (this PR).
2. **Reset polluted `fundamentals_period_source_evidence`** before reuse.
3. **Re-run SEC-first FPFD extraction against full active universe** and verify against known mega-cap samples (LMT, PEP, etc.) to ensure the extractor's "first filing date" semantic is correct (earliest filing, not stale-state).
4. **Repair `lifetime_start`** — backfill from SEC-backed FPFD where available, else from earliest ticker_history valid_from.
5. **Backfill missing predecessor classifications and `ticker_history` windows** for the delisted-then-reused cohort (the 10 worst offenders + likely 200-500 system-wide). Research-heavy multi-day arc requiring SEC historical CIK lookup.
6. **Re-run the existing SCD-2 trigger logic as an idempotent `UPDATE` pass** against existing `prices_daily`, `fundamentals_quarterly`, and other triggered tables. The trigger logic is correct; this just applies it retroactively to rows inserted when `ticker_history` was sparse. Fixes the 92,318 prices_daily and equivalent fundamentals_quarterly mis-attributed rows without code changes.
7. **Add `NOT VALID FK`** from `prices_daily.classification_id` → `ticker_classifications.id`, then `VALIDATE` after repair completes.
8. **Repair `fundamentals_quarterly` `classification_id`** attribution and resolve the 109 duplicate logical quarters (likely requires migration to natural PK `(ticker, period_end_date)`).
9. **Fix engine and backtest readers** to pass `as_of=row_date` into `IdentityDispatcher.ticker_to_classification_id`. Affects `tpcore/backtest/price_loader.py`, `tpcore/data/repositories/prices.py`, `momentum/backtest.py`, `catalyst/backtest.py`, plus the not-yet-audited `reversion`, `vector`, `sentinel`, `canary` engine paths.
10. **Audit and fix universe construction filters** for `asset_class` and `instrument_subtype`. The 34 stock-class U/W-suffix tickers and the 83 SPAC units in the tier-1+2 cohort need to be filtered consistently.
11. **Only after identity repair**: fix validator inference (`_infer_missing_period_ends`) to consume temporal identity (clamp to FPFD or `lifetime_start` for the current cls).
12. **Only after identity repair**: fix the `confirmed_data_gap_evidence_populator` FMP-leg date-key semantics.
13. **Rebuild the evidence substrate cleanly** — via `data_quality_log` rows with `kind='confirmed_data_gap_evidence'` per the consolidation matrix, not a separate sidecar table.
14. **Re-run the validation suite** and observe failure counts.
15. **Re-run `DATA_OPERATIONS_COMPLETE`**.

## 6. What this PR does NOT do

- No DB writes.
- No migrations.
- No code changes (no `tpcore/`, `scripts/`, `platform/migrations/`, `.github/workflows/`, `.claude/`, `data/`).
- No table creation.
- No table drops.
- No repair implementation.
- No validator patch.
- No backfill.
- No cleanup / quarantine / delete.
- No additional live write-bearing runs.

## 7. Sources

- Live DB queries against the operator's Supabase pooler (DATABASE_URL_IPV4), executed 2026-06-02 / 2026-06-03 during a multi-step audit pass.
- Repo grep against the worktree at `origin/main` HEAD `16968cd`.
- Per-table empirical evidence captured at `/tmp/audit/step*.json` (operator-local; not tracked).
- Cross-referenced against `CLAUDE.md` (validator gate semantics, paper-trading mandate, no-`yfinance` rule).
- Prior arc artifacts referenced: PR #441 (cleanup arc paused), PR #444 (symbol-history populate), PR #448 (sec_fundamentals_fallback dry-run knob), PR #452 (evidence-gated cleanup impl), PR #453/#454 (populator dry-run + return-shape fixes).
