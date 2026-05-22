# Referential Integrity Implementation Plan — `platform.*` schema

**Author / role:** db-architect (drafted 2026-05-23)
**Status:** PLAN ONLY — no migrations, no code, no tests written. Executable by a future subagent or the operator phase-by-phase without re-investigation.
**Spec basis:**
- `.claude/agents/db-architect.md` (drafted, not yet committed) — operating contract for this work
- memory `project_database_architecture_state_2026_05_23.md` — schema state + invariants
- `docs/superpowers/plans/2026-05-17-audit-driven-referential-remediation.md` (PR #167) — built `tpcore/auditheal` to **detect** orphans; this plan **enforces** at the schema layer
- operator 2026-05-22 *"how the fuck do you design a database with no referential integrity"* + 2026-05-23 *"we need referential integrity on these tables"*

**Goal:** every `ticker`-bearing table in `platform.*` has a real FK to `platform.ticker_classifications(ticker)` with `ON UPDATE CASCADE ON DELETE RESTRICT`. Drift becomes a constraint violation at INSERT time, not an audit-after-the-fact print line.

**Non-goals (this plan):**
- Composite `(ticker, date)` FK chains to `prices_daily` (parent-child between Tier 1 raw tables) — separate follow-up plan; current scope is only the universe-FK fan-out to `ticker_classifications`.
- Tier 2 derived-table refresh-freshness constraints — separate plan (operator-noted "stale upstream silently produces stale downstream" issue).
- RLS / Supabase policies — out of scope.

---

## 0. Ground truth — what we are protecting

### 0.1 Canonical parent

`platform.ticker_classifications` — PK `(ticker)`. ~13,669 rows at 2026-05-14 backfill; +46 drift today (2026-05-23) flagged by classify_tickers producer that doesn't DELETE-source-track. **Cannot be the FK parent until the drift fix lands** (see Phase 0 / Task #11 below).

Per operator 2026-05-23: invariant `ticker_classifications.ticker ⊆ prices_daily.ticker` — classify_tickers must filter Alpaca's asset list to in-prices_daily before upserting. Until that invariant holds, FK additions will be unsafe (rows in raw tables would orphan when classify_tickers prunes).

### 0.2 Child tables in scope (15 ticker-bearing tables)

**Tier 1 (raw, vendor-fed):**
1. `prices_daily`               PK `(ticker, date)`
2. `insider_transactions`       PK `(ticker, filing_date, insider_name, transaction_type, shares)` — **POST-RENAME** from `sec_insider_transactions`
3. `sec_material_events`        PK `(ticker, filing_date, event_type)`
4. `corporate_actions`          unique `(ticker, action_date, action_type)`
5. `earnings_events`            unique `(ticker, event_date)`
6. `fundamentals_quarterly`     PK `(ticker, filing_date)`
7. `short_interest`             PK `(ticker, settlement_date)`
8. `borrow_rates`               PK `(ticker, date)`
9. `social_sentiment`           PK `(ticker, date)`
10. `options_max_pain`          PK `(ticker, expiration_date, observed_date)` — single tracked symbol SPY
11. `insider_sentiment`         PK `(symbol, year, month)` — Finnhub monthly; note column name `symbol` not `ticker`
12. `aaii_sentiment`            PK `(date)` — **NOT in scope; no ticker column** (sentinel exclusion)
13. `tradier_options_chains`    no PK; FROZEN per docs — see §10.3 for handling
14. `spread_observations`       PK `(id)`, `ticker` column
15. `universe_candidates`       PK `(as_of_date, engine, ticker)`

**Tier 2 (derived):**
16. `liquidity_tiers`           PK `(ticker)`
17. `insider_mspr_daily`        (verify column shape during Phase 1 audit; ticker-keyed)

### 0.3 Why no FK exists today

`audit_data_pipeline` + `tpcore/auditheal` (PR #167) detect orphans after the fact and persist `cross_table_audit.<table>.<check_name>` rows to `data_quality_log`. They do **not** add `FOREIGN KEY` clauses. The cross_ref_cleanup stage only deletes from `tradier_options_chains` (the one frozen table). This plan closes the gap.

---

## 1. Phase summary + wall-clock budget

| Phase | Topic | Migrations | Est. wall-clock | Riskiest? |
|---|---|---|---|---|
| **0** | Pre-flight audit — orphan counts for every child table | 0 (read-only SQL) | 30 min | — |
| **1** | Rename `sec_insider_transactions` → `insider_transactions` | 1 | 20 min | low |
| **2** | Add `country char(2)` to `ticker_classifications` | 1 + producer change | 1 hr (incl. Alpaca backfill) | low |
| **3** | classify_tickers DELETE-source-tracking + `⊆ prices_daily` filter | 0 schema; 1 producer-code PR | 2 hr | medium (producer logic) |
| **4** | Verify drift = 0 + invariant holds | 0 (read-only verification gate) | 15 min | — |
| **5** | Cleanup orphans per child table (per-table decision) | 1–N (one per remediation class) | 2–6 hr total (depends on orphan volume) | medium |
| **6** | FK additions — **light** tables (low row-count, low orphan risk) | 5 migrations (one per table, batched into 1 PR) | 1 hr | low |
| **7** | FK additions — **medium** tables (Tier 1, 10K–1M rows) | 6 migrations | 2 hr | medium |
| **8** | FK additions — **heavy** table `prices_daily` (~20.6M rows) | 1 migration | 3–6 hr (`NOT VALID` + `VALIDATE CONSTRAINT` strategy) | **HIGHEST** |
| **9** | FK additions — Tier 2 derived tables (`liquidity_tiers`, `insider_mspr_daily`, `universe_candidates`) | 1 migration | 30 min | low |
| **10** | Post-FK verification — DELETE-RESTRICT smoke test + producer regression sweep | 0 schema; 1 verification PR | 1 hr | low |

**Total estimated wall-clock budget: 12–20 hours of focused work** spread across 10 sequenced PRs. **Each phase is gated** — Phase N+1 cannot start until Phase N's verification gate is green.

---

## 2. Phase 0 — Pre-flight orphan audit (READ-ONLY)

**Goal:** produce a per-table orphan count baseline before any change. No mutations. Numbers go into the PR body of subsequent phases as evidence.

### 2.1 Orphan-audit query template

For every child table T in §0.2 with a `ticker` column (excluding `aaii_sentiment`):

```sql
-- Orphan count: rows in T whose ticker is not in ticker_classifications
SELECT
    '<T>' AS child_table,
    COUNT(*) AS orphan_count,
    COUNT(DISTINCT c.ticker) AS distinct_orphan_tickers
FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.ticker
);
```

For `insider_sentiment` (column is `symbol`):

```sql
SELECT 'insider_sentiment' AS child_table, COUNT(*) AS orphan_count,
       COUNT(DISTINCT c.symbol) AS distinct_orphan_tickers
FROM platform.insider_sentiment c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.symbol
);
```

### 2.2 Run + record

- Execute every query against the live DB (read-only, `ROLE` need not be elevated).
- Persist results to `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md` (operator artefact; not a migration).
- Cross-check against existing `cross_table_audit.*` rows in `data_quality_log` — discrepancies indicate stale audit rows OR a missed predicate (e.g. tradier_options_chains uses `prices_daily_tickers` view).

### 2.3 Exit gate

- Per-table orphan counts captured.
- Distinct-orphan-ticker count captured (drives Phase 5 cleanup volume estimates).
- Any table with > 1% orphan rate is flagged for **per-table operator decision** in Phase 5 (delete? backfill ticker_classifications? rename ticker?).

---

## 3. Phase 1 — Rename `sec_insider_transactions` → `insider_transactions`

**Why first:** operator 2026-05-23 wants the FMP non-US insider feed (Task #15) to land in the same table with a `source` column (`'sec'` / `'fmp'`). Renaming once now is cheaper than dual-name FK later. Per memory `database_architecture_state_2026_05_23.md`: *"should have been ONE `insider_transactions` table with `source IN ('sec', 'fmp')`"*.

### 3.1 Migration template

File: `platform/migrations/versions/20260523_1000_rename_sec_insider_transactions.py`

```text
upgrade():
    op.execute("ALTER TABLE platform.sec_insider_transactions RENAME TO insider_transactions")
    -- rename indexes if they embed the old name:
    op.execute("ALTER INDEX IF EXISTS platform.sec_insider_transactions_pkey RENAME TO insider_transactions_pkey")
    op.execute("ALTER INDEX IF EXISTS platform.idx_sec_insider_ticker_date RENAME TO idx_insider_transactions_ticker_date")
    -- add source column with default 'sec' (current sole producer); not-null after backfill:
    op.add_column("insider_transactions",
        sa.Column("source", sa.Text(), nullable=True, server_default="sec"),
        schema="platform")
    op.execute("UPDATE platform.insider_transactions SET source = 'sec' WHERE source IS NULL")
    op.alter_column("insider_transactions", "source", nullable=False, schema="platform")
    op.create_check_constraint("ck_insider_transactions_source",
        "insider_transactions", "source IN ('sec', 'fmp')",
        schema="platform")

downgrade():
    op.drop_constraint("ck_insider_transactions_source",
        "insider_transactions", schema="platform")
    op.drop_column("insider_transactions", "source", schema="platform")
    op.execute("ALTER INDEX IF EXISTS platform.insider_transactions_pkey RENAME TO sec_insider_transactions_pkey")
    op.execute("ALTER INDEX IF EXISTS platform.idx_insider_transactions_ticker_date RENAME TO idx_sec_insider_ticker_date")
    op.execute("ALTER TABLE platform.insider_transactions RENAME TO sec_insider_transactions")
```

### 3.2 Producer code updates (same PR, no separate migration)

- `tpcore/sec/edgar_adapter.py` — INSERT statement table name + `source='sec'` in row tuple.
- `tpcore/ingestion/handlers.py` — `handle_sec_filings`.
- `tpcore/audit/cross_table.py` — `CROSS_TABLE_CHECKS` table-name reference if any.
- All grep-hits for `sec_insider_transactions` across codebase (`grep -r 'sec_insider_transactions' --include='*.py'`).
- `docs/DATABASE_AND_DATAFLOW.md` §2.2 — section heading + body.

### 3.3 Exit gate

- Migration round-trip green (`alembic upgrade head` + `downgrade -1` + `upgrade head`).
- `python -m pytest tpcore/tests/test_cross_table_audit.py -q` green (predicate intact).
- `python -m tpcore.scripts.check_imports tpcore ops reversion vector momentum sentinel canary catalyst carver` green.
- A read-against-live `SELECT COUNT(*), source FROM platform.insider_transactions GROUP BY source` returns the expected SEC row count with `source='sec'`.

---

## 4. Phase 2 — Add `country char(2)` to `ticker_classifications`

**Why now (before FK):** operator 2026-05-23 approved expert recommendation. Needed for Task #15 partitioning (`country='US'` → SEC canonical, `country IN ('GB','DE',…)` → FMP fallback). Adding it before FK rollout means downstream FK additions in Phase 6+ don't have to be re-migrated.

### 4.1 Migration template

File: `platform/migrations/versions/20260523_1100_ticker_classifications_country.py`

```text
upgrade():
    op.add_column("ticker_classifications",
        sa.Column("country", sa.CHAR(length=2), nullable=True),
        schema="platform")
    op.create_check_constraint("ck_ticker_classifications_country_iso",
        "ticker_classifications",
        "country IS NULL OR country ~ '^[A-Z]{2}$'",
        schema="platform")
    op.create_index("idx_ticker_classifications_country",
        "ticker_classifications", ["country"], schema="platform",
        postgresql_where=sa.text("country IS NOT NULL"))

downgrade():
    op.drop_index("idx_ticker_classifications_country",
        table_name="ticker_classifications", schema="platform")
    op.drop_constraint("ck_ticker_classifications_country_iso",
        "ticker_classifications", schema="platform")
    op.drop_column("ticker_classifications", "country", schema="platform")
```

### 4.2 Producer update

- `scripts/classify_tickers.py` — pull `country` from Alpaca `/v2/assets` response (`asset['country']` ISO2 if present).
- Backfill same-PR via a one-shot stage invocation: `--stage classify_tickers --param skip_guard_days=0`.
- Per memory: ~231 tickers expected to come up `country != 'US'` (the foreign-issuer ADRs missing from SEC Section 16).

### 4.3 Exit gate

- Schema migration round-trip green.
- Post-backfill row spot-check: `SELECT country, COUNT(*) FROM platform.ticker_classifications GROUP BY country ORDER BY 2 DESC LIMIT 20` returns US-dominant with ~200+ non-US.
- `country IS NULL` count is < 5% of rows (Alpaca occasionally omits country on ETFs/SPACs — log + accept).
- `country` stays nullable for now (NOT NULL gate is a follow-up after backfill stabilises).

---

## 5. Phase 3 — classify_tickers DELETE-source-tracking + ⊆-prices_daily filter

**Why blocking-FK:** without this, adding `ON DELETE RESTRICT` FK to `ticker_classifications` will **break every subsequent classify_tickers run** that tries to clean up orphans — Postgres will RESTRICT the DELETE. Producer must filter UPSTREAM before any FK lands.

### 5.1 Producer change (no schema migration)

- `scripts/classify_tickers.py` / `tpcore/...` (locate during exec):
  1. Pull Alpaca `/v2/assets` set (current source-of-truth universe `A`).
  2. Pull distinct `prices_daily.ticker` set (`P`).
  3. Compute upsert set `U = A ∩ P` (apply operator's invariant: ticker not in daily-bars → not in classification).
  4. Compute delete set `D = {existing ticker_classifications.ticker} - U`.
  5. Single transaction: `INSERT ... ON CONFLICT DO UPDATE` for `U`; `DELETE FROM platform.ticker_classifications WHERE ticker = ANY($D)`.
  6. Log `+inserts / ~updates / -deletes` counts to `application_log` for visibility.

### 5.2 Risk mitigation

- First-run dry-mode (`--param dry_run=true`) to surface the delete-set size. If `|D|` > 1% of universe, halt + operator review.
- Audit trail: persist the delete-set to `application_log.data` JSON before the DELETE executes (recoverable forensics).

### 5.3 Exit gate

- Producer test green (`python -m pytest tpcore/tests/test_classify_tickers.py -q` — write coverage during this phase).
- Live dry-run shows `|D| < 100` (expected: today's accumulated +46 drift + ~200 historical FMP-coverable-only ADRs that aren't in Alpaca's asset list).
- Operator approves the delete-set; live run executes; row-count delta matches dry-run forecast.

---

## 6. Phase 4 — Invariant verification gate

**Read-only gate, no migrations.** Confirms the FK precondition holds before Phase 5 cleanup work begins.

```sql
-- Gate 1: classify_tickers drift = 0 (ticker_classifications ⊆ prices_daily.ticker)
SELECT COUNT(*) AS classifications_not_in_prices
FROM platform.ticker_classifications tc
WHERE NOT EXISTS (
    SELECT 1 FROM platform.prices_daily p WHERE p.ticker = tc.ticker
);
-- Expected: 0

-- Gate 2: re-run the Phase 0 orphan audit to confirm baseline shape is stable
-- (Phase 5 plans cleanup against THIS baseline, not the original)
```

If Gate 1 ≠ 0: STOP. Producer fix in Phase 3 incomplete. Don't proceed.

---

## 7. Phase 5 — Per-table orphan cleanup

**Strategy per table:** for each table T from Phase 0 with `orphan_count > 0`, pick ONE of three remediation paths:

| Path | When to use | Migration template |
|---|---|---|
| **A. BACKFILL** | Orphan tickers are real-but-missing-from-classifications (foreign ADRs, recently-listed) | Insert into `ticker_classifications` from `prices_daily` distinct-ticker set + Alpaca `/v2/assets` re-pull |
| **B. DELETE** | Orphan tickers are stale/wrong (bad-source rows, delisted, typos) | `DELETE FROM platform.<T> WHERE ticker IN (...)` with audit row to `application_log` |
| **C. ARCHIVE-THEN-DELETE** | Orphan tickers contain non-trivial historical data that may be needed later | `INSERT INTO platform.<T>_archive SELECT *` then DELETE |

### 7.1 Cleanup migration template (path B example)

File: `platform/migrations/versions/20260523_1300_cleanup_orphans_<T>.py`

```text
upgrade():
    op.execute("""
        WITH orphans AS (
            SELECT c.ctid AS row_id, c.ticker
            FROM platform.<T> c
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.ticker_classifications p
                WHERE p.ticker = c.ticker
            )
        )
        DELETE FROM platform.<T>
        WHERE ctid IN (SELECT row_id FROM orphans)
    """)
    # Note: ctid is row-version-specific. For large tables, prefer
    # WHERE ticker NOT IN (SELECT ticker FROM ticker_classifications)
    # with EXPLAIN ANALYZE plan in PR body.

downgrade():
    # Orphan cleanup is forward-only by design; downgrade is documentation:
    op.execute("-- forward-only; restore from prices_daily archive if needed")
```

### 7.2 Per-table operator decision matrix (TO BE FILLED IN PHASE 0)

| Table | Orphan count (Phase 0) | Path A/B/C | Operator-sign-off |
|---|---|---|---|
| prices_daily | TBD | likely A (these tickers SHOULD be in classifications) | required |
| insider_transactions | TBD | likely A (foreign ADRs — backfill with country='XX') | required |
| sec_material_events | TBD | A | — |
| corporate_actions | TBD | A | — |
| earnings_events | TBD | likely B (FMP earnings for delisted tickers) | required |
| fundamentals_quarterly | TBD | B for delisted, A for active | required |
| short_interest | TBD | B (FINRA bi-monthly stale) | — |
| borrow_rates | TBD | B | — |
| social_sentiment | TBD | B (ApeWisdom returns random crypto tickers) | — |
| options_max_pain | TBD | A (single SPY symbol, must be in classifications) | — |
| insider_sentiment | TBD | B (Finnhub returns broader universe) | — |
| spread_observations | TBD | B | — |
| universe_candidates | TBD | C (historical engine outputs) | — |
| liquidity_tiers | TBD | A | — |
| insider_mspr_daily | TBD | derived; rebuild after parent cleaned | — |

### 7.3 Exit gate

- Per-table orphan re-audit: `orphan_count = 0` for every table in scope.
- Verification SELECT after each cleanup migration logged in PR body.

---

## 8. Phase 6 — FK additions: light tables (5 tables, batched 1 PR)

**Tables in scope** (low row-count, low FK risk): `liquidity_tiers`, `options_max_pain`, `insider_sentiment`, `spread_observations`, `universe_candidates`.

### 8.1 FK migration template

File: `platform/migrations/versions/20260523_1500_fk_light_tables.py`

```text
upgrade():
    # liquidity_tiers
    op.create_foreign_key(
        "fk_liquidity_tiers_ticker",
        source_table="liquidity_tiers", referent_table="ticker_classifications",
        local_cols=["ticker"], remote_cols=["ticker"],
        ondelete="RESTRICT", onupdate="CASCADE",
        source_schema="platform", referent_schema="platform",
    )
    # options_max_pain
    op.create_foreign_key("fk_options_max_pain_ticker", ...)
    # insider_sentiment (column: symbol)
    op.create_foreign_key(
        "fk_insider_sentiment_symbol",
        source_table="insider_sentiment", referent_table="ticker_classifications",
        local_cols=["symbol"], remote_cols=["ticker"],
        ondelete="RESTRICT", onupdate="CASCADE",
        source_schema="platform", referent_schema="platform",
    )
    # spread_observations
    op.create_foreign_key("fk_spread_observations_ticker", ...)
    # universe_candidates
    op.create_foreign_key("fk_universe_candidates_ticker", ...)

downgrade():
    op.drop_constraint("fk_universe_candidates_ticker", "universe_candidates",
        schema="platform", type_="foreignkey")
    # ... (drop in reverse order)
```

### 8.2 Index check

Every FK column in this phase already has an index via existing PK or unique constraint — verify with `\d+ platform.<T>` before adding FK. (Postgres does NOT auto-index FK columns.)

### 8.3 Exit gate

- Migration round-trip green.
- DELETE-RESTRICT smoke test: `BEGIN; DELETE FROM platform.ticker_classifications WHERE ticker = 'AAPL' LIMIT 1; ROLLBACK;` raises `ForeignKeyViolation` from one of the new FKs.
- Heavy-lane gates green (per `.claude/agents/db-architect.md` §7).

---

## 9. Phase 7 — FK additions: medium tables (6 tables, 1 PR per table)

**Tables in scope:** `insider_transactions`, `sec_material_events`, `corporate_actions`, `earnings_events`, `fundamentals_quarterly`, `short_interest`, `borrow_rates`, `social_sentiment`.

**Why per-table PR:** each table has ≥10K rows and a distinct producer; isolation aids rollback if producer regression surfaces post-merge.

### 9.1 FK migration template (same as Phase 6 but per file)

File pattern: `platform/migrations/versions/20260523_16<NN>_fk_<table>.py`

```text
upgrade():
    op.create_foreign_key(
        "fk_<table>_ticker",
        source_table="<table>", referent_table="ticker_classifications",
        local_cols=["ticker"], remote_cols=["ticker"],
        ondelete="RESTRICT", onupdate="CASCADE",
        source_schema="platform", referent_schema="platform",
    )
    # Ensure FK column is indexed (most are via PK first-col; verify before merge)
    # op.create_index("idx_<table>_ticker", "<table>", ["ticker"], schema="platform")

downgrade():
    op.drop_constraint("fk_<table>_ticker", "<table>",
        schema="platform", type_="foreignkey")
```

### 9.2 Recommended ordering (lowest risk first)

1. `borrow_rates` (small, frequently refreshed; first to expose producer-regression early)
2. `short_interest` (bi-monthly cadence; slow regression)
3. `social_sentiment` (ApeWisdom; some volatility expected)
4. `earnings_events` (FMP; corporate-actions-adjacent)
5. `corporate_actions` (Alpaca; touches splits/dividends — careful)
6. `sec_material_events` (SEC; high row count but stable producer)
7. `insider_transactions` (largest of the SEC tables)
8. `fundamentals_quarterly` (~178K rows, FMP)

### 9.3 Exit gate (per PR)

- Migration round-trip green.
- Producer regression test: run the relevant `--stage <X>` once after migration; verify upsert succeeds (no FK violation on a ticker that's already in classifications).
- `python -m tpcore.auditheal` exits 0 (no new orphans, since they were cleaned in Phase 5).

---

## 10. Phase 8 — FK addition: `prices_daily` (THE HARDEST MIGRATION)

**Why hardest:**
- ~20.6M rows.
- Default `ALTER TABLE ... ADD FOREIGN KEY` takes an **AccessExclusiveLock** on the child table AND scans every row for the constraint check. On 20.6M rows + Supabase Pro tier limits, this will easily hit the **5-minute statement_timeout** and lock out concurrent reads from engines.
- This is THE table engines query on every scan — read lock-out is a hard production-impact event.

### 10.1 Strategy: `NOT VALID` + concurrent `VALIDATE CONSTRAINT`

The Postgres-canonical two-phase add-FK-without-table-lock pattern (docs: <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>):

```text
-- Step 1 (fast, only short ACCESS EXCLUSIVE lock): mark constraint as
-- NOT VALID. Postgres skips the full-table scan; new rows still enforced.
ALTER TABLE platform.prices_daily
    ADD CONSTRAINT fk_prices_daily_ticker
    FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker)
    ON UPDATE CASCADE ON DELETE RESTRICT
    NOT VALID;

-- Step 2 (slow but only SHARE UPDATE EXCLUSIVE — concurrent reads OK):
-- validate existing rows. May take 5-30 min on 20.6M rows.
ALTER TABLE platform.prices_daily
    VALIDATE CONSTRAINT fk_prices_daily_ticker;
```

### 10.2 Migration template (single migration, two ops)

File: `platform/migrations/versions/20260523_1800_fk_prices_daily.py`

```text
upgrade():
    op.execute("""
        ALTER TABLE platform.prices_daily
            ADD CONSTRAINT fk_prices_daily_ticker
            FOREIGN KEY (ticker)
            REFERENCES platform.ticker_classifications(ticker)
            ON UPDATE CASCADE ON DELETE RESTRICT
            NOT VALID
    """)
    op.execute("""
        ALTER TABLE platform.prices_daily
            VALIDATE CONSTRAINT fk_prices_daily_ticker
    """)
    # FK column index check: (ticker, date) PK first-col already covers ticker.

downgrade():
    op.drop_constraint("fk_prices_daily_ticker", "prices_daily",
        schema="platform", type_="foreignkey")
```

### 10.3 Pre-migration checks

- **Phase 5 cleanup must show `orphan_count = 0` on prices_daily.** Validate the day-of:
  ```sql
  SELECT COUNT(*) FROM platform.prices_daily p
  WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications c WHERE c.ticker = p.ticker);
  ```
  If > 0, abort. VALIDATE CONSTRAINT will fail loudly mid-migration otherwise (worse outcome).
- Statement timeout: explicit `SET LOCAL statement_timeout = '30min'` inside the migration transaction (operator note: Supabase Pro tier may need to raise this via dashboard before running).
- Run during the **off-window** — local laptop UTC 04:00–08:00 (market closed in US, Asia open but engines don't trade off prices_daily mid-Asia).

### 10.4 Rollback strategy

- Downgrade = simple DROP CONSTRAINT (fast, ms-scale).
- If VALIDATE CONSTRAINT fails mid-way, the NOT VALID constraint remains — new rows are protected, but existing orphans cause VALIDATE to fail. Re-run Phase 5 cleanup, then `VALIDATE CONSTRAINT` again.

### 10.5 Exit gate

- `\d+ platform.prices_daily` shows `fk_prices_daily_ticker` as `NOT NULL FOREIGN KEY` (not "NOT VALID").
- DELETE-RESTRICT smoke test against ticker_classifications.
- Heavy-lane gates green.
- Run a real ingest cycle (`--stage daily_bars --param universe=active --param lookback_days=1`) end-to-end without FK violation.

### 10.6 Why this is the **riskiest phase**

- Largest row count by 100× over the next-largest table.
- Touches the table engines depend on every scan.
- Statement timeout interaction with Supabase Pro tier is unproven for this volume.
- Locks during VALIDATE CONSTRAINT could affect concurrent producer writes (daily_bars upserts during US market close).
- Cleanup-precondition is fragile — even one orphan from a producer-race-condition between Phase 5 and Phase 8 will fail VALIDATE.

**Mitigation:** schedule the migration in a maintenance window where:
1. No `daily_bars` stage is mid-flight.
2. No engine cron is scheduled in the next 60 min.
3. Operator is present to monitor + abort if timeout approaches.

---

## 11. Phase 9 — FK additions: Tier 2 derived tables (1 PR)

**Tables:** `liquidity_tiers` (already done in Phase 6 since it's small), `insider_mspr_daily`. Also: confirm `universe_candidates` was done in Phase 6.

### 11.1 Migration template

Same as Phase 6 — single-line FK creation.

### 11.2 Note on derived-table integrity

Per memory: derived tables silently inherit upstream staleness. Adding the universe FK does **not** fix freshness (separate plan needed). It does fix the orphan dimension: a `liquidity_tier` for `XYZ` cannot exist unless `XYZ` is in `ticker_classifications`. Combined with the producer rule (Phase 3), this means stale tier rows for delisted tickers get purged when classify_tickers DELETEs.

### 11.3 Exit gate

- All ticker-bearing tables in §0.2 now have FK to `ticker_classifications`.
- `\d+ platform.ticker_classifications` shows all child FKs referenced.

---

## 12. Phase 10 — Post-FK verification (READ-ONLY + producer regression)

### 12.1 DELETE-RESTRICT smoke test

```sql
BEGIN;
DELETE FROM platform.ticker_classifications WHERE ticker = 'AAPL';
-- expected: ERROR  update or delete on table "ticker_classifications" violates
--          foreign key constraint "fk_prices_daily_ticker" on table "prices_daily"
ROLLBACK;
```

Run for 3 sample tickers (AAPL = heavy / SPY = derived-dependency / a newly-listed low-row ticker).

### 12.2 Producer regression sweep

Run each `--stage` once and confirm no FK violation:

- `daily_bars`, `corporate_actions`, `fundamentals_refresh`, `compute_fundamental_ratios`, `earnings_events`, `sec_filings`, `finra_short_interest`, `iborrowdesk_borrow_rates`, `apewisdom_social_sentiment`, `finnhub_insider_sentiment`, `greeks_max_pain`, `aaii_sentiment`, `macro_indicators`, `classify_tickers`, `assign_liquidity_tiers`, `universe_candidates_refresh`, `compute_insider_mspr_daily` (verify name).

### 12.3 Update authoritative docs

- `docs/DATABASE_AND_DATAFLOW.md` §2.1 ERD — add FK arrows from every ticker-bearing entity to `TICKER_CLASSIFICATIONS`.
- `docs/DATABASE_AND_DATAFLOW.md` §2.2 — add a top-level note: "Every `ticker` column FK → `platform.ticker_classifications(ticker)` with ON UPDATE CASCADE ON DELETE RESTRICT."

### 12.4 Exit gate

- All producers ran clean.
- DELETE-RESTRICT works on all 3 sample tickers.
- ERD updated.
- Memory file `project_database_architecture_state_2026_05_23.md` outstanding-debt section item (3) marked DONE.

---

## 13. Dependency graph — which Task # blocks which phase

| Phase | Blocks-on Task # | Notes |
|---|---|---|
| 0 | none | read-only audit |
| 1 | none | rename is independent; doesn't depend on #8/#9/#10/#12/#14/#15 |
| 2 | (operator approval for Alpaca country pull — granted 2026-05-23) | needed for Task #15 partitioning |
| 3 | **none structurally**, but should ideally land AFTER #12 fmp_daily_bars CSV archive diagnostic completes (so classify_tickers can lean on a known-stable prices_daily set when computing `A ∩ P`) | medium risk if #12 finds a coverage gap |
| 4 | depends on Phase 3 | verification gate |
| 5 | depends on Phase 4 | per-table operator decisions |
| 6 | depends on Phase 5 | FK rollout — light tables |
| 7 | depends on Phase 5 | FK rollout — medium tables |
| 8 | depends on Phase 5 + Phase 6/7 (validates the pattern at smaller scale first) | THE hard one |
| 9 | depends on Phase 5 | Tier 2 derived FK |
| 10 | depends on Phase 6+7+8+9 | verification |

**Task-# cross-references (open work that interacts with this plan):**

- **Task #8** (auto-rebuild-from-archive for all feeds): orthogonal but useful — if FK rollback is needed mid-Phase-7, archive rebuild lets producers restore quickly. Doesn't block this plan.
- **Task #9** (CSV archive cleanup): orthogonal.
- **Task #10** (data-lake story = PR #167 auditheal): **complementary**. PR #167 detects+remediates orphans after the fact; this plan prevents them at INSERT. After this plan ships, the `cross_table_audit.<T>.orphan_no_prices` checks become **assertion-style sentinels** (should always read 0; non-zero indicates either a producer bug or a constraint loophole) rather than active-remediation feeds.
- **Task #12** (missing fmp_daily_bars CSV archive): **soft-blocks Phase 3** — if FMP daily_bars producer has hidden coverage gaps, classify_tickers's `A ∩ P` set will be wrong; mitigate by running Phase 3 dry-run BEFORE Phase 4 gate.
- **Task #14** (ProviderProfile + download constraints): orthogonal to FK rollout, parallel work.
- **Task #15** (per-country insider adapters → `insider_transactions`): **depends on Phase 1 (rename) + Phase 2 (country column)**. After this plan's Phase 2 ships, Task #15 can land its FMP-non-US adapter writing into the renamed `insider_transactions` table with `source='fmp'` + per-country routing via `ticker_classifications.country`.

---

## 14. Risk register

| # | Risk | Phase | Mitigation |
|---|---|---|---|
| R1 | classify_tickers producer change inadvertently DELETEs valid rows when Alpaca asset list has a transient outage | 3 | Dry-run gate + delete-set audit row + operator review if |D| > 1% |
| R2 | Phase 5 cleanup deletes rows the operator wanted to keep (path B vs A misjudgment) | 5 | Per-table operator sign-off; path C (archive-then-delete) as default for any uncertain table |
| R3 | Phase 8 VALIDATE CONSTRAINT hits statement timeout on prices_daily | 8 | Pre-set `statement_timeout = '30min'`; off-window scheduling; orphan_count=0 pre-check |
| R4 | New row INSERTed via raw SQL (not through producer) violates new FK | post-6+ | Acceptable — this is the whole point. Constraint violation is the desired signal. |
| R5 | Tier 2 derived-table compute runs mid-classify_tickers-DELETE and produces a stale row that's then orphaned by the cascade | 3, 9 | Producer ordering: classify_tickers DELETE before Tier 2 refresh (already the case per memory dependency tree) |
| R6 | Downgrade path needed mid-Phase 8 with VALIDATE half-done | 8 | DROP CONSTRAINT is fast even mid-VALIDATE; retain a rollback PR ready before push |
| R7 | Alembic head divergence — another db-architect PR lands during this plan | all | Re-base each phase against fresh main; verify alembic heads before each migration push (per agent contract §3) |
| R8 | `aar_events` / `application_log` / `data_quality_log` / `risk_state` ALSO have ticker mentions (in JSON payloads, not columns) — NOT in FK scope but operator might expect them | post-10 | Documented as out-of-scope; payload-level integrity is a forensics question, not a schema question |
| R9 | Phase 1 rename breaks an external dashboard query or external script | 1 | Grep before-push: `grep -r 'sec_insider_transactions' --include='*.py' --include='*.sh' --include='*.sql' --include='*.md'`; verify dashboard.py touched |
| R10 | A Tier 2 derived table not in §0.2 list emerges (e.g. `insider_mspr_daily` schema differs) | 9 | Phase 0 audit includes a `\dt platform.*` enumeration; reconcile against §0.2 list and adjust before Phase 9 |

---

## 15. Final FK relationship diagram (text-based)

```text
                    platform.ticker_classifications  (ticker PK, country, asset_class, ...)
                                      ↑
                                      │ ON UPDATE CASCADE ON DELETE RESTRICT
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
        │ Tier 1 — RAW                │                             │ Tier 2 — DERIVED
        │                             │                             │
        ├─ prices_daily.ticker        ├─ social_sentiment.ticker    ├─ liquidity_tiers.ticker
        ├─ insider_transactions.ticker├─ options_max_pain.ticker    ├─ spread_observations.ticker
        ├─ sec_material_events.ticker ├─ insider_sentiment.symbol   ├─ universe_candidates.ticker
        ├─ corporate_actions.ticker   ├─ borrow_rates.ticker        └─ insider_mspr_daily.ticker
        ├─ earnings_events.ticker     ├─ short_interest.ticker
        └─ fundamentals_quarterly.ticker

      Out-of-scope (no ticker column):
        - aaii_sentiment (date-only)
        - macro_indicators (indicator+date)
        - aar_events (ticker in JSONB payload, not column)
        - application_log (no ticker)
        - data_quality_log (no ticker)
        - risk_state (engine-keyed)
        - allocations (engine-keyed)
        - forensics_triggers (no ticker column)
        - open_orders (ticker column, BUT short-lived working state — TBD whether FK adds value or just lock contention)
        - tradier_options_chains (FROZEN per docs; handled by cross_ref_cleanup; TBD whether FK adds value to a frozen table)
```

**Open question for operator (Phase 9 design):** add FK to `open_orders.ticker` and `tradier_options_chains.ticker`? Both are edge cases:
- `open_orders` is transactional + short-lived; FK adds correctness guarantee but introduces a hard fail mode if a ticker classification drops between order-submit and order-fill (rare but theoretically possible).
- `tradier_options_chains` is frozen + cleaned by cross_ref_cleanup; FK would render cross_ref_cleanup redundant (cascade-on-delete already handled by RESTRICT + producer-side filter). Recommend: yes for `open_orders` (defer the rare-race-handling); skip for `tradier_options_chains` until table is unfrozen.

---

## 16. Out-of-scope — explicit future plans referenced but not built here

1. **Composite `(ticker, date)` FK to prices_daily** for time-series children (`spread_observations`, `liquidity_tiers`, `insider_mspr_daily`) — would enforce "no derived row without a parent bar". Separate plan; depends on prices_daily being clean post-Phase 8.
2. **Tier 2 freshness constraints** — operator's "stale upstream silently produces stale downstream" issue. Need DB-level or producer-level enforcement that derived `recorded_at` is within N days of parent `recorded_at`. Separate plan.
3. **`source` column policy on multi-source tables** — `prices_daily.source IN ('alpaca','tradier','fmp')` already exists; `insider_transactions.source IN ('sec','fmp')` added in Phase 1. Other tables (single-source today) should NOT get a source column per the agent contract §2.
4. **RLS / Supabase row-level policies** — not part of this plan.
5. **Per-engine table FKs** (`open_orders.engine`, `risk_state.engine`, etc.) — engine roster is a separate SoT (see `engine-roster-sot` plan); coordinate before adding.

---

## 17. Execution-time checklist (for the subagent that runs this plan)

For each phase:

1. Branch off fresh `main`: `git fetch origin && git checkout -b feat/refint-pNN-<topic> origin/main`.
2. Read the relevant phase section above; do NOT skip the read.
3. Run the orphan-audit query (Phase 0 baseline if not done; per-table re-audit per phase).
4. Implement the migration per the template.
5. Run alembic round-trip (`upgrade head` → `downgrade -1` → `upgrade head`).
6. Run heavy-lane gates per `.claude/agents/db-architect.md` §7.
7. Open PR with audit numbers + EXPLAIN ANALYZE plans (if Phase 7+8) in the body.
8. Squash-merge on green CI.
9. Pull main locally; verify exit gate; only THEN proceed to next phase.

Never run two phases in a single PR. Never skip the round-trip. Never skip the orphan re-audit before adding the FK.

---

**END OF PLAN.**
