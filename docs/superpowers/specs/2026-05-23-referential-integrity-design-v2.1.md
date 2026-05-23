# Referential-Integrity Design v2.1 — `platform.*` Schema, Phase 1 (FK Enforcement, complete-concern-map amendment)

**Status:** v2.1. **Supersedes v2 (`docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md`)** by closing the concern-map gaps surfaced mid-execution and re-sequencing the phase order to land in the right shape. v2 stays on disk as the historical record of the original NOT-VALID-FIRST inversion thinking. Where v2.1 and v2 conflict on phase sequence, orphan-handling protocol, or in-scope table list, **v2.1 wins**.

**Corrections (post-merge 2026-05-23):**
1. **Phase 0.6 (`pg_dump` daily backup regimen) DROPPED.** Supabase Pro provides daily backups + 7-day PITR (operator-verified 2026-05-23). The earlier expert opinion that recommended `pg_dump` framed it as COMPLEMENT to Supabase's coverage; in this single-Mac, single-tenant operator context, the Supabase coverage is sufficient. Tenant-loss recovery for paper-trading-only scope doesn't justify the daily compute + S3 storage overhead.
2. **Phase 0.5 (`db_snapshots/`) RE-SCOPED to ON-DEMAND ONLY.** No daily cron, no 30-day retention, no launchd plist. The script (`scripts/db_snapshots.py`) is invoked manually right before a Phase 4 cleanup PR — captures the table(s) being cleaned up — and the snapshot files are DELETED after the cleanup is verified. Pure pre-cleanup rollback, not a backup.

v2 (and v1 before it) stay readable for tactical migration templates + risk-register cross-reference. **For sequencing, scope, and the contract, v2.1 is authoritative.**

**Author / role:** `db-architect` (Postgres + Supabase Pro tier; `platform.*` schema). See `.claude/agents/db-architect.md`.

**Spec basis (read in this order):**
1. v2 of this spec: `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md` — the NOT-VALID-FIRST inversion. v2.1 inherits its §2 invariants, §3 scope (REVISED per v2.1 §3 below — `insider_mspr_daily` is a VIEW, removed), §4 non-goals, §5 NOT-VALID pattern, §6 index-audit method, §8 test contracts (EXPANDED per v2.1 §8 below), §9 verification gates, §10 compatibility-view pattern (REVISED per v2.1 §10 below — Postgres auto-rewrites simple views), §11 cleanup-template `ctid` fix.
2. v1 of this spec: `docs/superpowers/specs/2026-05-23-referential-integrity-design.md` — §2 invariants, §3 scope, §4 non-goals inherited via v2.
3. `.claude/agents/db-architect.md` — operating contract.
4. Memory `feedback_complete_concern_map_first.md` — the 12-item concern map this amendment applies (reproduced at §2 of this spec).
5. Memory `project_database_architecture_state_2026_05_23.md` — schema state + `ticker_classifications.ticker ⊆ prices_daily.ticker` operator invariant.
6. Phase 0 audits captured during v2 execution:
   - `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md` — orphan baseline (336,857 total; 99.5% in `prices_daily`).
   - `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md` — FK-column index coverage + `insider_mspr_daily` VIEW finding.
   - `docs/superpowers/audits/2026-05-23-referential-integrity-timeout-locks-baseline.md` — `statement_timeout=120s`, role config.
7. Postgres canonical refs:
   - `NOT VALID` / `VALIDATE CONSTRAINT`: <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>
   - Simple views are AUTO-UPDATABLE: <https://www.postgresql.org/docs/current/sql-createview.html#SQL-CREATEVIEW-UPDATABLE-VIEWS>
   - `pg_dump` custom-format + `pg_restore`: <https://www.postgresql.org/docs/current/app-pgdump.html>
8. Memory `project_supabase_pro_tier.md` — `statement_timeout` posture.
9. Memory `project_railway_archive_substrate_migration.md` — D2 (Postgres-detection) + R3 (object-storage-bucket recovery) decisions; `db_snapshots/` Phase 0.5 builds on R3.

**Operator trigger (v2.1-specific, 2026-05-23):**
> *"you dont think about the entire picture and then we get half through and now i'm asking questions where there are no answers"*
>
> *"hold #320, write the v2.1 amendment first"*

v2 spec/plan scoped too tight. Phases 0/1/2 already merged (PR #317/#318/#319); Phase 3 PR #320 held in flight. v2.1 closes the concern-map gaps surfaced mid-execution.

---

## 1. What v2.1 adds to v2 — execution-derived findings

Each finding below was discovered AFTER the v2 spec/plan shipped and AFTER one or more phases had merged. v2.1 incorporates each into the spec contract so the rollout no longer has unanswered questions.

### 1.1 `insider_mspr_daily` is a VIEW, not a table — in-scope drops 15 → 14

Phase 0 index-audit discovered `insider_mspr_daily` has `relkind='v'` (a view, not a table). Created by `20260522_0200_drop_insider_filings_add_sec_mspr.py`. Views structurally cannot have:
- Indexes (`CREATE INDEX CONCURRENTLY ... ON platform.insider_mspr_daily` returns `WrongObjectTypeError: cannot create index on relation - This operation is not supported for views`)
- Foreign keys (FKs are constraints on base tables; views don't have constraints)

**Resolution:** `insider_mspr_daily` is removed from the in-scope FK-target list. Its referential integrity is inherited from `insider_transactions` (its base table), which IS in scope and gets an FK on `ticker`. Any orphan in `insider_mspr_daily` can only exist if its base row exists in `insider_transactions` — which post-Phase-2 is FK-protected. **In-scope count: 14 tables.**

### 1.2 Postgres auto-rewrites simple views as UPDATABLE — the compat view's "loud-fail-on-INSERT" claim was wrong for this view shape

v2 spec §10.3 claimed: *"missed INSERT consumers fail loud (`cannot insert into view`)."* In practice the compatibility view created at Phase 1 was:
```sql
CREATE VIEW platform.sec_insider_transactions AS
    SELECT * FROM platform.insider_transactions WHERE source = 'sec';
```
Per Postgres docs (`sql-createview.html#SQL-CREATEVIEW-UPDATABLE-VIEWS`), a view is **automatically updatable** if it satisfies several conditions including: simple `SELECT * FROM single_table WHERE …` with no joins/grouping/DISTINCT/window functions. The view above meets every condition. Postgres auto-rewrites INSERT/UPDATE/DELETE through the view as INSERT/UPDATE/DELETE on the base table with `source='sec'` applied.

**Consequence for v2's invariant:**
- DATA INTEGRITY is preserved (the CHECK on `source IN ('sec','fmp')` still enforces; writes still hit `insider_transactions` correctly).
- The missed-write-migration **detection** signal v2 promised is LOST. A producer still writing to the old name silently succeeds via the auto-rewrite. No `cannot insert into view` error surfaces.

**Resolution (v2.1):**
- Phase 2 of v2 already DROPPED this view (per PR #319) before the v2.1 amendment was written — so the runtime behavior is now correct (writes to the old name fail with `relation "sec_insider_transactions" does not exist`).
- v2.1 amends the compatibility-view pattern (§10 below): for any future rename where loud-fail-on-write is needed, either (a) use a TRIGGER on the view that raises an `EXCEPTION`, or (b) skip the view entirely and rely on grep-sweep + drop-without-replacement, accepting `relation does not exist` as the loud-fail signal.
- The v2 contract is preserved — the *intent* of loud-fail was honored once the view was dropped at Phase 2; the *mechanism* described in v2 §10.3 was wrong for simple views.

### 1.3 Alpaca `/v2/assets` does NOT return `country` — real source is FMP `/stable/profile` per-ticker

v2 §12 assumed Alpaca returned `country` and that null-rates would be Alpaca-shape (~30–50% for ETFs). In practice Alpaca's asset listing does not expose a country field; the real source for ticker-level country is FMP's `/stable/profile/<symbol>` endpoint (per-ticker call). Phase 1 backfill is currently **88% complete** with these null-rates (verified mid-execution):

| Asset class | Null-rate |
|---|---:|
| stock | 4.8% |
| etf | 13.8% |
| spac | 19.0% |
| fund | 33.3% |

**Resolution:** v2.1 amends the country-backfill source to FMP `/stable/profile`. The Phase 1 PR (#318) already merged; the backfill is running. v2.1 documents the FMP path explicitly so Task #15's "per-country insider adapters" dependency is clear about which feed populated the column. The honest-null-rate gate per v2 §12 stays (log it; no hard threshold).

### 1.4 `prices_daily` has 335,159 orphan rows / 166 distinct tickers — BACKFILL not DELETE

Phase 0 baseline captured 336,857 total orphans across the 14 in-scope tables; `prices_daily` alone holds 335,159 (99.5%) across 166 distinct tickers. v2's plan §6.2 prescribed Path B (DELETE) as the cleanup template; for `prices_daily` this would be a deletion of real market history.

**Expert verdict 2026-05-23:** these 335K bars are real history for tickers that delisted-then-got-dropped-from-the-classifier. The correct cleanup protocol is BACKFILL `ticker_classifications` with minimal classification rows (`source='phase4_backfill', status='delisted_historical'`) for each of the 166 orphan tickers — NOT delete the bars.

**Resolution:** v2.1 amends Phase 4 to use Path A (BACKFILL) as the **default** for `prices_daily`, with Path B reserved for tables where the orphan IS junk (vendor-side ticker fragment, test rows, cryptos in social_sentiment). Per-table operator decision matrix in v2.1 plan §6.

### 1.5 `statement_timeout = 120s` on Supabase Pro — needs raise to 30min before `prices_daily` VALIDATE

Phase 0 timeout audit confirmed:
- Current `statement_timeout`: 120000 ms (2 min)
- Role: `postgres`; no rolconfig override
- Source: `configuration file` / `user` context

**Resolution:** v2.1 makes the dashboard-override action explicit in Phase 4's exit-gate dependency: before `prices_daily` VALIDATE can run, operator MUST raise `statement_timeout` to 30 min via Supabase dashboard (project → settings → database → `statement_timeout`). v2 plan §2.4 documented the budget; v2.1 makes it a hard pre-PR action.

### 1.6 Phase 3 producer first run crashed (asyncpg import was TYPE_CHECKING-only)

PR #320's `scripts/classify_tickers.py` body referenced `asyncpg.ForeignKeyViolationError` in an `except` clause but imported `asyncpg` only under `TYPE_CHECKING`. The first live run crashed with `NameError: name 'asyncpg' is not defined`. Recovered via corrective snapshot; dry_run=True is now the default.

**Resolution:** v2.1 adds an explicit test-contract item (§8 below): every producer that catches `ForeignKeyViolationError` MUST import `asyncpg` at module level (not TYPE_CHECKING) and have a unit test that simulates the FK-violation path. New §8.8 test-contract entry below.

### 1.7 classify_tickers delete-set is 6,083 stale tickers (43.7% of universe) when run today

v2 plan §5 prescribed a dry-run + `|D| > 1% halt`. Today's dry-run produces `|D| = 6083` against a ~13,919-ticker `ticker_classifications` row count — far above 1%. Many of these are Alpaca-listed-no-bars (preferred shares, delisted, etc.) and others are bars-no-Alpaca-listing (foreign ADRs, etc.).

**Resolution:** v2.1 amends Phase 3's halt threshold:
- The 1% halt was based on a normal-day drift estimate (~46 tickers per the v1 baseline).
- A 43.7% delete-set indicates the producer is missing the `A ∩ P` intersection step OR Alpaca's universe has materially shrunk recently. **Operator review mandatory** before any DELETE commits. The dry_run=True default in PR #320 holds; do NOT bypass.
- v2.1 adds a Phase 3.5 dependency: the `parent_resolver` (§1.8 below) must be live before `classify_tickers` runs in DELETE mode against an FK-protected parent. Otherwise the 6,083 DELETEs that hit live children will fail loud-and-immediate via ON DELETE RESTRICT and the operator has no automated path to resolve them. With `parent_resolver` live, the unresolvable subset shrinks and the remainder gets explicit Path-A backfill.

### 1.8 NEW — Phase 0.5: `db_snapshots/` per-table COPY-to-CSV.gz baseline (Task #22)

Per `feedback_complete_concern_map_first.md` item 6 (Rollback / snapshot substrate) and operator's mid-execution callout, v2's plan had NO pre-cleanup substrate for recovering bad Phase 4 cleanup decisions. Task #22 description (already in tracker) captures the design:

- Per-table daily COPY-to-CSV.gz snapshots at `data/db_snapshots/<table>/<utc_stamp>.csv.gz`.
- Manifest.json with row counts + sha256 + alembic revision.
- Daily cron at 22:00 UTC (after Tier 1/Tier 2 refresh).
- 30-day retention.
- ~1–2.5 GB total disk; manageable locally on operator's Mac; later moves to R3 object-storage bucket per `project_railway_archive_substrate_migration.md`.

**Resolution:** v2.1 inserts this as Phase 0.5, blocking Phase 4 (any cleanup). See plan v2.1 §2.5.

### 1.9 NEW — Phase 0.6: `pg_dump` daily backup regimen

Per `feedback_complete_concern_map_first.md` item 7 (Backup / disaster recovery) and operator's mid-execution callout, v2 had no tenant-loss recovery story beyond Supabase Pro's 7-day PITR.

- `pg_dump --format=custom --compress=9 --schema=platform` at 22:00 UTC daily.
- Upload to S3 (or R3) via the same object-storage backend as `db_snapshots`.
- 30-day retention (~18 GB cumulative at ~$5/year storage on AWS S3 standard).
- Complements Supabase Pro's 7-day PITR (which is for fast-recovery; the dump is for tenant-loss recovery — Supabase project deletion, account compromise, region failure).
- Restore protocol: `pg_restore --schema=platform --no-owner --no-privileges --dbname=<new-conn>`.
- Recovery test quarterly: restore the latest dump into a throwaway Supabase project, smoke-query `prices_daily` row count, drop the project.

**Resolution:** v2.1 inserts this as Phase 0.6, independent of cleanup. Can land in parallel with Phase 0.5. See plan v2.1 §2.6.

### 1.10 NEW — Phase 3.5: `parent_resolver` build (Task #24)

Per `feedback_complete_concern_map_first.md` item 4 (Migration safety) + item 9 (Ongoing operations), v2 prescribed `ON DELETE RESTRICT` loud-fail as the producer-side handling for orphan-creating writes. That handles the symptom (loud-fail), not the cause (the producer wrote a ticker that wasn't in `ticker_classifications` and should be). Task #24 description (already in tracker) captures the design:

- New module `tpcore/ingestion/parent_resolver.py`.
- Pre-INSERT sentinel inside every handler in `tpcore/ingestion/handlers.py`:
  ```python
  unknown = set(incoming.ticker) - set(ticker_classifications.ticker)
  if unknown:
      resolved = await parent_resolver.resolve(unknown)
      # resolved tickers get auto-INSERTed into ticker_classifications
      # unresolvable tickers logged as INGEST_ORPHAN_BLOCKED + dropped from batch
  ```
- Auto-resolve via vendor profile/reference endpoints:
  - FMP `/stable/profile` (primary; has country, asset_class, exchange).
  - Alpaca `/v2/assets` (fallback for active US-listed).
  - SEC company-tickers JSON (fallback for CIK-based resolution).
- Unresolvable tickers logged as `event_type='INGEST_ORPHAN_BLOCKED'` in `application_log` + dropped from batch.

**Resolution:** v2.1 inserts this as Phase 3.5, lands BEFORE Phase 4 backfill (the resolver is the engine that does the backfill for every other table downstream of `prices_daily`). See plan v2.1 §5.5.

### 1.11 REVISED — Phase 4 orphan-protocol per table

v2's plan §6.1 had every table at Path A/B/C as a recommendation. v2.1 reclassifies based on Phase 0 baseline + expert 2026-05-23 verdict:

| Table | Orphan rows | Orphan tickers | Path | Rationale |
|---|---:|---:|---|---|
| `prices_daily` | 335,159 | 166 | **A (BACKFILL)** | Real market history; backfill `ticker_classifications` via `parent_resolver` |
| `corporate_actions` | 1,506 | 69 | **A (BACKFILL)** | Alpaca canonical; tickers are real corporate-action subjects |
| `fundamentals_quarterly` | 135 | 8 | **A (BACKFILL)** | FMP canonical; tickers are real reporting entities |
| `spread_observations` | 33 | 8 | **B (DELETE)** | Derived from prices_daily; rebuildable post-backfill |
| `earnings_events` | 12 | 1 | **A (BACKFILL)** | Real upcoming/past earnings; 1 ticker |
| `liquidity_tiers` | 8 | 8 | **B (DELETE)** | Derived from prices_daily + spread_observations; rebuildable |
| `short_interest` | 3 | 1 | **A (BACKFILL)** | FINRA canonical |
| `universe_candidates` | 1 | 1 | **B (DELETE)** | Engine output; rebuildable on next engine run |
| `sec_insider_transactions` | 0 | 0 | — | already clean (renamed to `insider_transactions`) |
| `sec_material_events` | 0 | 0 | — | already clean |
| `borrow_rates` | 0 | 0 | — | already clean |
| `social_sentiment` | 0 | 0 | — | already clean |
| `options_max_pain` | 0 | 0 | — | already clean |
| `insider_sentiment` | 0 | 0 | — | already clean |

**Path-A backfill template** for tables marked above (executes via `parent_resolver` from Phase 3.5, not via the raw `INSERT … SELECT DISTINCT` from v2 plan §6.3):

```sql
-- For each orphan ticker T in <child>:
--   parent_resolver.resolve(T) → vendor lookup
--   On success: INSERT INTO ticker_classifications (ticker, source, asset_class, country, status, last_updated)
--               VALUES (T, 'phase4_backfill', <vendor>, <vendor>, 'delisted_historical', now())
--   On failure: log INGEST_ORPHAN_BLOCKED + leave the orphan child rows
--               (operator decides Path-B/C in a follow-up)
```

After backfill, the table's orphan_count should drop to (orphan_tickers_unresolvable). VALIDATE proceeds only when orphan_count = 0; unresolvable-orphan rows get explicit Path-B in a follow-up PR with operator sign-off per ticker.

### 1.12 Re-sequenced phase order

v2's sequence: Phase 0 → 1 → 2 → 3 → 4 → 5.

**v2.1 sequence:**
1. **Phase 0** — pre-flight audit + FK-column index migration. DONE PR #317.
2. **Phase 0.5** — `db_snapshots/` build. NEW. Builds before any cleanup so a pre-state baseline exists.
3. **Phase 0.6** — `pg_dump` regimen. NEW. Independent of cleanup; lands in parallel with Phase 0.5.
4. **Phase 1** — rename + `country` column + FMP backfill. DONE PR #318.
5. **Phase 2** — 14 FKs NOT VALID + drop sec_insider_transactions view. DONE PR #319 (note 14, not 15 — `insider_mspr_daily` excluded per §1.1).
6. **Phase 3** — classify_tickers dry_run=True default. IN FLIGHT PR #320.
7. **Phase 3.5** — `parent_resolver` build. NEW. Lands before Phase 4 backfill.
8. **Phase 4** — per-table backfill-then-VALIDATE. REVISED: backfill not delete.
9. **Phase 5** — verification + ongoing-ops sentinel + drop compatibility view (already done in Phase 2 per PR #319) + add country CHECK.

---

## 2. Concern-map checklist (REQUIRED by `feedback_complete_concern_map_first.md`)

Each of the 12 standard concerns gets a one-line answer. No "no clear answer" — block-condition met.

| # | Concern | Coverage |
|---|---|---|
| 1 | **Schema changes** — DDL, new constraints, changes to existing | Covered: Phase 1 (rename + country column + CHECK + partial index), Phase 2 (14 FKs NOT VALID), Phase 4 (per-table VALIDATE), Phase 5 (drop view + add country CHECK). |
| 2 | **Producer changes** — every handler/script/stage writing to affected tables | Covered: Phase 1 (rename grep sweep across `tpcore/sec/`, `tpcore/ingestion/handlers.py`, `tpcore/audit/cross_table.py`, `scripts/ops.py`); Phase 3 (classify_tickers DELETE-source-tracking + `A ∩ P` filter); Phase 3.5 (`parent_resolver` integrated into every handler). |
| 3 | **Consumer changes** — every engine/check/dashboard reading from affected tables | Covered: Phase 1 same grep sweep covers reads; the compat view (dropped in Phase 2 per #319) handled any lagging readers during the rename window. Phase 5 verifies `tpcore/auditheal` cross-table-audit reads all-green. |
| 4 | **Migration safety** — NOT VALID, statement_timeout, lock budgets, transaction shape | Covered: v2 spec §5 (NOT-VALID-FIRST), §6 (index audit), §9 (verification gates); v2.1 §1.5 (timeout raise to 30min before Phase 4 prices_daily VALIDATE — operator dashboard action). Cleanup + VALIDATE in SEPARATE migrations per v2 §5.2. |
| 5 | **Data quality concerns** — orphan handling, duplicate handling, NULL tolerance | Covered: v2.1 §1.11 per-table orphan path matrix (BACKFILL default for real-history tables, DELETE for derived/junk); v2 §12 country null-rate honest-log (no hard threshold). |
| 6 | **Rollback / snapshot substrate** — pre-state baseline, recovery if wrong | Covered: Phase 0.5 (`db_snapshots/` per-table COPY-to-CSV.gz daily with 30-day retention). Per-PR Alembic round-trip per v2 §9.5. |
| 7 | **Backup / disaster recovery** — backup story; tenant-loss recovery path | Covered: Phase 0.6 (`pg_dump` daily to S3/R3 with `pg_restore` protocol + quarterly recovery test). Complements Supabase Pro 7-day PITR. |
| 8 | **Test coverage** — what tests pin invariants; what existing tests need updating | Covered: v2 §8.1–§8.7 (constraint-presence, DELETE-RESTRICT, INSERT-violation, cross-table-orphan sentinel, source-tag CHECK, country-format CHECK, NOT-VALID-still-enforces); v2.1 §8.8 NEW (every handler that catches `ForeignKeyViolationError` MUST import asyncpg at module level + have FK-violation-path unit test). |
| 9 | **Ongoing operations** — daily/weekly stages that hit affected tables; how do they cope post-change | Covered: Phase 3.5 (`parent_resolver` integrated into every handler in `tpcore/ingestion/handlers.py`); operator daily-cycle order unchanged (classify_tickers first; derived populators after per memory `feedback_data_update_first`). |
| 10 | **Documentation** — DATABASE_AND_DATAFLOW, runbooks, memory entries that need updating | Covered: Phase 5 (`docs/DATABASE_AND_DATAFLOW.md` §2.1 ERD + §2.2 note); memory item `project_database_architecture_state_2026_05_23.md` outstanding-debt closes; Task #22/#24 statuses transition to DONE; v2 and v2.1 spec/plan docs themselves are the long-form documentation. |
| 11 | **Cross-table change ordering** — order minimizes blast radius | Covered: v2.1 §1.12 re-sequenced order; Phase 4 light → heavy (prices_daily last). Phase 0.5/0.6 land before any cleanup. Phase 3.5 before Phase 4. |
| 12 | **Operator manual actions** — Supabase dashboard tweaks, env-var changes | Covered: (a) raise `statement_timeout` to 30min via Supabase dashboard before Phase 4 prices_daily VALIDATE (v2.1 §1.5); (b) provision S3 bucket + IAM for Phase 0.6 pg_dump uploads (or use existing R3 bucket per `project_railway_archive_substrate_migration.md`); (c) confirm read-replica state for v2 §9.4 propagation check; (d) per-table operator sign-off on Phase 4 backfill protocol per v2.1 §1.11. |

**All 12 concerns have a clear coverage line. Spec is ready to ship.**

---

## 3. Scope (REVISED from v1 §3 / v2 §3 — in-scope drops 15 → 14)

### 3.1 In-scope tables (14)

Inherits v1 §3.1 with **`insider_mspr_daily` removed** (it's a VIEW; see §1.1). Final list:

1. `prices_daily`
2. `insider_transactions` (renamed from `sec_insider_transactions` in Phase 1)
3. `sec_material_events`
4. `corporate_actions`
5. `earnings_events`
6. `fundamentals_quarterly`
7. `short_interest`
8. `borrow_rates`
9. `social_sentiment`
10. `options_max_pain` (FK column: `symbol`)
11. `insider_sentiment` (FK column: `symbol`)
12. `liquidity_tiers`
13. `spread_observations`
14. `universe_candidates`

### 3.2 Out-of-scope (REVISED — adds `insider_mspr_daily`)

Inherits v1 §3.2 + adds `insider_mspr_daily` (it's a VIEW; referential integrity inherited from base table `insider_transactions`).

### 3.3 Edge-case operator-decision tables (UNCHANGED from v1 §3.3)

`open_orders` and `tradier_options_chains` per v1 §3.3.

---

## 4. Non-goals (UNCHANGED from v1 §4 / v2 §4)

Out-of-scope items 1–9 in v1 §4 stand. v2's three reiterations stand (macro consolidation Task #18, Phase 2 denormalization Task #17, per-country insider adapters Task #15).

---

## 5. NOT-VALID-FIRST pattern (UNCHANGED from v2 §5)

v2 §5.1–§5.4 unchanged. Pattern: bulk `ADD CONSTRAINT … NOT VALID` ships in ONE Phase 2 migration; per-table `VALIDATE CONSTRAINT` in SEPARATE Phase 4 migrations (never bundled with cleanup). DONE PR #319.

---

## 6. Pre-FK index audit (UNCHANGED from v2 §6)

DONE PR #317. `universe_candidates(ticker)` index added; all other 13 in-scope tables already covered.

---

## 7. Updated phase sequence (REVISED from v2 §7 — adds Phase 0.5, 0.6, 3.5)

### Phase 0 — Pre-flight audit + index audit + statement_timeout verification

DONE PR #317. Deliverables landed:
- `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md`
- `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md`
- `docs/superpowers/audits/2026-05-23-referential-integrity-timeout-locks-baseline.md`
- `platform/migrations/versions/20260523_0500_idx_concurrently_universe_candidates_ticker.py` (applied to live DB).

### Phase 0.5 — `db_snapshots/` build (NEW; Task #22)

Per §1.8 above. Provides pre-cleanup rollback baseline for Phase 4.

**Deliverables:**
- New module: `scripts/db_snapshots.py` (or equivalent stage in `scripts/ops.py`).
- Per-table COPY-to-CSV.gz daily into `data/db_snapshots/<table>/<utc_stamp>.csv.gz`.
- Manifest.json with row counts + sha256 + alembic revision.
- Daily launchd plist at 22:00 UTC (Manila 06:00 next-day).
- 30-day retention via age-based prune.
- Disk budget: ~1–2.5 GB total (verified against today's row counts).
- Future R3 object-storage upload integration deferred to follow-up; local-disk first.

**Exit gate:**
- One full successful daily snapshot run captured.
- Restore-test: pick one of `prices_daily` / `insider_transactions` / `ticker_classifications`; load the latest CSV.gz into a temp table; row count matches manifest; sha256 matches manifest.
- launchd plist installed + running.

**Blocks:** Phase 4 (any cleanup).

### Phase 0.6 — `pg_dump` daily backup regimen (NEW)

Per §1.9 above. Tenant-loss disaster-recovery path.

**Deliverables:**
- New script: `scripts/pg_dump_daily.sh` (or equivalent stage).
- Daily `pg_dump --format=custom --compress=9 --schema=platform --no-owner --no-privileges` at 22:00 UTC.
- Upload to S3 bucket `s3://<operator-bucket>/short-term-trading-engine/pg_dumps/<utc_date>.dump` OR existing R3 bucket per `project_railway_archive_substrate_migration.md`.
- 30-day retention via S3 lifecycle policy (~18 GB cumulative; ~$5/year on S3 standard).
- Restore protocol documented in `docs/runbooks/database_disaster_recovery.md`:
  ```bash
  # 1. Provision throwaway Supabase project; capture DATABASE_URL.
  # 2. aws s3 cp s3://<bucket>/short-term-trading-engine/pg_dumps/<date>.dump /tmp/
  # 3. pg_restore --schema=platform --no-owner --no-privileges --dbname=<new-url> /tmp/<date>.dump
  # 4. Smoke: SELECT COUNT(*) FROM platform.prices_daily;
  # 5. Drop the throwaway project.
  ```
- Quarterly recovery-test calendar entry.

**Exit gate:**
- One full successful daily dump uploaded.
- Restore-test: dump downloaded; `pg_restore --list` enumerates expected tables; smoke restore of a single table into a temp database succeeds.
- launchd plist installed + running.

**Independent of Phase 0.5** — can land in parallel.

### Phase 1 — Rename + country column + FMP backfill

DONE PR #318. Migrations + producer change landed:
- `sec_insider_transactions` → `insider_transactions` + `source` column + CHECK + compat view (view dropped in Phase 2 per PR #319).
- `country char(2)` column on `ticker_classifications` + partial index `WHERE country IS NOT NULL`.
- FMP `/stable/profile` per-ticker backfill running (88% complete; null-rates per §1.3 above).

### Phase 2 — 14 FKs NOT VALID + drop compat view

DONE PR #319. Migration landed adding `FOREIGN KEY … NOT VALID` to all 14 in-scope tables (15 minus `insider_mspr_daily`). The `sec_insider_transactions` compatibility view was dropped in the same PR (the auto-rewrite finding per §1.2 meant the view was not providing loud-fail and was no longer worth keeping).

### Phase 3 — classify_tickers DELETE-source-tracking + `A ∩ P` filter (dry_run=True default)

IN FLIGHT PR #320. Producer change ships with `dry_run=True` as the default to surface the 6,083-ticker delete-set per §1.7 without committing any DELETEs. Live DELETE mode unblocks AFTER Phase 3.5 lands.

**Exit gate (REVISED from v2 §7 Phase 3):**
- Producer unit tests green.
- Dry-run produces audit row in `application_log` with full delete-set captured.
- Operator reviews the delete-set with Phase 3.5 (`parent_resolver`) capabilities in mind — many of the 6,083 should auto-resolve once `parent_resolver` is live and re-runs through the `A ∩ P` filter post-resolution.
- Live DELETE mode DOES NOT execute until Phase 3.5 lands.

### Phase 3.5 — `parent_resolver` build (NEW; Task #24)

Per §1.10 above. Auto-resolution layer for handler-side FK-violation handling.

**Deliverables:**
- New module: `tpcore/ingestion/parent_resolver.py`.
- Integration: every handler in `tpcore/ingestion/handlers.py` pre-INSERT-sentinel-checks against `ticker_classifications`.
- Vendor fallback chain: FMP `/stable/profile` → Alpaca `/v2/assets` → SEC company-tickers JSON.
- Unresolvable tickers logged as `INGEST_ORPHAN_BLOCKED` in `application_log` + dropped from batch.
- Unit tests cover all three vendor paths + the unresolvable path.

**Exit gate:**
- All unit tests green.
- Integration smoke: one ingest cycle (`daily_bars` or `corporate_actions`) runs cleanly with a synthesized unknown-ticker; the resolver auto-resolves it; the row lands; no FK violation surfaces.
- `application_log` shows the auto-resolution event.

**Blocks:** Phase 4 (per-table backfill cleanup uses `parent_resolver` as the engine).

### Phase 4 — Per-table BACKFILL-then-VALIDATE (REVISED — backfill not delete)

REVISED per §1.11 above. For each in-scope table T with `orphan_count > 0`:

1. **Per-table operator decision** — A (BACKFILL via `parent_resolver`) or B (DELETE) per §1.11 matrix.
2. **Cleanup migration** (BACKFILL or DELETE; per spec §11 `WHERE NOT EXISTS` template).
3. **Separate VALIDATE CONSTRAINT migration** per v2 §5.2.
4. **Verification gates per v2 §9.**

**Pre-Phase-4 dependencies (REVISED):**
- Phase 0.5 `db_snapshots/` live with at least one daily snapshot captured.
- Phase 0.6 `pg_dump` regimen live with at least one daily dump captured.
- Phase 3.5 `parent_resolver` live.
- `statement_timeout` raised to 30min via Supabase dashboard (operator action).

**Ordering recommendation (light → heavy, UNCHANGED from v2 §7 Phase 4):**

1. `universe_candidates` (1 orphan; Path B — engine output rebuildable)
2. `short_interest` (3 orphans; Path A — FINRA canonical)
3. `liquidity_tiers` (8 orphans; Path B — derived rebuildable)
4. `earnings_events` (12 orphans; Path A — real earnings)
5. `spread_observations` (33 orphans; Path B — derived rebuildable)
6. `fundamentals_quarterly` (135 orphans; Path A — FMP canonical)
7. `corporate_actions` (1,506 orphans; Path A — Alpaca canonical)
8. `prices_daily` (335,159 orphans; **Path A — real market history**; 166 ticker `parent_resolver` resolutions expected)

**prices_daily special handling (REVISED from v2 §6.8):**
- Schedule during local 04:00–08:00 UTC.
- Run `parent_resolver` over the 166 distinct orphan tickers FIRST. Expected: most resolve via FMP `/stable/profile` (delisted-but-historical-data-available). Unresolvable tickers get `status='delisted_historical_unresolvable'` minimal classification with `source='phase4_backfill'`.
- After backfill, orphan_count should be 0.
- Then VALIDATE CONSTRAINT on prices_daily (under raised 30min statement_timeout).

### Phase 5 — Post-FK verification + ongoing-ops sentinel + country CHECK

REVISED from v2 §7 Phase 5 — drop-view is already done (Phase 2 per #319); Phase 5 now focuses on:

- Verification per v2 §15 acceptance criteria 1–13.
- Add country CHECK constraint (`country IS NULL OR country ~ '^[A-Z]{2}$'`).
- Ongoing-ops sentinel: nightly cross-table-audit check via `tpcore/auditheal` reads all-green; new orphan in any table fires `DATA_INTEGRITY_BREACH` event in `application_log`.
- ERD + memory updates per v2 §7 Phase 5.

---

## 8. Test contracts (REVISED — adds §8.8)

§8.1–§8.7 inherited from v2 §8 verbatim.

### 8.8 Handler-side FK-violation-path test (NEW per §1.6)

Every handler in `tpcore/ingestion/handlers.py` that catches `asyncpg.ForeignKeyViolationError` MUST:

- Import `asyncpg` at module level (NOT under `TYPE_CHECKING`).
- Have a unit test simulating an orphan-ticker INSERT and asserting:
  - `asyncpg.ForeignKeyViolationError` is caught.
  - The row is NOT silently swallowed — either `parent_resolver.resolve(...)` is invoked OR an `INGEST_ORPHAN_BLOCKED` event lands in `application_log`.
  - No `NameError` or `AttributeError` surfaces from the except block (regression test for the PR #320 first-run crash).

Test location: `tpcore/tests/test_referential_integrity.py` (alongside §8.1–§8.7 contracts).

---

## 9. Migration safety + verification gates (UNCHANGED from v2 §9)

§9.1–§9.6 unchanged. v2.1 §1.5 makes the §9.1 statement_timeout dashboard-raise an explicit operator-action gate before Phase 4 prices_daily VALIDATE.

---

## 10. Compatibility-view pattern (REVISED per §1.2)

v2 §10.3's "loud-fail-on-INSERT" claim was wrong for simple `WHERE source = X` views — Postgres auto-rewrites them as updatable. v2.1 amends:

### 10.1 For future renames where loud-fail-on-write is needed

Two options:

(a) **TRIGGER-based loud-fail:**
```sql
CREATE OR REPLACE FUNCTION platform.deprecated_view_loud_fail()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Writes to platform.% are deprecated; write to platform.<new_name> instead', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE VIEW platform.old_name AS SELECT * FROM platform.new_name;
CREATE TRIGGER tr_old_name_loud_fail
    INSTEAD OF INSERT OR UPDATE OR DELETE ON platform.old_name
    FOR EACH ROW EXECUTE FUNCTION platform.deprecated_view_loud_fail();
```

(b) **Drop without replacement** — accept `relation "<old_name>" does not exist` as the loud-fail signal; rely on grep-sweep to catch missed call-sites. This is what Phase 2 PR #319 effectively chose for `sec_insider_transactions`.

### 10.2 v2.1 retroactive note on PR #319

The Phase 2 PR's choice to drop the compat view (rather than retain it with a TRIGGER) was correct given §1.2's finding. The view was providing no detection benefit; the loud-fail mechanism is now the missing-relation error from option (b).

---

## 11. Cleanup-template `ctid` fix (UNCHANGED from v2 §11)

v2 §11 mandates `WHERE NOT EXISTS` over `ctid`. Unchanged. v2.1 §1.11 Path-A backfill uses the same MVCC-safe template (via `parent_resolver`'s INSERT … ON CONFLICT DO NOTHING).

---

## 12. Country-backfill null-tolerance (REVISED per §1.3)

v2 §12 assumed Alpaca was the source; the real source is FMP `/stable/profile` per-ticker. v2.1 documents the measured null-rates (88% complete state at v2.1 writing):

| Asset class | Null-rate |
|---|---:|
| stock | 4.8% |
| etf | 13.8% |
| spac | 19.0% |
| fund | 33.3% |

These are the operator-accepted honest numbers. No hard threshold. CHECK constraint (`country IS NULL OR country ~ '^[A-Z]{2}$'`) ships in Phase 5 once null-rate stabilizes.

Phase 1.5 follow-up backfill sources from v2 §12.2 (Polygon `/v3/reference/tickers`, OpenFIGI) remain available as ENRICHMENT options if Task #15 needs ETF/SPAC/fund tightening; not blocking.

---

## 13. Updated dependency graph (REVISED per v2.1 §1.12)

```text
Phase 0 (DONE PR #317)
    ├─→ Phase 0.5 (db_snapshots — NEW)
    │       └──┐
    ├─→ Phase 0.6 (pg_dump regimen — NEW)
    │       └──┐
    └─→ Phase 1 (DONE PR #318)
            └─→ Phase 2 (DONE PR #319; 14 FKs NOT VALID + compat view dropped)
                └─→ Phase 3 (IN FLIGHT PR #320; dry_run=True default)
                    └─→ Phase 3.5 (parent_resolver — NEW; Task #24)
                        └─→ Phase 4 (BACKFILL-then-VALIDATE rolling per-table)
                            │   Pre-deps: Phase 0.5 + 0.6 live;
                            │             statement_timeout raised to 30min
                            └─→ Phase 5 (verification + country CHECK + sentinel)
```

Phase 0.5 and Phase 0.6 can land in parallel with each other. Phase 3.5 is a strict gate before Phase 4.

---

## 14. Updated risk register (REVISED from v2 §14 — adds R18-v2.1 through R22-v2.1)

Inherits v2 §14 verbatim. v2.1 adds:

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| **R18-v2.1** | `parent_resolver` vendor lookup hits rate limit / fails / returns wrong data; an orphan ticker gets a bogus `ticker_classifications` row | 3.5, 4 | Vendor fallback chain (FMP → Alpaca → SEC); unresolvable tickers log `INGEST_ORPHAN_BLOCKED` and STAY orphan rather than getting bogus parents; quarterly review of `phase4_backfill`-sourced rows in `ticker_classifications` |
| **R19-v2.1** | `db_snapshots/` disk consumption grows beyond expected ~2.5 GB | 0.5 | Daily size monitoring; 30-day retention prune; alert if `data/db_snapshots/` exceeds 5 GB |
| **R20-v2.1** | `pg_dump` upload to S3 fails silently (network, IAM, bucket lifecycle) | 0.6 | Upload returns checksum confirmation; daily dump completion writes `BACKUP_COMPLETE` event to `application_log`; missing event = alert |
| **R21-v2.1** | Phase 4 prices_daily VALIDATE crosses 30min ceiling even after backfill | 4 | If VALIDATE fails: constraint stays NOT VALID (new rows still protected); re-run cleanup for any missed orphan; raise dashboard ceiling further if needed (operator decision); no downgrade required |
| **R22-v2.1** | A new in-scope table emerges between v2.1 and Phase 4 final (vendor adds table; new producer ships) | 4, 5 | Phase 5 reconciles `\dt platform.*` against §3.1 in-scope list; any new table needing FK gets a follow-up PR |

**Risks unchanged from v2:** R1–R13 (v1 inheritance), R14-v2 through R17-v2.

**Risks removed in v2 (still removed in v2.1):** R5 (producer-race window), R10 (cleanup-precondition fragility).

---

## 15. Acceptance criteria — v2.1 Phase 1 is "done" when

Inherits v2 §15 criteria 1–13. v2.1 adds:

14. **Phase 0.5 `db_snapshots/` live** with at least one successful daily snapshot run + restore-test passed.
15. **Phase 0.6 `pg_dump` regimen live** with at least one successful daily dump + restore-test passed + quarterly recovery calendar entry.
16. **Phase 3.5 `parent_resolver` live** with unit tests green for all three vendor paths + unresolvable path + integration smoke pass.
17. **Concern-map checklist (§2 above) re-verified** post-rollout — every item still has a clear coverage line.
18. **Handler FK-violation-path tests (§8.8) green** for every handler in `tpcore/ingestion/handlers.py` catching `asyncpg.ForeignKeyViolationError`.

---

## 16. Open questions (REVISED from v2 §16)

v2 §16 questions 1–3 (read-replica state, statement_timeout role-cap, country-backfill source) are RESOLVED:
- Read-replica: no replica provisioned today; §9.4 replica check is a no-op confirmation that `pg_stat_replication` is empty.
- statement_timeout: 120s today; raise to 30min via Supabase dashboard before Phase 4 prices_daily VALIDATE (operator action documented in §1.5).
- Country-backfill source: FMP `/stable/profile` per-ticker per §1.3; 88% complete at v2.1 writing.

v2.1 adds:

1. **Phase 0.6 S3 vs R3 choice** — does the operator want pg_dumps in a dedicated S3 bucket OR reuse the existing R3 bucket per `project_railway_archive_substrate_migration.md`? (Recommendation: R3 reuse — single bucket, single IAM, single lifecycle policy.)
2. **Phase 3.5 `parent_resolver` triage of the 6,083 classify_tickers delete-set** — once resolver lands, what fraction of the delete-set auto-resolves (preferred shares, foreign ADRs, etc.) vs. genuinely needs DELETE (delisted-and-bars-already-gone)? Run the resolver against the dry-run delete-set as a Phase 3.5 sub-deliverable.
3. **Phase 4 prices_daily 166-orphan-ticker resolution rate** — what fraction of the 166 distinct orphan tickers FMP `/stable/profile` recognizes vs. unresolvable (truly old delisted tickers FMP no longer indexes)? Captured as a Phase 4 audit row pre-VALIDATE.

---

## 17. References

- **v2 of this spec (superseded by v2.1; preserved as historical record):** `docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.md`
- **v1 of this spec (superseded by v2; preserved):** `docs/superpowers/specs/2026-05-23-referential-integrity-design.md`
- **v2.1 implementation plan:** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.1.md`
- **v2 implementation plan (superseded by v2.1 plan; preserved):** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan-v2.md`
- **Operating contract:** `.claude/agents/db-architect.md`
- **Concern-map memory:** `feedback_complete_concern_map_first.md`
- **Schema state memory:** `project_database_architecture_state_2026_05_23.md`
- **Archive substrate memory:** `project_railway_archive_substrate_migration.md`
- **Phase 0 audits:**
  - `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md`
  - `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md`
  - `docs/superpowers/audits/2026-05-23-referential-integrity-timeout-locks-baseline.md`
- **Postgres `NOT VALID` + `VALIDATE`:** <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>
- **Postgres updatable views:** <https://www.postgresql.org/docs/current/sql-createview.html#SQL-CREATEVIEW-UPDATABLE-VIEWS>
- **`pg_dump` / `pg_restore`:** <https://www.postgresql.org/docs/current/app-pgdump.html>
- **Supabase Pro tier statement_timeout:** memory `project_supabase_pro_tier.md`
- **Already-merged Phase PRs:** #317 (Phase 0), #318 (Phase 1), #319 (Phase 2).
- **In-flight Phase 3 PR:** #320 (`feat/phase-3-classify-tickers-delete-source-tracking`).
- **Open tasks:** #22 (`db_snapshots/` build → Phase 0.5), #24 (`parent_resolver` build → Phase 3.5).

---

**END OF SPEC v2.1.**
