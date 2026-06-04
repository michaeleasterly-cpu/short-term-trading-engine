# Data-layer rebuild — design spec v1.2 (clean re-ingest, identity-first)

**Status:** SPEC v1.2 — gated docs-only deliverable (heavy-lane step 3). NO code, NO DDL, NO migration, NO DB access in this PR. The 7 moratoria of `docs/audits/2026-06-03-identity-substrate-data-flow.md` §4 remain in force: **no DB mutation until this spec is operator-approved.** This spec is the input the operator needs to lift the moratoria for the rebuild plan PR.

## Revision history

- **v1.1 → v1.2 (key-consistency re-review).** db-architect re-reviewed the key model ("look it over again" — should everything key on TKR-14?):
  - **Child substrate tables KEEP natural PKs `(ticker, <date>)` + nullable `classification_id` FK — confirmed correct, NOT switched to `(classification_id, <date>)`.** Live-proven: across the 20-ticker reuse cohort there are ZERO `(ticker, date)` collisions, because `ticker_history`'s half-open EXCLUDE already guarantees one security per ticker per day; and `classification_id` must stay NULLABLE (the triggers leave it NULL when identity isn't yet resolvable, so it can be backfilled, and the FK-NOT-VALID→VALIDATE load needs it nullable) — making it a PK column would turn a recoverable gap into a hard INSERT abort. TKR-14 is the universal *relation* (the FK on all 19 substrate tables), not the child primary key. The one identity-keyed table, `issuer_securities`, is correct because it's a relationship table.
  - **Half-open SCD-2 fix EXTENDED to the read layer** (§4.3 / §1.2 decision 5d): live verification found `IdentityDispatcher` (`dispatcher.py:68-69`) and the `corp_history` resolver (`__init__.py:42-43,53-55`) ALSO use the closed predicate (v1.1 wrongly said the dispatcher was correct). All three change together; the boundary sentinel asserts against the `ticker_history` `daterange('[)')` oracle, NOT trigger-vs-dispatcher (which would agree by mutual wrongness).
  - **`aar_events` FK gap** (§3.4): migration `20260524_1903` added the `classification_id` column but never the FK; live `aar_events` has zero FKs. The rebuild must add it (the v1.1 "(FK)" label was aspirational).
  - Confirmed: `ticker_history` correctly maps ticker→TKR-14 over time; CIK is an attribute, never a key; `issuers` correctly uses `issuer_id` (not TKR-14) on granularity.

- **v1.0 → v1.1 (SME-review fixes).** Folds in two SME reviews (finance + DB). Material changes:
  - **FPFD semantic corrected** (A5 / §3.1 / §5.5): FPFD = earliest `filingDate` across the issuer's full EDGAR submission index — NOT `min(DocumentPeriodEndDate)` (which is earliest fiscal-period-end and reintroduces look-ahead). A separate `first_periodic_report_date` boundary is named for the "first periodic-fundamentals available" concept.
  - **`fundamentals_quarterly` PK is now 3-part** `(ticker, period_end_date, filing_date)` (reconciled across §3.2 / §2.1 / §1.2 decision 8 / D1 / I2 / the Provenance + §5.3/§8.2 ingest steps). The apparent "duplicates" are AMENDED/restated filings; a 2-part key destroys restatement history. Dedup is now restatement-preserving (ON CONFLICT on the 3-part key). The 267 figure is amended-filing rows, not garbage dups.
  - **`adjusted_close` respec'd as a cumulative split+dividend factor** over RAW `close` (§3.2 / §5.6 / OQ-3 / D4) — idempotent, dividend- and reverse-split-aware; `close` stays raw and PIT-honest. `split_pre_image_log` removed from the §2.3 DROP list (conditioned on the factor model landing).
  - **SCD-2 trigger off-by-one FIXED** in the rebuild (§3.1 / §4.1 / §4.2): half-open `(valid_to IS NULL OR NEW.date < valid_to)` predicate — surfaced as a deliberate 4th identity-layer change beyond §1.2's three, with a trigger-vs-IdentityDispatcher boundary sentinel.
  - **`corporate_actions` kept as a thin standalone table** for the split-adjust hot path (§2.2 / §5.6 / OQ-3); only `ticker_lifecycle_events` folds into `corporate_events`.
  - **`data_quality_log` reframed as a redesign** (§3.3) with `notes` as jsonb + per-`kind` partial indexes — not a transcription.
  - Dup count standardized on **267** (live-verified amended-filing rows) everywhere.
  - `lifetime_start` DEFAULT `'1900-01-01'` explicitly dropped (§3.1 / A6); `ticker` vs `current_ticker` survivor declared (§3.1); `short_interest` PIT boundary + trigger pinned to `release_date` (§3.1 / §3.3); foreign-filer / Form-4 / reused-ticker / SPAC-unit nuances added.

**Author / role:** in-thread heavy-lane session, grounded against `docs/DATA_INVARIANTS.md` (the 113 invariants this rebuild MUST preserve), `docs/DATABASE_AND_DATAFLOW.md` §0/§2, `docs/audits/2026-06-03-identity-substrate-data-flow.md` (§2 health, §3 matrix, §4 moratoria, §5 repair order), the LOCKED identity design (`2026-05-23-referential-integrity-design-v2.2.md` + `2026-05-24-corporate-history-enrichment.md`), the live migrations under `platform/migrations/versions/`, and `.claude/rules/{identity-path,discovery-first,data-adapter,migrations,selfheal-auditheal,data-feed-roster}.md`.

**Provenance:** the 2026-06-03 identity-substrate audit established the baseline (49 platform tables; structurally-correct write side; population/attribution rot; read-side `as_of` bypass; 100% `lifetime_start` sentinel; 16.6% FPFD coverage; 6.06% pre-FPFD bars; 92,318 mis-windowed bars; 267 amended-filing rows on duplicate logical `(ticker, period_end_date)` quarters — see §3.2: these are restatements, not garbage). The operator's decision — recorded as the scope of this spec — is a **full data-layer rebuild via clean re-ingest** (new clean schema, re-pull from source) rather than preserve-in-place repair, **except for SACRED / un-re-pullable series which are copied verbatim**. Tradier closed 2026-06-03; options are out.

---

## 1. Goal + settled decisions

### 1.1 Goal

Produce a clean, survivorship-free, identity-first data layer of **~20 working platform tables** that satisfies every invariant in `docs/DATA_INVARIANTS.md`, re-ingested from authoritative source so that no documented mistake is re-committed. The acceptance bar is unchanged and structural: **`DATA_OPERATIONS_COMPLETE` fires only when all 32 validation checks are 100% green** (invariant F1; `.claude/rules/selfheal-auditheal.md`).

### 1.2 Settled decisions (do NOT re-open)

1. **Method = CLEAN RE-INGEST, not preserve-in-place.** Build the clean schema; re-pull every re-pullable series from source. The 6.06% pre-FPFD / 92,318 mis-windowed rot is not patched row-by-row; it is replaced by a correct identity-first re-ingest. (The "267 duplicate quarters" are NOT rot — they are amended/restated filings preserved by the 3-part `fundamentals_quarterly` PK; see decision 8 + §3.2.)
2. **The rebuild touches TICKER / identity data ONLY. `macro_data` is entirely OUT of scope — preserved in place, untouched** (operator decision 2026-06-04). It has no FKs, no `classification_id`, and is not in the identity rot, so there is no reason to wipe it — and doing so would needlessly endanger the SACRED `hy_spread`. The SACRED `hy_spread` series (K1) and the byte-identical sentiment-baseline fixtures (K2) are therefore safe **by construction** (we don't touch macro). A verbatim off-DB backup of `hy_spread` exists at `data/macro_hy_spread_sacred_archive/` + `s3://ste-archives/macro_hy_spread_sacred_archive/` (and the recovery source `data/hy_spread_recovery/` is preserved) as belt-and-suspenders. **NOTE (separate future arc, NOT this rebuild):** the tall+bitemporal `macro_data` design grows unbounded (revisions + LWA series sprawl); a partition/retention/series-prune arc is OQ-6 — out of scope here.
3. **Universe is REBUILT FROM SOURCE, survivorship-free** (invariants G1, G3): SEC full company list (CIK universe) ∪ FMP symbol list + FMP delisting / symbol-change history. Delisted and delisted-then-reused tickers are preserved, not dropped.
4. **Identity-FIRST build order** (audit §5; invariant A1/A4). Identity substrate (universe → issuers → ticker_classifications + ticker_history) is built and correct BEFORE any child row is loaded, so the 15 `BEFORE INSERT` triggers attribute `classification_id` correctly at load time (no retroactive re-attribution pass needed).
5. **Identity layer = ADOPT THE LOCKED DESIGN UNCHANGED — with four explicit corrective deltas, not a redesign.** The TKR-14 smart-key, `ticker_history` SCD-2, `issuers`/`issuer_securities`/`issuer_history` 3-table corp-history substrate, and the 15 SCD-2 triggers are settled by `v2.2` + corp-history v0.2 and the SHAPE is NOT redesigned here (invariants A3, I5, I6). CIK is "US issuers, NULL otherwise" — NEVER a PK. The rebuild makes **four** narrow corrective changes to identity-bearing tables (none alter the locked shape): (a) `lifetime_start` loses its `'1900-01-01'` DEFAULT and becomes `NOT NULL` with no default, forcing FPFD population at load (§3.1; the existing DEFAULT produced 100% sentinel contamination); (b) FPFD is computed from earliest `filingDate`, not `min(DocumentPeriodEndDate)` (§3.1 / A5); (c) `current_ticker` becomes the canonical mutable-symbol column and `ticker` is retained ONLY as the `ON CONFLICT` UPSERT key (§3.1 — survivor declared explicitly); and (d) **the 15 SCD-2 BEFORE INSERT triggers FIX an off-by-one** — the live trigger uses the CLOSED predicate `valid_to >= NEW.date`, which contradicts the half-open `[)` EXCLUDE constraint on `ticker_history` and invariant D2; the rebuild ships the half-open predicate `(valid_to IS NULL OR NEW.date < valid_to)` in all 15 triggers **AND in the `IdentityDispatcher` + `corp_history` resolver** (live verification: all three use the closed predicate today — §4.3), with a sentinel asserting all three == the `ticker_history` `daterange('[)')` EXCLUDE oracle at the boundary date (NOT trigger-vs-dispatcher, which would agree by mutual wrongness).
6. **Tradier / options OUT** (invariant B10). Tradier account decommissioned 2026-06-03: no adapter, no options feed. `tradier_options_chains` → DROP; `options_max_pain` → DROP (already RETIRED, no producer).
7. **Gated by the 7 moratoria** (audit §4). No DB mutation, no migration, no backfill, no validator patch, no consolidation migration until this spec is approved.
8. **`fundamentals_quarterly` PK is the THREE-part natural key `(ticker, period_end_date, filing_date)`** — not the 2-part `(ticker, period_end_date)`. The 267 logical-quarter "duplicates" the audit counted are AMENDED/restated filings: a `period_end_date` legitimately recurs with a later `filing_date` (10-Q/A, 10-K/A, restatement). A 2-part key would destroy restatement history and break point-in-time reads (an `as_of` query would see the restated number for dates before the restatement was filed). Live already carries `UNIQUE (ticker, filing_date)`. So the rebuild PRESERVES restatements and dedups only identical re-ingests via `ON CONFLICT (ticker, period_end_date, filing_date)`. PIT reader idiom (§3.2). This supersedes the v1.0 "natural PK `(ticker, period_end_date)` + dedup the dups" framing.

### 1.3 What this spec is NOT

It does not redesign identity (§4 cites the locked design). It does not write DDL, migrations, or code. It does not authorize any live run. The implementing migrations + ingest stages + reader-contract code changes are a SEPARATE plan PR (heavy-lane step 5) after operator spec-read.

---

## 2. Target table set — KEEP / MERGE / DROP / VIEW matrix

Today: **49 platform tables** (audit §2.1). Target working footprint: **~20 tables** (audit §3). One row per current-or-target table, with a one-line rationale. Column-level target schema for each KEEP table is in §3.

### 2.1 KEEP (≈20 working tables)

| Target table | Class | One-line rationale |
|---|---|---|
| `ticker_classifications` | KEEP (identity master) | TKR-14 smart-key PK; SECURITY-level identity (v2.2 §2). Rebuilt with correct FPFD + lifetime_start, not sentinel. |
| `ticker_history` | KEEP (SCD-2 ticker timeline) | Load-bearing ticker-at-row-date map (v2.2 §1.6/§1.7); rebuilt multi-row for the delisted-then-reused cohort (invariant G3). |
| `issuers` | KEEP (issuer master) | `issuer_id` surrogate PK; cik/lei nullable-unique (corp-history §3.1). Built from SEC first. |
| `issuer_history` | KEEP (issuer SCD-2) | Legal-name / reincorporation timeline (corp-history §3.3). |
| `issuer_securities` | KEEP (issuer↔security M:N SCD-2) | Share-class fan-out (GOOG/GOOGL) + merger-driven transfer (corp-history §3.2); live consumers `tpcore/corp_history/resolve_issuer_at_date`, `issuer_securities_integrity`, selfheal registry. Do NOT drop (invariant I6). |
| `prices_daily` | KEEP (substrate) | Survivorship-free OHLCV; FMP full CTA tape primary. Re-ingested identity-first so `classification_id` is attributed at load. |
| `prices_daily_staging` | KEEP (work table) | Batch-validate-before-promote staging (P3 trust-audit, `20260525_0900`). Mirrors prices_daily minus FK. |
| `fundamentals_quarterly` | KEEP (substrate; PK fix) | Quarterly PIT fundamentals. **3-part natural PK `(ticker, period_end_date, filing_date)`** — the 267 logical-quarter recurrences are amended/restated filings (distinct `filing_date`), preserved not dropped; restatement-aware dedup via `ON CONFLICT` on the 3-part key (§1.2 decision 8; §3.2; invariant D1/I2). |
| `earnings_events` | KEEP (signal) | FMP earnings beats; `(ticker, event_date)` unique. |
| `corporate_events` | KEEP (consolidation target) | Bitemporal M&A graph (corp-history §3.4). **Absorbs `ticker_lifecycle_events` only** (Form 25/15 → delisting). `corporate_actions` is NOT folded — it stays a thin standalone table for the split-adjust hot path (bitemporal/PK-shape mismatch with the M&A graph; a split has no predecessor/successor for the `event_id` SHA). See §2.2 / §5.6 / OQ-3. |
| `corporate_actions` | KEEP (split/dividend hot path) | Splits + dividends feeding the `adjusted_close` cumulative-factor model (§3.2/§5.6). Kept standalone — the M&A `corporate_events` graph is the wrong shape (no predecessor/successor for a split; bitemporal overhead unwanted on the price-adjust read path). 2-part-plus-type natural key `(ticker, action_date, action_type)`. |
| `short_interest` | KEEP (signal) | FINRA bi-monthly; `release_date` is the PIT boundary (invariant B7). |
| `borrow_rates` | KEEP (signal) | IBorrowDesk daily borrow %; scrape-fragility circuit-breaker (invariant B8/H1). |
| `insider_transactions` | KEEP (signal) | SEC Form 4 BUY/SELL; CHECK physical-truth predicates (invariant D5/E2). |
| `insider_sentiment` | KEEP (signal) | Finnhub free-tier MSPR; premium endpoints excluded (invariant B6). |
| `social_sentiment` | KEEP (signal) | ApeWisdom Reddit mentions; T1/T2 universe. |
| `sec_material_events` | KEEP (signal) | 8-K item-code events; one row per item. |
| `spread_observations` | KEEP (derived input) | Corwin-Schultz spread estimates feeding `liquidity_tiers`. |
| `macro_data` | **OUT OF SCOPE — preserve in place, UNTOUCHED** | No FKs, no `classification_id`, not in the identity rot (operator 2026-06-04). Holds FRED (incl. SACRED `hy_spread`) + sentiment + non-trading LWA series; shim VIEWs ride on it unchanged. The rebuild does NOT wipe or re-ingest it. Tall+bitemporal unbounded-growth is a separate future arc (OQ-6). |
| `liquidity_tiers` | KEEP (derived) | Per-ticker tier 1-5 from spread_observations; cost-model lookup. |
| `universe_candidates` | KEEP (derived) | Per-engine daily pre-screen; engine universe scope. |
| `risk_state` | KEEP (ops) | Per-engine risk-governor state; kill-switch (invariant — never delete rows). |
| `risk_close_ledger` | KEEP (ops) | Close-out ledger (`20260519_0000`). |
| `open_orders` | KEEP (ops) | Working live-order state (`20260512_0000`). |
| `allocations` | KEEP (ops) | Allocator-decision audit trail. |
| `aar_events` | KEEP (ops) | After-action reports per trade; jsonb payload. |
| `aar_deferred` | KEEP (ops) | Deferred-AAR queue (`20260522_0000`). |
| `daemon_heartbeats` | KEEP (ops) | Daemon liveness (`20260515_0000`). |
| `ingest_manifest` | KEEP (ops/log) | Per-batch ingest reconciliation (source vs DB counts) (`20260525_0200`). |
| `application_log` | KEEP (ops/log) | Rolling 7-day audit trail; the `DATA_OPERATIONS_COMPLETE` emission stream. |
| `data_quality_log` | KEEP (REDESIGN + consolidation target) | Durable detector substrate, **redesigned** (jsonb `notes` + per-`kind` partial indexes; §3.3 — the live table is a single-purpose freshness-metric log, not this). **Absorbs** `fundamentals_period_source_evidence` + `failed_alpha_ledger` + `parity_drift_log` + `forensics_triggers` + `ingest_quarantine` + `execution_quality_log` via a `kind` discriminator (audit §3; invariant I1). `ingestion_metrics` fold is RECONSIDERED (may belong on `ingest_manifest` — §2.2/§3.3). |

> Note: the table count lands at ~20 *working domain* tables once the consolidation absorptions in `corporate_events`, `macro_data`, and `data_quality_log` collapse the today-49 footprint. Ops/log tables (risk/orders/aar/heartbeats/manifest/application_log) are counted toward the working footprint but are operational, not domain-substrate.

### 2.2 MERGE (folded into a KEEP consolidation target; no standalone table in the rebuild)

| Today table | Merges into | Rationale (audit §3) |
|---|---|---|
| `ticker_lifecycle_events` | `corporate_events` | Form 25 / Form 15 delisting events fit `event_kind IN ('delisting','bankruptcy_*')`. (This is the ONLY fold into `corporate_events`; `corporate_actions` stays standalone — see §2.1 / §5.6 / OQ-3.) |
| `fundamentals_period_source_evidence` | `data_quality_log` (`kind='confirmed_data_gap_evidence'`) | 506 polluted rows; semantic is a quality-evidence log, not a sidecar (audit §2.12; invariant E5). Reset + rebuild clean. |
| `failed_alpha_ledger` | `data_quality_log` (`kind='failed_alpha'`) | 5 rows; one-shot use; a quality-log row. |
| `parity_drift_log` | `data_quality_log` (`kind='parity_drift'`) | Paper-vs-live drift is a quality observation; folds in. |
| `forensics_triggers` | `data_quality_log` (`kind='forensics_trigger'`) | Detection events with fingerprint/dossier in the jsonb payload. |
| `ingest_quarantine` | `data_quality_log` (`kind='ingest_quarantine'`) | Rejected-record retention is a quality-log concern; payload + error in jsonb. |
| `ingestion_metrics` | `data_quality_log` (`kind='ingestion_metrics'`) — RECONSIDER | Per-run ingest metrics. **Open in the plan PR (§3.3 note):** high-churn per-run metrics may belong on `ingest_manifest` (the per-batch reconciliation row) rather than in the detector substrate. Fold only if the metric is a quality observation, not a reconciliation count. |
| `aaii_sentiment` | `macro_data` (`source='aaii'`) + `aaii_sentiment_v` VIEW | Already consolidated by Task #18 (`20260524_0900`); the wide table is a shim VIEW over `macro_data`. |
| `macro_indicators` | `macro_data` (`source='fred'`) + `macro_indicators_v` VIEW | Same Task #18 consolidation. |
| `fear_greed` | `macro_data` (`source='cnn_fear_greed'`) + `fear_greed_v` VIEW | Same Task #18 consolidation. |
| `execution_quality_log` | `data_quality_log` (`kind='execution_quality'`) | Fill-quality is a quality-log concern (already partly co-located per DATABASE_AND_DATAFLOW §2). |

### 2.3 DROP (empty / dead / out-of-scope; no table in the rebuild)

| Today table | Why dropped |
|---|---|
| `fundamentals_quarterly_archive` | Empty speculative; cleanup arc paused (audit §2.12). Status semantics fold into FQ via a `status` enum if ever needed. |
| `fundamentals_quarterly_quarantine` | Same — empty speculative. |
| `options_max_pain` | RETIRED 2026-06-01; no producer; options out (invariant B10). |
| `tradier_options_chains` | Tradier CLOSED 2026-06-03; no adapter, no options (invariant B10). |
| `provider_binding_state` | Provider-lifecycle state belongs in `tpcore/providers.py` ProviderBinding SoT (DFCR), not a DB table; dead/redundant. |
| `ingestion_jobs` | Already dropped (`20260524_1800`); Railway registry not used in local-active mode; stage registry is `scripts/ops.py`. |

> **`split_pre_image_log` — RETAINED (drop deferred).** v1.0 listed this pre-image-of-split audit sidecar for DROP on the theory that clean re-ingest re-applies splits idempotently from a consolidated events table and in-place price mutation goes away. v1.1 keeps `split_pre_image_log` **until the §3.2/§5.6 `adjusted_close` cumulative-factor model lands and replaces the in-place split mutation.** The factor model leaves raw `close` untouched (no pre-image to log), so the sidecar becomes genuinely dead only once that ships. Conditional drop = a follow-up migration in the plan PR after the factor-applier is in place; not in the schema-cutover phase. (KEEP-class for the rebuild's initial cutover; documented here rather than in §2.1 because it is transient.)

### 2.4 VIEWS (not physical tables)

| View | Over | Purpose |
|---|---|---|
| `macro_indicators_v` → `macro_indicators` (post-cutover) | `macro_data WHERE source='fred'` | Back-compat tall macro read (`20260524_0900`). |
| `aaii_sentiment_v` → `aaii_sentiment` | `macro_data WHERE source='aaii'` (pivot) | Back-compat wide AAII read. |
| `fear_greed_v` → `fear_greed` | `macro_data WHERE source='cnn_fear_greed'` (pivot) | Back-compat wide F&G read. |
| `series_catalog` | `macro_data` distinct (source, series_id) | Series discovery (Task #18 `20260524_1100`). |
| `*_count_snapshot` (e.g. `earnings_events_count_snapshot`, `sec_insider_row_counts_snapshot`, `ticker_classifications_source_count`) | parent table aggregates | Monotone count snapshots become VIEWs, not materialized tables (audit §3 VIEW_CANDIDATE class). |

---

## 3. Column-level target schema

Column-level for each KEEP table. The identity spine (§3.1) adopts the **shape** of the locked design unchanged, with the four narrow corrective deltas of §1.2 decision 5 (lifetime_start default removed; FPFD = filingDate; current_ticker canonical; trigger off-by-one fixed). Substrate / signal / ops columns are normalized from the live migrations cited. The column-level *changes* the rebuild makes are: (a) `fundamentals_quarterly` 3-part natural PK `(ticker, period_end_date, filing_date)`; (b) `prices_daily.source` enum reflecting FMP-primary / no-Tradier; (c) `prices_daily.adjusted_close` respec'd as a cumulative split+dividend factor over raw `close` (§3.2); (d) the consolidation discriminators on `corporate_events` / `data_quality_log` / `macro_data`; (e) `data_quality_log` redesigned (jsonb `notes` + per-`kind` partial indexes — §3.3), which is a redesign of the live table, not a transcription.

### 3.1 Identity spine (ADOPT LOCKED — v2.2 §2 + corp-history §3)

#### `ticker_classifications` — identity master (SECURITY level)

PK is the immutable 14-char TKR-14 smart-key (invariant A3; v2.2 §1.1–1.5). `cik`/`figi`/`cusip`/`isin` are UNIQUE-NULLABLE companions. CIK is "US issuers, NULL otherwise" — NEVER a PK.

> **`ticker` vs `current_ticker` (survivor declared).** The live table carries BOTH columns: `ticker` (NOT NULL, UNIQUE via `ticker_classifications_ticker_key`) AND `current_ticker`. The 2026-05-24 PK swap (`20260524_1400`) deliberately kept `ticker` because three UPSERT callers infer `ON CONFLICT (ticker)` against it: `parent_resolver_backfill`, `classify_tickers`, `_tkr14_backfill_fmp_profile`. **Rebuild decision: `current_ticker` is the canonical mutable-symbol column** (it is the one §3.1 documents below, with the partial-UNIQUE on active status; ticker-at-row-date always lives in `ticker_history`). The legacy `ticker` column is the migration risk: if the rebuild DROPS `ticker`, those three UPSERT callers must be repointed to `ON CONFLICT (current_ticker) WHERE status IN (...)` and audited in the plan PR (a `validator_or_gate_change` / `ingestion_or_backfill_change` per the discovery-first CIC gate). If `ticker` is RETAINED as a redundant UPSERT key, that must be stated as a deliberate carry-over, not left ambiguous. **The plan PR MUST pick one and repoint/justify accordingly — this is not left open here: the recommendation is to drop `ticker` and repoint the three callers to `current_ticker`, since a duplicated mutable-symbol column is exactly the kind of drift the rebuild is removing.**

- `id` (text, **PK**): TKR-14 smart-key. `CHECK (id ~ '^[A-Z]{2}[SPEFRTAUWN][NQABOXZ][0-9]{2}[FSAO][0-9A-HJ-KM-NP-TV-Z]{5}[0-9]{2}$')` (v2.2 §1.2). ISO 7064 Mod-97-10 check digits (invariant L3); ISO 3166-1 α-2 country segment (L2); Crockford base32 issuer-hash (L4).
- `figi` (char(12), NULL): OpenFIGI compositeFIGI; UNIQUE WHERE NOT NULL.
- `cusip` (char(9), NULL): UNIQUE WHERE NOT NULL.
- `isin` (char(12), NULL): UNIQUE WHERE NOT NULL.
- `cik` (text, NULL): SEC CIK for US issuers; UNIQUE WHERE NOT NULL. **NEVER a PK** (invariant A3; v2.2 §1.1).
- `current_ticker` (text, NOT NULL): current symbol; UNIQUE WHERE `status IN ('active','active_when_issued')`. Mutable — ticker-at-row-date lives in `ticker_history`.
- `current_exchange` (text, NULL): mutable; indexed.
- `current_legal_name` (text, NULL).
- `gics_sector` (text, NULL): mutable reclassification.
- `status` (text, NOT NULL): lifecycle state.
- `country` (char(2), NOT NULL): ISO 3166-1 α-2 (invariant L2).
- `asset_class` (text, NOT NULL): `stock`/`etf`/`spac`/`fund`/preferred/REIT/etc. Mirrors TKR-14 pos-3 for joinless access.
- `instrument_subtype` (text, NULL): finer subtype (units/warrants/ADR/leveraged-inverse). Drives the universe U/W-suffix + SPAC-unit filter (audit §2.10/§5.10). Note: **SPAC unit-separation** (a unit `XYZU` separating into common `XYZ` + warrant `XYZW`) is an unmodeled `corporate_events`-kind today; any future SPAC-aware reader that needs to link a unit to its post-separation common/warrant legs would consume a new `event_kind` (e.g. `unit_separation`) on `corporate_events` — flagged here so it is not silently assumed handled.
- `etf_inverse` (boolean, NULL): true for inverse ETFs; NULL for non-ETF.
- `etf_leverage` (numeric, NULL): leverage factor; NULL for non-ETF / 1x.
- `etf_category` (text, NULL): basket family for the sentinel engine.
- `ipo_venue` (text, NULL): listing venue at IPO (snapshot; mirrors TKR-14 pos-4).
- `discovery_source` (text, NOT NULL): `FMP`/`SEC`/`Alpaca`/`other` (snapshot; mirrors TKR-14 pos-7).
- `lifetime_start` (date, **NOT NULL, NO DEFAULT**): **backfilled from SEC FPFD (or earliest `ticker_history.valid_from`); the `1900-01-01` sentinel is FORBIDDEN** (invariant A6; audit §2.3). The live column is `DATE NOT NULL DEFAULT '1900-01-01'` (`20260524_1700`) — that DEFAULT is exactly what produced 100% sentinel contamination, because any row inserted without an explicit `lifetime_start` silently got the sentinel. **The rebuild DROPS the DEFAULT clause** and declares `lifetime_start date NOT NULL` with no default, so a load that fails to populate FPFD errors out at insert instead of silently sentineling. `CHECK (lifetime_end IS NULL OR lifetime_end > lifetime_start)` (`20260524_1700`).
- `lifetime_end` (date, NULL): NULL = active.
- `first_public_filing_date` (date, NULL): **FPFD = earliest `filingDate` across the issuer's FULL EDGAR submission index** (`submissions.zip`, full-history pagination — NOT the recent ~1000-filing shard, NOT FMP ipoDate) (invariant A5; audit §2.3; `20260530_0200`). This corrects the v1.0 / live `20260530_0200` formula of `min(dei:DocumentPeriodEndDate)`, which is the earliest fiscal-PERIOD-END (the period a filing reports on), not the earliest filing — using it reintroduces look-ahead (a Q1-FY2010 10-K filed in 2010 reports a period ending 2009) and ignores the issuer's first non-periodic public filings (S-1 / 424B / 8-A), which are the true "first public filing". The implementing fix in `tpcore/.../companyfacts_adapter.py` (stop using `min(reportDate)`; read the submission-index `filingDate` min) is plan-PR work, scoped via the discovery-first CIC gate as an `ingestion_or_backfill_change`. Kills the LMT-2016 / PEP-2015 decade-shift.
- `first_periodic_report_date` (date, NULL): **separate, NOT conflated with FPFD** — the earliest `period_end_date` for which standardized periodic fundamentals (10-Q/10-K/20-F/40-F/6-K) are available, i.e. the "first periodic-fundamentals boundary" the v1.0 `min(DocumentPeriodEndDate)` formula was actually computing. Validators that want a fundamentals-availability floor (D6 completeness clamp) read THIS, not FPFD; the price-completeness floor reads FPFD / `lifetime_start`. Keeping them distinct is what prevents the look-ahead conflation.
- `sec_document_type_primary` (text, NULL): dispositive issuer-class signal (10-Q/10-K/20-F/40-F/6-K) from SEC EDGAR DEI histogram (`20260530_0200`). Drives identity-aware fundamentals routing (invariant D6).
- `sec_document_type_history` (jsonb, NULL): full DocumentType histogram.
- `fiscal_year_end_month` (smallint, NULL): `CHECK 1..12`; from `dei:CurrentFiscalYearEndDate`.
- `last_filing_date` (date, NULL): corroborates delisting via filing cessation.
- `metadata_source` (text, NULL): `CHECK IN ('sec_companyfacts','sec_submissions','manual','fmp_profile')`.
- `metadata_updated_at` (timestamptz, NULL).
- `cik_source` (text, NULL): `CHECK IN ('sec_ticker_map','fmp','manual')` — CIK provenance.
- `created_at`, `updated_at` (timestamptz, NOT NULL DEFAULT now()).
- Expression indexes on `substring(id,…)` for the TKR-14 filter cohorts (v2.2 §2); partial UNIQUE on `current_ticker WHERE status IN (...)`; lifetime indexes (`20260524_1701`).

#### `ticker_history` — SCD-2 ticker-at-row-date map (v2.2 §1.7)

- `classification_id` (text, NOT NULL, FK → `ticker_classifications.id`).
- `ticker` (text, NOT NULL): the symbol valid over `[valid_from, valid_to)`.
- `valid_from` (date, NOT NULL).
- `valid_to` (date, NULL): NULL = current.
- **PK `(classification_id, valid_from)`.**
- `CONSTRAINT no_overlap EXCLUDE USING gist (classification_id WITH =, daterange(valid_from, COALESCE(valid_to,'infinity'::date),'[)') WITH &&)` (the single EXCLUDE constraint, audit §2.1).
- Indexes: `(ticker) WHERE valid_to IS NULL` (active lookup); ticker_history lookup index (`20260527_0300`).
- **Rebuilt multi-row** for the delisted-then-reused cohort + predecessor classifications (invariant A4/G3; audit §2.4/§5 step 5). Canonical predicate `valid_from <= d < COALESCE(valid_to,∞)` (invariant D2).
- **Reused-ticker boundary source.** For the delisted-then-REUSED cohort (a ticker freed by one CIK and later assigned to a different CIK), the boundary between the predecessor window and the successor window is sourced from **SEC Form 25** (the predecessor's exchange-delisting filing — the `valid_to` of the old window) + the successor's FPFD (the `valid_from` of the new window). It is NOT sourced from FMP `/symbol-change`, which only covers RENAMES (same entity, new symbol), not REUSE across distinct CIKs — relying on it would silently merge two entities' histories. FMP `/symbol-change` remains valid for the same-entity rename case (one classification, new `current_ticker`).

#### `issuers` — issuer master (corp-history §3.1)

- `issuer_id` (text, **PK**): operator-minted surrogate (stable across CIK change + take-private).
- `cik` (text, NULL): UNIQUE.
- `lei` (char(20), NULL): UNIQUE.
  > Harmonization note: `issuers.cik`/`lei` are plain `UNIQUE` (corp-history §3.1), whereas `ticker_classifications.cik`/`figi`/`cusip`/`isin` are `UNIQUE WHERE NOT NULL` (partial). On PG15 a plain `UNIQUE` treats NULLs as distinct by default (so multiple NULL CIKs are allowed — the intended behavior for non-US issuers), which makes the two shapes behaviorally equivalent for the "many NULLs allowed, non-NULLs unique" case. The rebuild should either (a) harmonize `issuers` to the explicit `UNIQUE WHERE NOT NULL` partial form for consistency, or (b) document the plain-UNIQUE-with-default-NULLS-DISTINCT reliance explicitly. Recommendation: harmonize to the partial form (intent is unambiguous and matches `ticker_classifications`). Avoid `NULLS NOT DISTINCT` (PG15+) here — it would forbid multiple NULL CIKs, breaking non-US issuers.
- `legal_name` (text, NOT NULL).
- `country_of_incorp` (char(2), NULL): ISO 3166-1 α-2.
- `fiscal_year_end_month` (smallint, NULL): issuer-level FYE (corp-history-aligned issuer metadata).
- `sec_document_type_primary` (text, NULL): issuer SEC doc-type.
- `first_public_filing_date` (date, NULL): issuer FPFD; validators clamp completeness to this (invariant F6).
- `status` (text, NOT NULL DEFAULT 'active'): `CHECK IN ('active','dissolved','merged','private')`.
- lifecycle-state column(s) per `20260530_0300` issuer-lifecycle-evidence foundation.
- `created_at`, `updated_at` (timestamptz, NOT NULL DEFAULT now()).

#### `issuer_securities` — issuer↔security M:N SCD-2 (corp-history §3.2; KEEP, invariant I6)

- `issuer_id` (text, NOT NULL, FK → `issuers.issuer_id`).
- `classification_id` (text, NOT NULL, FK → `ticker_classifications.id`).
- `share_class` (text, NULL): `A`/`B`/`C`/NULL.
- `valid_from` (date, NOT NULL).
- `valid_to` (date, NULL): NULL = current.
- `notes` (text, NULL).
- **PK `(issuer_id, classification_id, valid_from)`.**
- Index `(classification_id, valid_from)`.
- Live consumers (do NOT drop): `tpcore/corp_history/resolve_issuer_at_date`, `issuer_securities_integrity` validator, selfheal registry.

#### `issuer_history` — issuer SCD-2 legal-name/CIK (corp-history §3.3)

- `issuer_id` (text, NOT NULL, FK → `issuers.issuer_id`).
- `cik` (text, NULL): CIK at that point (re-domiciliation).
- `legal_name` (text, NOT NULL): name at that point.
- `valid_from` (date, NOT NULL).
- `valid_to` (date, NULL).
- `source` (text, NOT NULL).
- `recorded_at` (timestamptz, NOT NULL DEFAULT now()).
- **PK `(issuer_id, valid_from)`.** Single-timeline SCD-2 (not bitemporal).

#### 15 SCD-2 `BEFORE INSERT` triggers (write side — ADOPT, `20260524_1500`)

INSERT-only triggers auto-assign `classification_id` by looking up `ticker_history` at the row's date column (audit §2.8). Per-table date column: `prices_daily.date`, `fundamentals_quarterly.period_end_date`, `earnings_events.event_date`, `corporate_actions.action_date`, `insider_transactions.filing_date`, `sec_material_events.filing_date`, `short_interest.release_date` (the PIT boundary — see below), `borrow_rates.date`, `liquidity_tiers.last_updated::date`, `insider_sentiment` (`make_date(year,month,1)`), `social_sentiment.date`, `spread_observations.observed_at::date`, `universe_candidates.as_of_date`, `aar_events`. Trigger ORDER BY for ticker-reuse (`20260524_1901`). **Because the rebuild is identity-first, these fire correctly at load time** — no retroactive re-attribution UPDATE pass is needed (contrast audit §5 step 6, which was the preserve-in-place path).

> **Trigger off-by-one FIX (deliberate change — §1.2 decision 5d).** The LIVE trigger functions (`20260524_1500`) look up `ticker_history` with the CLOSED predicate `valid_from <= as_of AND (valid_to IS NULL OR valid_to >= as_of)`. That is wrong: `ticker_history`'s `no_overlap` EXCLUDE constraint uses a half-open `daterange(valid_from, …, '[)')` and invariant D2's canonical predicate is `valid_from <= d < COALESCE(valid_to,∞)`. On the exact boundary date `d = valid_to` of a delisted-then-reused pair, the CLOSED trigger predicate matches BOTH the predecessor (via `valid_to >= d`) and the successor (via `valid_from <= d`) and the `LIMIT 1` picks arbitrarily — a silent mis-attribution at the seam. **The rebuild ships the half-open predicate `valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)`** in all 15 trigger functions **AND in the read layer — `IdentityDispatcher` (`dispatcher.py:68-69`) and the `corp_history` resolver (`__init__.py:42-43,53-55`), which live verification showed ALSO use the closed predicate today.** All three must change together; "matching the IdentityDispatcher implementation" was a v1.1 error (the dispatcher is closed too). The sentinel asserts trigger/dispatcher/resolver output == the **expected value from the `ticker_history` `daterange(valid_from, valid_to, '[)')` EXCLUDE oracle** on a synthetic reuse pair evaluated AT the boundary date — NOT trigger-vs-dispatcher (which would pass by mutual wrongness). This is delta (d) of the identity-layer corrective deltas in §1.2 decision 5.

> **`short_interest` trigger pins to `release_date`, not `settlement_date`.** The live trigger (`20260524_1500`) keys the `short_interest` lookup on `NEW.settlement_date`. Per invariant B7, `release_date` (FINRA dissemination, ~settlement + 8-9 NYSE sessions) is the PIT boundary; both the trigger's as-of lookup AND all readers must use `release_date`. The rebuild changes the trigger as-of expression for `short_interest` to `NEW.release_date`. (The §3.3 schema also pins readers + the `CHECK release_date >= settlement_date` to `release_date`.)

> **Non-classification triggers in the inventory (do not lose these).** Beyond the 15 classification-assign triggers, `spread_observations` carries an AFTER-INSERT **30-day rolling-retention trigger** (`spread_observations_retention_trg` → `platform._spread_observations_retention()`, `20260512_2100`) that `DELETE`s rows older than 30 days on every insert (same shape as `application_log`'s housekeeping). It is housekeeping, not identity, but the rebuild must recreate it — listed here + in §3.3 so the implementer does not silently drop it during the clean-schema rebuild.

### 3.2 Substrate

#### `prices_daily` — daily OHLCV (survivorship-free)

- `ticker` (text): symbol-at-row-date (NOT current_ticker — invariant A4/v2.2 §1.6).
- `date` (date): trading day.
- `classification_id` (text, FK → `ticker_classifications.id`): auto-assigned by trigger; FK NOT VALID → VALIDATE post-load (invariant I3).
- `open`, `high`, `low`, `close` (numeric): `CHECK close>0, high>=low` (invariant F5). **`close` stays RAW (unadjusted) — PIT-honest.** Price-level logic (the actual traded price on `date`, gap detection, circuit-breaker reference) reads raw `close`. The rebuild does NOT mutate raw OHLC in place for splits/dividends (the v1.0 / live in-place split-mutation behavior is replaced — see `adjusted_close` below).
- `volume` (bigint).
- `adjusted_close` (numeric): **total-return-adjusted = `close × cumulative_adjustment_factor(date)`**, where `cumulative_adjustment_factor(date)` is the running product over all split + dividend events on `corporate_actions` with `action_date` STRICTLY AFTER `date`. Per event: a split contributes its split factor; a cash dividend contributes `(1 − div / ex_date_raw_close)`. This model (invariant D4):
  - is **idempotent by construction** — `adjusted_close` is a deterministic function of raw `close` + the (immutable, append-only) `corporate_actions` rows, so re-ingest recomputes the identical value and never compounds a prior adjustment (the v1.0 split-only ratio heuristic mutated raw `close` in place, so a re-ingest could double-apply or partially-apply);
  - **handles dividends** (the ratio heuristic could not) and **reverse splits** (factor < 1; the heuristic assumed factor > 1);
  - is **commutative for same-ex-date split+dividend** because both contribute multiplicative factors computed against the same ex-date raw close — order-independent;
  - replaces the dropped-then-restored `split_pre_image_log` need: with raw `close` never mutated, there is no pre-image to retain once this lands (§2.3 note).
  The factor-applier reading `corporate_actions` is the OQ-3 / §5.6 plan-PR code change. Until it lands, `split_pre_image_log` is retained (§2.3).
- `delisted` (boolean, DEFAULT false): PIT-safe filter only (invariant D3/G2).
- `delisting_date` (date, NULL).
- `source` (text): `'fmp'` primary; `'alpaca'` (IEX/SIP) fallback/diagnostics only. **NO `'tradier'`** (invariant B1/B10).
- `recorded_at` (timestamptz, DEFAULT now()).
- **PK `(ticker, date)`** (post-dedup-promote, `20260525_0100`). Indexes: `(date)`; active-universe index (`20260527_0400`).

#### `prices_daily_staging` — batch-validate work table (`20260525_0900`)

- `staging_run_id` (UUID, NOT NULL): usually the `ingest_manifest_id`.
- `ticker` (TEXT), `date` (DATE), `open`/`high`/`low`/`close` (NUMERIC), `volume` (BIGINT), `adjusted_close` (NUMERIC), `delisted` (BOOLEAN), `delisting_date` (DATE), `source` (TEXT).
- `staged_at` (TIMESTAMPTZ, NOT NULL DEFAULT now()), `promoted` (BOOLEAN, NOT NULL DEFAULT false).
- **PK `(staging_run_id, ticker, date)`.** No FK on classification_id (relationship-free by design; FK applied at production merge). Partial indexes WHERE `promoted=false`.

#### `fundamentals_quarterly` — quarterly PIT fundamentals (**3-part natural PK**)

- `ticker` (text): symbol-at-period.
- `period_end_date` (date): fiscal quarter end.
- `filing_date` (date): SEC filing date — the PIT boundary (`WHERE filing_date <= as_of`, invariant D1).
- `classification_id` (text, FK): trigger-assigned on `period_end_date`.
- `revenue`, `net_income`, `fcf` (numeric).
- `total_assets`, `total_liabilities` (numeric).
- `current_assets`, `current_liabilities` (numeric).
- `shares_outstanding` (numeric): PIT shares for derived ratios (invariant E4).
- `pb` (numeric, NULL): computed price-to-book.
- `de` (numeric, NULL): computed debt-to-equity.
- `source` (text): originating feed (invariant E3).
- `recorded_at` (timestamptz, DEFAULT now()).
- **PK `(ticker, period_end_date, filing_date)`** — the change from the surrogate `id BIGINT` PK + `UNIQUE (ticker, filing_date)` the live table carries (`20260510_0049`). The 267 logical-quarter recurrences are NOT duplicates to discard — they are amended/restated filings: the same `period_end_date` re-filed with a later `filing_date` (10-Q/A, 10-K/A, restatement). A 2-part `(ticker, period_end_date)` PK would collapse them and destroy restatement history, breaking PIT (an `as_of` between the original and the amendment must see the ORIGINAL number). The 3-part key preserves every restatement; **dedup is restatement-aware: `ON CONFLICT (ticker, period_end_date, filing_date) DO …`** drops only an identical re-ingest of the same filing (audit §2.7; invariant D1/I2; §1.2 decision 8). Live already enforces `UNIQUE (ticker, filing_date)`, which the rebuild keeps as a secondary uniqueness guard.
- **PIT reader idiom** (D1): the latest-known-as-of read selects one row per logical quarter —
  ```sql
  SELECT DISTINCT ON (ticker, period_end_date) *
  FROM platform.fundamentals_quarterly
  WHERE ticker = :ticker AND filing_date <= :as_of
  ORDER BY ticker, period_end_date, filing_date DESC
  ```
  i.e. the most recently FILED revision known at `as_of` for each period — which is exactly the restatement-correct PIT answer.
- Degenerate FMP rows (`total_assets=0`, `total_liabilities<0`) rejected at CSV validation. Completeness routes by `sec_document_type_primary` + per-form MAX_GAP (invariant D6); foreign-filer routing (20-F/40-F annual-only, 6-K interim) per §6.

### 3.3 Signals

#### `earnings_events`
- `ticker` (text), `event_date` (date), `event_type` (text — `'EARNINGS_BEAT'`), `magnitude_pct` (numeric), `classification_id` (FK), `recorded_at` (timestamptz). Unique `(ticker, event_date)`.

#### `corporate_events` — bitemporal M&A graph + consolidation target (corp-history §3.4)
- `event_id` (text): minted `SHA-256(predecessor‖successor‖event_date‖event_kind)`.
- `event_kind` (text, NOT NULL): `CHECK IN` the 19-kind taxonomy (corp-history §3.5 + `fdic_receivership`), **extended to absorb `ticker_lifecycle_events`** (Form 25/15 → `delisting`/`bankruptcy_*`). **NOT extended for `corporate_actions`** — splits/dividends stay on the standalone `corporate_actions` table (§2.1/§2.2/§5.6); a split has no predecessor/successor for the `event_id` SHA and does not belong in the M&A graph. (A future `unit_separation` SPAC kind would live here — §3.1.)
- `event_date` (date, NOT NULL): effective date. `announced_date` (date, NULL).
- `predecessor_cls_id`, `successor_cls_id` (text, NULL, FK → `ticker_classifications.id`).
- `predecessor_issuer_id`, `successor_issuer_id` (text, NULL, FK → `issuers.issuer_id`): denormalized for graph walks.
- `successor_external` (text, NULL): free-text when successor isn't tracked.
- `ratio_num`, `ratio_den`, `cash_per_share` (numeric, NULL): for M&A exchange-ratio / cash-merger terms (NOT split ratios — those stay on `corporate_actions`).
- `extra_terms` (jsonb, NULL): CVRs / elections.
- `source` (text, NOT NULL), `source_filing_url` (text, NULL), `notes` (text, NULL).
- `realtime_start` (timestamptz, NOT NULL DEFAULT now()), `realtime_end` (timestamptz, NOT NULL DEFAULT 'infinity'), `recorded_at` (timestamptz).
- **PK `(event_id, realtime_start)`** (bitemporal). Partial indexes WHERE `realtime_end='infinity'` on predecessor/successor/issuer/date.

#### `short_interest`
- `ticker` (text), `settlement_date` (date), `release_date` (date, **the PIT boundary** — both the SCD-2 classification-assign trigger's as-of lookup AND every reader key on `release_date`, NOT `settlement_date`; `CHECK release_date >= settlement_date`, invariant B7), `short_interest_pct` (numeric, NULL — derived from PIT shares, NULL when absent, invariant E4), `days_to_cover` (numeric, NULL), `classification_id` (FK), `recorded_at`. PK `(ticker, settlement_date)`.
- **Lag-reasonableness validator (new, beyond the CHECK).** The `release_date >= settlement_date` CHECK only guards direction. FINRA dissemination is ~8-9 NYSE sessions after settlement; a validator should additionally flag rows whose `release_date − settlement_date` is OUTSIDE a 5–20-session band as a quality observation (`data_quality_log kind='validation'`) — catches a feed that mis-fills `release_date` (e.g. copies `settlement_date`, reintroducing silent look-ahead). Lower bound 5 sessions, upper bound 20 sessions; flag-only, not a hard reject. Plan-PR validator.

#### `borrow_rates`
- `ticker` (text), `date` (date), `borrow_rate_pct` (numeric, `CHECK >= 0 AND date <= today+1`), `classification_id` (FK), `recorded_at`. PK `(ticker, date)`. Scrape circuit-breaker contract (invariant B8/H1).

#### `insider_transactions`
- `ticker` (text), `filing_date` (date), `insider_name` (text), `raw_transaction_code` (text, NOT NULL — the SEC Form 4 Table I transaction code: `P`/`S`/`A`/`M`/`F`/`G`/…), `transaction_type` (text, `CHECK IN ('BUY','SELL')`), `shares` (bigint, `CHECK >0`), `price` (numeric(18,4), `CHECK >=0`), `value` (numeric(20,2), `CHECK >=0`), `source` (text, `CHECK IN ('sec','fmp')`, invariant A7), `classification_id` (FK), `recorded_at`. PK `(ticker, filing_date, insider_name, transaction_type, shares)` (live composite, `20260525_0100`). CSV-write layer applies the same CHECK predicates (invariant E2).
- **BUY/SELL = open-market `P`/`S` ONLY.** Form 4 Table I codes are NOT all economically equivalent: `P` = open-market purchase → `BUY`; `S` = open-market sale → `SELL`; but `A` (award/grant), `M` (option/derivative exercise → acquisition), `F` (shares withheld for tax) are NOT open-market signals and MUST be excluded from the `transaction_type` mapping — counting them as BUY/SELL pollutes every insider-flow signal. **Decision: the rebuild KEEPS the raw code (`raw_transaction_code` column above) AND drops non-`P`/`S` codes at the CSV-validation layer** (E2) before they reach `transaction_type` — so `transaction_type` only ever holds `P`→BUY / `S`→SELL, while the raw code is retained for audit / future signal work (e.g. an exercise-then-sell pattern detector). Plan-PR work for the CSV validator + the SEC Form 4 parser.

#### `insider_sentiment`
- `symbol` (text), `year` (int), `month` (int), MSPR (numeric [-100,100]), net insider share change (numeric), `classification_id` (FK), `recorded_at`. PK `(symbol, year, month)`. Finnhub free-tier only (invariant B6).

#### `social_sentiment`
- `ticker` (text), `date` (date), mentions / upvotes / rank + 24h-ago comparators (numeric/int), `classification_id` (FK), `recorded_at`. PK `(ticker, date)`. ApeWisdom; T1/T2 filter.

#### `sec_material_events`
- `ticker` (text), `filing_date` (date), `event_type` (text — 8-K item code, `CHECK length>0`), `summary` (text, NULL), `classification_id` (FK), `recorded_at`. Unique `(ticker, filing_date, event_type)`.

#### `spread_observations`
- `id` (bigserial, PK), `ticker` (text), `source` (text — `'corwin_schultz'`), `spread_pct` (numeric), `n_observations` (int), `observed_at` (timestamptz), `classification_id` (FK).

#### `macro_data` — bitemporal tall macro (consolidation target; SACRED carve-out)
- `source` (text, NOT NULL): `'fred'` / `'aaii'` / `'cnn_fear_greed'`.
- `series_id` (text, NOT NULL): within-provider series. **`hy_spread` (FRED `BAMLH0A0HYM2`) is SACRED — copy verbatim, never re-pull** (invariant K1).
- `observed_date` (date, NOT NULL).
- `value_num` (numeric, NULL), `value_text` (text, NULL): `CHECK` XOR (exactly one non-null).
- `realtime_start` (timestamptz, NOT NULL DEFAULT now()), `realtime_end` (timestamptz, NOT NULL DEFAULT 'infinity'), `recorded_at` (timestamptz).
- **PK `(source, series_id, observed_date, realtime_start)`.** GIST PIT index; partial latest/source indexes WHERE `realtime_end='infinity'`.

#### `liquidity_tiers`
- `ticker` (text, PK), `tier` (smallint 1-5), `median_spread_pct` (numeric), `p95_spread_pct` (numeric), `observations` (int), `provisional` (boolean DEFAULT false), `classification_id` (FK), `last_updated` (timestamptz).

#### `universe_candidates`
- `as_of_date` (date), `engine` (text), `ticker` (text), `tier` (smallint, NULL), `last_close` (numeric(18,6), NULL), `reason` (text, NULL), `classification_id` (FK), `created_at` (timestamptz). PK `(as_of_date, engine, ticker)`; index `(engine, as_of_date)`. Denominator = engine universe (invariant — coverage-gate denominator matches engine universe). U/W-suffix + SPAC-unit filtered via `instrument_subtype` (audit §2.10/§5.10).

### 3.4 Ops / logs

#### `risk_state`
- `engine` (text, PK), `daily_pnl` (numeric), `weekly_pnl` (numeric), `kill_switch_active` (boolean DEFAULT false), `kill_switch_reason` (text, NULL), `open_positions` (int DEFAULT 0), `last_updated` (timestamptz). `ON CONFLICT (engine) DO UPDATE`; never delete rows.

#### `risk_close_ledger` (`20260519_0000`)
- Close-out ledger rows (per-engine close events); PK + audit columns per the migration.

#### `open_orders` (`20260512_0000`)
- `id` (uuid, PK), `engine` (text), `trade_id` (text), `ticker` (text), `order_type` (text), `alpaca_order_id` (text, NULL), `status` (text), `fill_price` (numeric, NULL), `filled_at` (timestamptz, NULL), `decision_data` (jsonb), `created_at`, `updated_at` (timestamptz).

#### `allocations`
- `id`, `engine`, `decided_at`, `weight`, `allocated_capital`, `prior_equity`, `realized_vol`, `freeze_state` (`active`/`soft_frozen`/`hard_frozen`), `freeze_reason`, `drawdown_pct`.

#### `aar_events`
- `id` (uuid, PK), `engine` (text), `trade_id` (text), `ticker` (text), `aar_data` (jsonb), `classification_id`, `recorded_at`. Unique `(engine, trade_id)`. **FK GAP (rebuild must close):** `20260524_1903` added the `classification_id` COLUMN but **never added the FK constraint** — live `aar_events` has zero FKs (verified), so a row can carry a `classification_id` pointing at nothing (the orphan class the FK rollout meant to close). The rebuild MUST add `FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) ON UPDATE CASCADE ON DELETE RESTRICT` (NOT VALID → VALIDATE after orphan audit), matching the other 19 substrate FKs. The v1.1 "(FK, `20260524_1903`)" label was aspirational, not live.

#### `aar_deferred` (`20260522_0000`)
- Deferred-AAR queue rows; PK + payload per the migration.

#### `daemon_heartbeats` (`20260515_0000`)
- Per-daemon liveness rows; PK on daemon name + last-beat timestamp per the migration.

#### `ingest_manifest` (`20260525_0200`)
- `manifest_id` (UUID, PK DEFAULT gen_random_uuid()), `source` (TEXT), `provider` (TEXT), `pulled_at` (TIMESTAMPTZ), `source_locator` (TEXT), `expected_rows` (BIGINT, NULL), `actual_rows` (BIGINT), `status` (TEXT `CHECK IN ('ok','partial','failed')`), `checksum` (TEXT, NULL), `date_range_start`/`date_range_end` (DATE, NULL), `notes` (TEXT, NULL), `recorded_at` (TIMESTAMPTZ). Indexes on `(source, pulled_at DESC)` + `status <> 'ok'`.

#### `application_log`
- `id` (uuid, PK), `engine` (text), `run_id` (uuid), `event_type` (text — incl. `'DATA_OPERATIONS_COMPLETE'`), `severity` (text), `message` (text), `data` (jsonb, NULL), `recorded_at` (timestamptz). Index `(engine, run_id, recorded_at)`. 7-day retention DELETE on every write.

#### `data_quality_log` — durable detector substrate + consolidation target (**REDESIGN, not a transcription**)
The LIVE table (`20260509_0000`) is `id bigint PK`, NOT-NULL metric columns (`latency_ms int NOT NULL`, `missing_bars int NOT NULL DEFAULT 0`, `stale bool NOT NULL`, `confidence numeric(4,3) NOT NULL`), `notes text`, no `kind`. The rebuild's version is therefore a **deliberate redesign** of this table — it generalizes a single-purpose freshness-metric log into the consolidation home for the merged sidecars (§2.2). The target schema:
- `id` (uuid, PK).
- `kind` (text, NOT NULL): the discriminator — `'validation'` / `'confirmed_data_gap_evidence'` / `'failed_alpha'` / `'parity_drift'` / `'forensics_trigger'` / `'ingest_quarantine'` / `'execution_quality'` / `'backtest_credibility'` (absorbs the merged sidecars, §2.2). `'ingestion_metrics'` is RECONSIDERED — it may live on `ingest_manifest` instead (see note below).
- `source` (text): originating check / feed.
- `timestamp` (timestamptz).
- **Typed metric columns are now VALIDATION-ONLY and NULLABLE** (the live ones are NOT-NULL, which only made sense when every row was a freshness check): `latency_ms` (int, NULL), `missing_bars` (int, NULL — repurposed failure count), `stale` (boolean, NULL), `confidence` (numeric, NULL). They carry a `CHECK` that ties them to `kind` (e.g. populated only WHERE `kind='validation'`); for all other kinds the payload lives in `notes`. (Alternative considered: move the typed columns entirely into `notes` and keep none typed — rejected because the `kind='validation'` hot path benefits from typed/indexable freshness columns. The plan PR picks one; default is keep-typed-validation-only-with-CHECK.)
- `notes` (**jsonb**, not text): kind-specific payload with a **documented per-`kind` sub-schema** — each `kind` declares its required jsonb keys (e.g. `validation`: `{failures:[…], check_name}`; `parity_drift`: `{client_order_id, drift_bps, paper_fill, live_fill}`; `forensics_trigger`: `{fingerprint, dossier_path}`; `ingest_quarantine`: `{raw_payload, error}`; `confirmed_data_gap_evidence`: `{ticker, period_end_date, evidence}`). The jsonb migration from the live `notes text` is part of the redesign.
- `recorded_at` (timestamptz DEFAULT now()).
- **Partial indexes per hot `kind`** so the consolidation does not turn every read into a full scan filtered by discriminator: `(timestamp) WHERE kind='ingest_quarantine'`, `WHERE kind='parity_drift'`, `WHERE kind='execution_quality'` (the high-traffic discriminators) at minimum; plus a jsonb GIN index on `notes` if reader predicates dig into payload keys.
- Persistence is per-check, per-phase, crash-safe (each check written as it completes; `.claude/rules/migrations.md`). **No new sidecar table** — this is the home (invariant I1; audit §4 moratorium #1).

> **`ingestion_metrics` — reconsider the fold (open in the plan PR).** Per-run ingest metrics are high-churn and are arguably reconciliation data, not quality observations. `ingest_manifest` (the per-batch source-vs-DB reconciliation row, `20260525_0200`) is the more natural home for counts/durations. Fold a metric into `data_quality_log kind='ingestion_metrics'` only if it is genuinely a quality OBSERVATION (anomaly, drift); route plain reconciliation counts to `ingest_manifest`. Decision deferred to the plan PR; flagged here so the consolidation is not applied blindly (§2.2).

> **`spread_observations_retention_trg` (housekeeping trigger) — recreate.** Listed in §3.1's trigger inventory; restated here so the implementer recreates the 30-day rolling-retention AFTER-INSERT trigger on `spread_observations` (`20260512_2100`) during the clean-schema rebuild.

---

## 4. Identity model + triggers + `as_of` reader contract

### 4.1 The chain (ADOPT — invariant A1/A2; identity-path rule)

`(ticker, date) → ticker_history (SCD-2: valid_from <= date < COALESCE(valid_to,∞)) → classification_id (TKR-14) → ticker_classifications → {cik, figi, cusip, isin, asset_class, …} → issuer_securities (SCD-2) → issuers`.

Writers prove the chain; readers pass `as_of`; SEC-first authority. This is the locked design — §4 cites it, does not redesign it.

### 4.2 Triggers (write side — ADOPT SHAPE + FIX the predicate, §3.1)

15 INSERT-only `BEFORE INSERT` triggers auto-assign `classification_id` from `ticker_history` at the row's date column (`20260524_1500`). Identity-first build order (§5.3) means the substrate is complete before child loads, so triggers attribute correctly at load — the rebuild does NOT depend on a retroactive re-attribution UPDATE.

**Deliberate fix (not a pure adopt):** the live trigger functions key the lookup with the CLOSED predicate `valid_from <= as_of AND (valid_to IS NULL OR valid_to >= as_of)`, which double-matches at the `valid_to` boundary of a reused-ticker pair (the predecessor and successor windows touch) and contradicts the half-open `[)` EXCLUDE constraint + invariant D2. The rebuild ships the **half-open predicate `valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)`** in all 15 trigger functions, plus the `short_interest` as-of fix to `release_date` (§3.1). A boundary-date sentinel asserts trigger output == `IdentityDispatcher` output (§3.1 trigger note; §1.2 decision 5d). This is the single behavioral change to the write side; the per-table date columns and the INSERT-only shape are otherwise adopted unchanged.

### 4.3 The `as_of` reader contract (heavy-lane CODE change — design only here)

This is the read-side fix (audit §2.9/§5 step 9; invariant A2). It is a **heavy-lane code change** executed in the plan PR, not this docs-only spec; the contract it must satisfy:

- **`IdentityDispatcher.ticker_to_classification_id(ticker, as_of)` must always receive `as_of`.** The bare-`ticker` overload is the bypass that contaminated the ~1,400-ticker cohort (1,149 pre-FPFD + 266 distinct-cls). **TWO defects, not one:** (i) callers omitting `as_of`; AND (ii) — the v1.1 claim "the dispatcher already implements SCD-2 correctly" is WRONG against live code: `tpcore/identity/dispatcher.py:68-69` and `tpcore/corp_history/__init__.py:42-43,53-55` BOTH use the same CLOSED predicate `(valid_to IS NULL OR valid_to >= as_of)` as the live triggers. So the half-open `[)` fix (decision 5d) must be applied to the **`IdentityDispatcher` and the `corp_history` resolver too**, not just the 15 triggers — otherwise all three agree by being mutually wrong at the boundary date. The boundary sentinel must therefore assert against the **`ticker_history` `daterange('[)')` EXCLUDE oracle / an explicit expected value**, NOT trigger-output == dispatcher-output (which would pass by mutual wrongness).
- **20/20 engine readers must pass `as_of=row_date`.** Named violators (audit §2.9): `tpcore/backtest/price_loader.py:61`, `tpcore/data/repositories/prices.py` (`PricesRepo.get_window_batch` — `WHERE classification_id = ANY($1)` with no date filter), `momentum/backtest.py:223,531`, `catalyst/backtest.py:317,370,423`, plus the not-yet-audited `reversion`/`vector`/`sentinel`/`canary` reader paths.
- **Contract shape:** `PricesRepo.get_window_batch(...)` and every `IdentityDispatcher` caller take `as_of` (default disallowed — a new caller without `as_of` is a critical defect per identity-path rule). The repo resolves `(ticker, as_of) → classification_id` via `ticker_history` SCD-2, then filters bars by that resolved cls — so a delisted-then-reused ticker's old bars resolve to the predecessor cls, not the current entity.
- **Order routing is NOT contaminated** (audit §2.9): Alpaca uses the ticker string (`broker_adapter.submit_order`); RiskGovernor operates on ticker string. The `as_of` fix is read/backtest-scoped.
- **Gate:** this change runs through `/system-wide-verification` + `/change-impact-classification` (discovery-first rule), classified `engine_signal_change` / `validator_or_gate_change`, citing the identity chain explicitly (identity-path rule).

---

## 5. Ingest contract

### 5.1 Principles (invariants C1–C7)

- **Bulk file BEFORE per-row API crawl** (C1): SEC `submissions.zip` / FMP `historical-price-eod/full` before any per-ticker loop. If a loop's ETA > 1h, STOP.
- **CSV-first** (C2): download → validate-at-CSV (same CHECK predicates as schema, invariant E2) → COPY → compress. No pure DB-side INSERT loops.
- **All via `scripts/ops.py --stage <name> --param …`** (C7): NEVER a one-off `scripts/foo.py`. New stages adjacent to DFCR/cutover are heavy-lane.
- **Idempotent stages** (C4): `ON CONFLICT …`; re-run = same end-state.
- **HTTP retries via `tpcore.outage.with_retry`** (C5): no local loops / `asyncio.sleep`.
- **Adapter-contract sentinel** (C6): producer hard-stop if a required field is empty across a non-empty pull.
- **Daily update runs FIRST** (C3); non-daily after daily completes.

### 5.2 Per-source roster (DFCR-governed — never hand-edit `tpcore/providers.py`)

| Lane | Source (primary) | Fallback | Bulk-first artifact | Invariant |
|---|---|---|---|---|
| Daily prices | **FMP** (full CTA tape) | Alpaca IEX/SIP (diagnostics; NEVER backfill) | FMP `/stable/historical-price-eod/full` | B1, G1 |
| Identity (universe, CIK, FPFD) | **SEC EDGAR** | FMP non-US only | `submissions.zip`, full company list, `full_history=True` | A5, A7, B2 |
| Fundamentals | **SEC/FMP** (FMP parses SEC XBRL) | FMP | bulk fundamentals; SEC companyfacts for FPFD | D1, D6 |
| Filings / insider / 8-K | **SEC EDGAR** | FMP non-US | `submissions.zip` index | A7, B2, H5 |
| Macro | **FRED** | — | FRED bulk; **`hy_spread` COPIED verbatim** | B5, K1 |
| Short interest | **FINRA** (OAuth2) | — | FINRA consolidated; `release_date` PIT | B7 |
| Borrow | **IBorrowDesk** | — | per-ticker scrape; 3-fail circuit-breaker | B8, H1 |
| Insider sentiment | **Finnhub** (free tier) | — | free-tier MSPR only | B6 |
| Social | **ApeWisdom** | — | all pages, T1/T2 filter | — |
| Retail | **AAII** | — | single OLE2 `.xls` full-history workbook (browser-shaped request) | B9, H2 |

### 5.3 Identity-first ingest order (invariant A1/A4; audit §5)

Strict ordering so the 15 triggers attribute `classification_id` correctly at load:

1. **universe** (SEC full company list ∪ FMP symbol list + delisting/symbol-change history) — survivorship-free roster.
2. **issuers** (SEC-first: CIK, legal_name, FPFD, FYE, doc-type) → add cik FK NOT VALID → VALIDATE.
3. **ticker_classifications** (TKR-14 mint; SEC-first identity; lifetime_start from FPFD, NO sentinel) **+ ticker_history** (multi-row for delisted-then-reused; predecessor windows).
4. **issuer_securities** (M:N issuer↔security links; share-class fan-out).
5. **prices** (FMP full tape) — triggers attribute on `date`.
6. **fundamentals** (SEC/FMP) — triggers attribute on `period_end_date`; restatement-preserving dedup via the 3-part PK `(ticker, period_end_date, filing_date)` (amended filings kept).
7. **signals** (earnings, short_interest, borrow, insider, social, sec_material_events, spreads). **`macro_data` is OUT of scope — not wiped, not re-ingested.**
8. **derived** (liquidity_tiers from spreads; universe_candidates pre-screen).

### 5.4 Macro is OUT of scope — SACRED preserved by construction (invariant K1/K2)

`macro_data` is **not touched** by the rebuild (operator 2026-06-04): it has no FKs / no `classification_id` and is not in the identity rot. The SACRED `hy_spread` series (K1, FRED `BAMLH0A0HYM2`, 7,681 rows 1996→2026-05-31) and the byte-identical sentiment-baseline fixtures (K2) are therefore preserved **automatically** — there is no wipe-and-restore step for macro. A verbatim off-DB backup of `hy_spread` (`data/macro_hy_spread_sacred_archive/` + `s3://ste-archives/...`, taken 2026-06-04) plus the recovery source (`data/hy_spread_recovery/`) exist as belt-and-suspenders. The tall+bitemporal `macro_data` unbounded-growth concern is a separate future arc (OQ-6), not this rebuild.

### 5.5 Population/attribution fixes baked into the re-ingest (the actual rot)

- **SEC full-history FPFD = earliest `filingDate`** (A5): kill the recent-shard stale-state bug AND correct the semantic — FPFD is the earliest `filingDate` across the full EDGAR submission index, NOT `min(DocumentPeriodEndDate)` (which is the earliest fiscal-period-end and reintroduces look-ahead; §3.1). The implementing fix is in the companyfacts/submissions adapter (`companyfacts_adapter.py`: stop using `min(reportDate)`; read the submission-index `filingDate` min) — plan-PR work. Verify against LMT/PEP mega-cap samples (earliest FILING, not earliest period, not stale-state). The "first periodic-fundamentals available" boundary, if wanted, is the SEPARATE `first_periodic_report_date` (§3.1) — never conflated with FPFD.
- **lifetime_start from FPFD** (A6): kill the `1900-01-01` sentinel (100% today).
- **predecessor-classification + ticker_history backfill** for delisted-then-reused (A4/G3).
- **identity-aware validators** (F6): clamp completeness to issuer FPFD / security lifetime_start (the 32 checks are identity-blind today, audit §2.9).
- **read-side `as_of`** (A2): §4.3.

### 5.6 Split/dividend adjustment — cumulative-factor model

`prices_daily.adjusted_close` is the total-return-adjusted price; raw `close` is never mutated (§3.2). The adjustment reader (`apply_splits` → a cumulative-factor applier) reads split + dividend rows from the **standalone `corporate_actions` table** (which the rebuild KEEPS, NOT folds into `corporate_events` — §2.1/§2.2): `adjusted_close = close × Π(factor of every action with action_date > date)`, split factor for splits and `(1 − div/ex_date_raw_close)` for cash dividends. The model is idempotent (function of immutable raw close + append-only `corporate_actions`), dividend-aware, and reverse-split-aware (factor < 1). The decision to keep `corporate_actions` standalone (vs the v1.0 fold into `corporate_events`) is exactly to keep this hot read path off the bitemporal M&A graph — see OQ-3.

---

## 6. Validation contract

- **32 checks**, reconciled **identity-aware** (invariant F6): each completeness/freshness check clamps its denominator to issuer FPFD / security `lifetime_start` so legitimately-young issuers don't FAIL (the cause of today's `fundamentals_quarterly_completeness` 111-ticker FAIL, audit §2.13).
- **`prices_daily_completeness`** zero-tolerance (invariant F2): every tier≤2 liquid currently-trading common stock has a bar for every NYSE session in the recent window within its active range; ANY miss = FAIL. Denominator = engine universe.
- **`prices_daily_freshness`** dual gate (F3): CRITICAL_TICKERS ≤5 sessions stale + universe staleness ≤2% at 14d.
- **`fundamentals_quarterly_completeness`** routes by `sec_document_type_primary` + per-form MAX_GAP (D6); metadata-coverage sentinel (F4). **Foreign-filer routing:** 20-F / 40-F filers are ANNUAL-ONLY (no 10-Q quarterly equivalent); their interim reporting is the 6-K, which is often semi-annual and has no standardized period-end. So for `sec_document_type_primary IN ('20-F','40-F')` the completeness MAX_GAP must be ANNUAL (~450d), with 6-K interim treated as OPTIONAL (present-if-filed, never a FAIL when absent) — applying the 10-Q ~100d quarterly grid to a foreign annual filer would FAIL every legitimate annual filer. **The expected-period grid is computed relative to `fiscal_year_end_month`** (non-December FYE issuers — AZO=Aug, BNED=Jan, etc.), NOT calendar quarters; the live `fiscal_year_end_month` column (`20260530_0200`) drives this. This generalizes the per-form MAX_GAP of D6 to the foreign-filer and non-Dec-FYE cases.
- **`row_integrity`** (F5): close>0, high>=low, no future dates.
- **`DATA_OPERATIONS_COMPLETE` only on 100% green** (F1): no tolerance knob, no operator-task step; deterministic self-heal cascade is the only recovery (`.claude/rules/selfheal-auditheal.md`).
- **HealSpec registry-coverage**: HealSpec set == `suite.KNOWN_CHECK_NAMES`; a new/changed check fails the build until a HealSpec decision (healable OR honest `healable=False`) is recorded.
- **Detector/healer parity**: validation suite is the detector; `tpcore.selfheal` is the healer in the same layer, sharing `_evaluate`.
- **auditheal (Step 3)**: cross-table referential audit persisted to `data_quality_log`; bounded `cross_ref_cleanup`; hard-stop on any unremediated red. (Note: the proven `tradier_options_chains` orphan-cleanup class is now moot — table dropped.)

---

## 7. Supabase wipe / re-ingest mechanics (invariants §J)

- **TRUNCATE, not DELETE** (J7): immediate disk reclaim, no dead-tuple bloat, no VACUUM FULL.
- **DDL + bulk COPY over SESSION mode `:5432`** (J2), NOT the `:6543` transaction pooler (`statement_cache_size=0`; pooler can't hold DDL / large COPY).
- **CHUNKED DML for >100K-row ops** (J3): 100K/chunk, commit each, ~0.5s sleep per chunk for WAL checkpoint. A single-txn 21M-row UPDATE blew 1.95 GB WAL → 97% disk → read-only (2026-05-23). The 21M-row `prices_daily` load chunks.
- **Streaming commits** (J4): flush every 100–500 rows, not buffer-then-flush-at-end (a 13K-call buffer-at-end backfill was killed at 3600s → 0 rows committed).
- **Drop non-essential indexes before COPY, recreate after**; **ANALYZE after load**; **raise `SET LOCAL statement_timeout`** for the long VALIDATE (prices_daily FK VALIDATE ~5–15 min on 21M rows). Note: the `prices_daily.classification_id` FK is ALREADY VALIDATED on the live 21M-row table and all 15 child FK columns are already indexed — the validate-time risk is well-characterized and bounded. The real risk on the rebuild is the **COPY + WAL pressure of the 21M-row reload** (J3 chunking / J4 streaming-commits guard it), NOT the FK VALIDATE itself; the risk register (§8.3) is framed around the reload, not the validate.
- **Disk ≈ 18 GB** (J1, auto-resized from 8 GB — check the UI, assumed caps are stale); **auto read-only at ~95%**; 4h resize cooldown; **no superuser CHECKPOINT** (J6, auto every 5 min / 1 GB).
- **Read-only-event recovery** (J5): session READ WRITE override + VACUUM + wait for WAL checkpoint (~5–15 min).
- **Build issuers from SEC FIRST → add cik FK NOT VALID → ingest securities → VALIDATE** (invariant I3; the FK NOT-VALID-then-VALIDATE pattern keeps the load fast and validates after).

---

## 8. Phasing + rollback + scheduled writers to stop

### 8.1 Scheduled writers to STOP FIRST (before any wipe)

No writer may touch the DB during the rebuild. Stop both substrates (DATABASE_AND_DATAFLOW §0):

- **Local launchd**: `~/Library/LaunchAgents/com.michael.trading.*.plist` — engine-service, allocator, data-ops, lane-service, forensics, any ingest daemon. `launchctl unload` each.
- **Railway**: `railway.json` services (ingestion-engine, any deployed daemon) — pause / scale to zero.
- **Verify**: no `application_log` writes from a non-rebuild `run_id` during the window; the data-ops mkdir-atomic self-exclusion lock held.

### 8.2 Phases (each marked reversible / irreversible)

| # | Phase | Reversible? |
|---|---|---|
| 0 | This spec approved; moratoria lifted for the plan PR | reversible (no DB touch) |
| 1 | Snapshot SACRED series (`hy_spread`) + sentiment fixtures to a verbatim-copy artifact; full pre-wipe DB snapshot (Supabase PITR + on-demand snapshot) | reversible |
| 2 | Stop all scheduled writers (§8.1) | reversible (re-enable) |
| 3 | Apply clean-schema migrations (KEEP-table DDL, consolidation targets, triggers, EXCLUDE) on a fresh schema; **TRUNCATE** legacy tables / DROP the §2.3 set | **IRREVERSIBLE** (schema cutover; snapshot from step 1 is the rollback) |
| 4 | Ingest **universe** (SEC ∪ FMP, survivorship-free) | reversible (re-truncate + re-run) |
| 5 | Ingest **issuers** (SEC-first) → cik FK NOT VALID | reversible |
| 6 | Ingest **identity** (ticker_classifications TKR-14 + ticker_history multi-row + issuer_securities); lifetime_start from FPFD | reversible |
| 7 | Ingest **prices** (FMP full tape, chunked, staging→promote) | reversible |
| 8 | Ingest **fundamentals** (3-part-PK dedup; restatements preserved) | reversible |
| 9 | Ingest **signals** (`macro_data` is OUT of scope — never wiped, never re-ingested) | reversible |
| 10 | **Re-attribution verify**: 0 NULL classification_id, 0 orphans, 0 pre-FPFD bars, 0 out-of-window bars (the audit §2.6/§2.7 metrics → all zero) | reversible (re-run upstream phase) |
| 11 | **FK VALIDATE** all classification_id FKs (NOT VALID → VALIDATE under raised statement_timeout) | reversible (FK is additive) |
| 12 | Run **validation suite** identity-aware; observe failure counts | reversible |
| 13 | Re-enable scheduled writers; first `DATA_OPERATIONS_COMPLETE` on 100% green | reversible |
| 14 | Bring `DATABASE_AND_DATAFLOW.md` §2/§3 current (§9) | reversible (docs) |

### 8.3 Rollback

The pre-wipe snapshot (step 1) + Supabase PITR (7-day) is the rollback for the irreversible step 3. Every ingest phase (4–9) is idempotent re-run-from-truncate. If step 10 re-attribution verify is not all-zero, the offending upstream phase re-runs; the suite (step 12) never green-lights a partial state because of F1 (100%-green-or-don't-trade).

---

## 9. Plan to bring `DATABASE_AND_DATAFLOW.md` §2/§3 current

§0 already flags that §2 LAGS the live DB (it predates the TKR-14 identity layer; it still describes ticker-PK `prices_daily`, the removed `fear_greed`/`macro_indicators` wide tables, Tradier-as-source, `options_max_pain`, `ingestion_jobs`). The rebuild's doc-refresh (phase 14):

- **§2.1 ERD**: redraw with the identity spine (ticker_classifications ← ticker_history ← child tables; issuers ← issuer_securities ← classifications), corporate_events as the M&A graph, `corporate_actions` as the standalone split/dividend feed into `prices_daily.adjusted_close`, data_quality_log / macro_data as consolidation hubs. Remove Tradier/options nodes.
- **§2.2 table defs**: replace every stale table block with the §3 column-level target (TKR-14 PK; 3-part natural-PK `fundamentals_quarterly` `(ticker, period_end_date, filing_date)`; `current_ticker` as the canonical symbol; classification_id FK on all child tables with the half-open trigger predicate; FMP-primary `prices_daily.source` with no Tradier; raw `close` + cumulative-factor `adjusted_close`; `corporate_actions` kept standalone for split-adjust; macro_data + shim views; corporate_events absorbing `ticker_lifecycle_events` only; data_quality_log redesigned with jsonb `notes` + `kind` discriminator). Delete the `tradier_options_chains`, `options_max_pain`, `ingestion_jobs`, `fear_greed`, `macro_indicators`, `execution_quality_log` standalone blocks (folded/dropped per §2).
- **§3 dataflow**: replace the Alpaca-bars / Tradier-merge / `social_signals` / `filings_insider` narrative with the identity-first ingest order (§5.3), the FMP-primary + SEC-identity roster, and the `as_of` reader contract. Update the cron narrative to local launchd / paused-Railway reality.
- **§0 index**: flip the "§2 schema LAGS" caveat to "current as of rebuild"; point identity readers at this spec.
- Keep §0 invariant-pointer block (it is current).

---

## 10. Invariants the rebuild MUST preserve (cite `docs/DATA_INVARIANTS.md` by ID)

The rebuild preserves **all 113** invariants in `docs/DATA_INVARIANTS.md`. The load-bearing set, by theme:

- **A. Identity & attribution** — A1 (prove chain on every write; 15 triggers, half-open SCD-2 predicate fixed — §4.1/§4.2), A2 (readers pass `as_of` — §4.3), A3 (PK = TKR-14, never ticker/CIK), A4 (ticker_history load-bearing; row resolves to cls valid at row date via the half-open `valid_from <= d < valid_to` predicate — the trigger off-by-one is FIXED in the rebuild, §1.2 decision 5d), A5 (SEC-first FPFD = earliest **`filingDate`** across the full EDGAR submission index, NOT `min(DocumentPeriodEndDate)` — §3.1; `first_periodic_report_date` is the separate fundamentals-availability boundary), A6 (lifetime_start from FPFD; the `1900-01-01` sentinel DEFAULT is DROPPED, column is NOT NULL with no default — §3.1), A7 (SEC authoritative US, FMP non-US fallback), A8 (FMP never silently overrides SEC).
- **B. Source authority** — B1 (FMP prices; never Alpaca backfill), B2 (SEC bulk-first identity/fundamentals/filings), B3 (DFCR-only roster), B4 (CUTOVER automated, ONBOARD/RETIRE operator-approved), B5 (FRED macro), B6 (Finnhub free-tier insider), B7 (FINRA release_date PIT boundary), B8 (IBorrowDesk circuit-breaker), B9 (AAII browser-shaped), B10 (Tradier closed; options out).
- **C. Ingest mechanics** — C1 (bulk before crawl), C2 (CSV-first), C3 (daily first), C4 (idempotent), C5 (with_retry), C6 (contract-population sentinel), C7 (ops.py --stage only).
- **D. Point-in-time** — D1 (FQ 3-part natural PK `(ticker, period_end_date, filing_date)` preserving restatements + `WHERE filing_date <= as_of` PIT reader idiom — §3.2), D2 (SCD-2 predicate `valid_from <= d < COALESCE(valid_to,∞)` — now matched by the FIXED half-open trigger predicate, §4.1/§4.2), D3 (delisted PIT-safe filter not backfill), D4 (idempotent split+dividend adjustment via the cumulative-factor model over raw `close` — §3.2/§5.6), D5 (insider filing_date PIT + CHECKs; BUY/SELL = open-market P/S only, §3.3), D6 (FQ completeness per-form MAX_GAP incl. foreign-filer annual routing — §6).
- **E. Provenance honesty** — E1 (never fabricate; honest NULL/gap), E2 (CSV-layer applies schema CHECKs), E3 (source column), E4 (short_interest_pct PIT-derived, NULL when absent), E5 (evidence substrate via data_quality_log, not a sidecar).
- **F. Validation** — F1 (DOC only on 100% green), F2 (prices_daily_completeness zero-tolerance), F3 (freshness dual gate), F4 (FQ metadata-coverage sentinel), F5 (row_integrity), F6 (validators identity-aware).
- **G. Survivorship-freeness** — G1 (survivorship-free; FMP full CTA tape), G2 (delist_stale promotion), G3 (ticker_history covers delisted-then-reused; rebuild-from-source universe).
- **H. Scrape-fragility** — H1 (IBorrowDesk 3-fail skip), H2 (AAII 403 permanent), H3 (FMP per-endpoint limits + CSV rejection), H4 (no LLM in foreground data ops), H5 (SEC 10 req/s; bulk sidesteps).
- **I. Schema/PK** — I1 (no new table without rationale — every consolidation is into an EXISTING KEEP table; `data_quality_log` is a redesign of an existing table, not a new sidecar — §3.3), I2 (every table has a PK; FQ moves to the 3-part natural PK `(ticker, period_end_date, filing_date)` — restatement-preserving, §3.2), I3 (FK NOT VALID → VALIDATE; trigger auto-assign with the half-open predicate), I4 (idempotent migrations), I5 (surrogate PK only for append-only event/audit tables — `corporate_events` bitemporal; `corporate_actions` keeps its `(ticker, action_date, action_type)` natural key), I6 (issuer layer design-LOCKED — issuers surrogate PK, issuer_securities M:N kept, issuer_history SCD-2; never CIK-as-PK / never drop issuer_securities).
- **J. Supabase ops** — J1 (18 GB disk / auto read-only at 95%), J2 (session mode for DDL+COPY), J3 (chunked DML >100K), J4 (streaming commits), J5 (read-only recovery), J6 (no superuser CHECKPOINT), J7 (TRUNCATE not DELETE).
- **K. SACRED** — K1 (hy_spread copied verbatim, never re-pulled), K2 (byte-identical sentiment-baseline fixtures preserved).
- **L. ISO standards** — L1 (ISO before custom), L2 (ISO 3166-1 α-2 country), L3 (ISO 7064 Mod-97-10 check digit), L4 (Crockford base32), L5 (ISO 8601 dates).

---

## Open questions (genuinely unresolved — for operator/expert)

- **OQ-1 (universe source-union semantics).** When SEC's full company list and FMP's symbol list disagree on a ticker's existence/identity, A7/A8 say SEC wins for US CIK-backed issuers and FMP is non-US fallback — but the *universe-construction* union (which tickers enter `universe_candidates` / get a TKR-14 minted) needs an explicit precedence rule for the gray zone (FMP-only tickers with no SEC CIK that are nonetheless US-listed micro-caps). Proposed: mint from FMP with `cik=NULL`, `discovery_source='FMP'`; flag for SEC reconciliation. Needs confirmation.
- **OQ-2 (macro_data) — RESOLVED 2026-06-04: OUT OF SCOPE.** Operator decision: the rebuild touches TICKER / identity data ONLY; `macro_data` is preserved in place, **untouched** (no FKs, no `classification_id`, not in the identity rot). Not re-created, not TRUNCATEd, not re-ingested. The SACRED `hy_spread` is safe by construction (backed up off-DB anyway). Its redesign is OQ-6.
- **OQ-3 (split/dividend-adjustment reader) — RESOLVED to a design in v1.1; one operator-confirm point.** v1.0 proposed folding `corporate_actions` into `corporate_events` and reading splits from the M&A graph. **v1.1 chooses the opposite: keep `corporate_actions` as a thin standalone table** for the `adjusted_close` hot path (§2.1/§2.2/§5.6), because the bitemporal M&A graph is the wrong shape for splits (no predecessor/successor for an `event_id` SHA; bitemporal overhead on a price-adjust read). The adjustment code (`apply_splits` → cumulative-factor applier) reads `corporate_actions` directly and computes `adjusted_close = close × Π(factor for action_date > date)` (split + dividend factors; §3.2). This is a heavy-lane code change in the plan PR. **Operator confirm:** this standalone-table choice (vs the v1.0 fold). On `reverse_split`: the `corporate_actions` table's `action_type` CHECK today is `IN ('split','dividend')`; a reverse split is representable as a `split` with factor < 1 and the factor model handles it natively (no separate `reverse_split` predicate needed) — so the rebuild declares reverse splits **in-scope via the factor model**, NOT a separate read-predicate kind. (If, instead, a producer for an explicit `reverse_split` action_type is wanted, that is a `corporate_actions` CHECK extension + producer — not assumed here; absent that producer, do NOT list `reverse_split` as a distinct read predicate, which would be a dead branch.)
- **OQ-4 (count_snapshot tables → views).** §2.4 demotes the `*_count_snapshot` tables to VIEWs. Confirm no writer depends on them being materialized (e.g., a daemon that snapshots a count at a point in time for drift detection). If a point-in-time snapshot is load-bearing, it folds into `data_quality_log` (`kind='count_snapshot'`) rather than a view.
- **OQ-5 (execution_quality_log fold).** §2.2 folds `execution_quality_log` into `data_quality_log` (`kind='execution_quality'`). DATABASE_AND_DATAFLOW §2 already shows partial co-location, but confirm the `ExecutionQualityWriter` consumer/dashboard panel can read from the consolidated table without a behavior change (CIC `validator_or_gate_change` boundary).
- **OQ-6 (macro_data redesign — SEPARATE FUTURE ARC, explicitly NOT this rebuild; operator-flagged 2026-06-04 as prior-recommendation defects).** `macro_data` (tall + bitemporal, 186,937 rows and growing) has two design problems, both out of the ticker rebuild's scope:
  1. **Wrong-scope conflation.** It holds two distinct domains in one table: *market macro* (FRED VIX / yield-curve / Sahm / `credit_spread` / SACRED `hy_spread` + AAII / CNN F&G sentiment) — which the trading engines consume — and the *LWA county/state economic series* (`cle_coles_*`, `crb_*`, `phci_*`, `sos_*`), which belong to a **different scope** (the public LWA-25 dashboards). They share a table only by accident of the Task #18 "consolidate everything" fold. Correct model: `macro_data` = market-only; the per-state economic data is its own entity, owned by the dashboards project.
  2. **Unbounded growth.** Tall + bitemporal means every revision and every new series adds rows without bound; the per-state sprawl is the bulk of it. Needs partitioning / retention / realtime-version pruning — cleanest *after* the scope split removes the per-state volume.
  This is its own arc (audit → spec → plan), scoped against the public-dashboard project, NOT folded into the ticker rebuild. `macro_data` stays untouched until then.
