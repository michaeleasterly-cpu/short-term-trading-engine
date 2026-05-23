# Referential-Integrity Design v2 — `platform.*` Schema, Phase 1 (FK Enforcement, NOT-VALID-FIRST sequence)

**Status:** v2. **Supersedes v1 (`docs/superpowers/specs/2026-05-23-referential-integrity-design.md`)** by inverting the FK rollout sequence to **NOT-VALID-FIRST**. v1's content is preserved as the historical record; do not delete it. Where v2 and v1 conflict on sequence, gating, or migration shape, **v2 wins**. The contract (invariants in §2, scope in §3, acceptance criteria in §13) is unchanged in spirit from v1 — only the *how* changes.

**Author / role:** `db-architect` (Postgres + Supabase Pro tier; `platform.*` schema). See `.claude/agents/db-architect.md`.

**Spec basis (read in this order):**
1. `.claude/agents/db-architect.md` — operating contract (audit-before-alter, FK defaults `ON UPDATE CASCADE ON DELETE RESTRICT`, source-named tables, no redundant `source` column on single-source tables, heavy-lane gates).
2. v1 of this spec: `docs/superpowers/specs/2026-05-23-referential-integrity-design.md` — the contract this v2 inherits. **Read v1 for §2 invariants, §3 scope, §4 non-goals — v2 does not duplicate them.**
3. Memory `project_database_architecture_state_2026_05_23.md` — current schema state, two-tier (raw + derived) dependency tree, `ticker_classifications.ticker ⊆ prices_daily.ticker` operator invariant.
4. `docs/DATABASE_AND_DATAFLOW.md` §2 — current schema tables.
5. `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md` — the prior referential work (`tpcore/auditheal`) which detects orphans after-the-fact. v2 (like v1) is the *schema-layer enforcement* that detection layer never delivered.
6. Postgres canonical `NOT VALID` / `VALIDATE CONSTRAINT` pattern: <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>.
7. The implementation plan referenced by v1 — `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md` — **stays as-is for now**; v2 supersedes its phase sequence. A follow-up plan v2 will re-template the phases against this spec; until then this spec is authoritative for the sequence.

**Operator triggers (unchanged from v1):**
- 2026-05-22 *"how the fuck do you design a database with no referential integrity"*
- 2026-05-23 *"we need referential integrity on these tables"*
- 2026-05-23 *"if the ticker isn't in daily bars then the ticker doesn't need to be in the ticker classification"*

**v2-specific trigger (spec-review pass, 2026-05-23):** v1's Phase 5 → 6 → 7 → 8 → 9 sequence makes per-table cleanup a *finite pre-FK event* held across five subsequent PRs while producers keep running. Even one producer race during that window fails Phase 8's 20.6M-row `VALIDATE`. v2 inverts: ship all FKs as `NOT VALID` first (fast, no scan), then per-table cleanup + `VALIDATE` at leisure with producers protected throughout.

---

## 1. Why supersede v1 — the single biggest weakness

v1 treats Phase 5 cleanup as a one-shot pre-Phase-6 event. The schema commits to **15 tables at zero orphans before any FK lands, AND held at zero across four more PRs plus a 20.6M-row `VALIDATE CONSTRAINT`**. In reality producers regenerate orphans every run (the whole reason FKs are being added in the first place). Even one producer race between Phase 5 (cleanup-complete) and Phase 8 (`VALIDATE`) = `VALIDATE` fails on the offending row, and the migration aborts mid-flight. v1's R10 ("cleanup-precondition fragile — even one orphan from a producer race condition will fail VALIDATE") acknowledges this but offers only a day-of re-check as mitigation; the re-check window is the entire migration runtime, which on 20.6M rows is 5–30 minutes long. v1's sequence is fragile by design.

**The NOT-VALID-FIRST inversion (v2):** ship all 15 FKs as `ADD CONSTRAINT … NOT VALID` in **one** Phase 2-equivalent PR. This is a fast operation (ACCESS EXCLUSIVE held briefly, no row scan). **From that moment producers cannot create new orphans** — the FK is enforced for every INSERT/UPDATE; only existing rows remain unvalidated. Per-table cleanup + per-table `VALIDATE CONSTRAINT` then happen at whatever cadence each table's orphan volume justifies, **independently and at leisure**. Phase 5's race window disappears entirely; v1's R10 vanishes from the risk register.

This collapses v1's 11 phases to **5 phases** (Phase 0 audit, Phase 1 rename + country + pre-FK index audit, Phase 2 NOT-VALID-FIRST bulk, Phase 3 classify_tickers producer fix, Phase 4 per-table cleanup-then-VALIDATE rolling, Phase 5 final verification). It also lets the operator stop the rollout after Phase 2 if needed — new-orphan protection is fully shipped, only existing-orphan VALIDATE remains.

---

## 2. Invariants the schema will enforce (UNCHANGED from v1 §2)

Invariants I-1 through I-6 from v1 §2 are retained verbatim. v2 strengthens the verification side of I-1: the FK is *enforced* for new rows after Phase 2 ships (`NOT VALID` mode); the *validated* state (existing rows verified) is reached per-table during Phase 4, on the table's own clock.

A new FK violation MUST fail loud — see §8 (test contracts, unchanged from v1).

---

## 3. Scope (UNCHANGED from v1 §3)

15 in-scope tables, 12 out-of-scope, 2 edge-case operator-decision tables. See v1 §3.1, §3.2, §3.3 — v2 does not alter the scope list. v2 adds one pre-Phase-2 verification step under §4.3 of this doc: the FK-column-indexing audit (see §6 below).

---

## 4. Non-goals (UNCHANGED from v1 §4)

Out-of-scope items 1–9 in v1 §4 stand. v2 reiterates three boundaries explicitly because they intersect with the v2 sequence inversion:

- **Macro consolidation** (`macro_indicators` / `aaii_sentiment` / `fear_greed` → `macro_data`) — Task #18, stays SEPARATE. v2 does not fold it in.
- **Phase 2 denormalization** — Task #17, stays deferred.
- **Per-country insider adapters** — Task #15, stays separate. Builds *on* this spec (post-Phase 1 of v2).

---

## 5. The NOT-VALID-FIRST pattern (v2's load-bearing change)

### 5.1 Why NOT-VALID-FIRST is safe

Per Postgres docs (https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES):

> *"This form adds a new constraint to a table using the same syntax as `CREATE TABLE`. Foreign key constraints can also be marked `NOT VALID`, which prevents Postgres from verifying that all existing rows satisfy the constraint. Future inserts and updates are checked, even when the constraint is `NOT VALID`. Adding a constraint with `NOT VALID` requires only a `SHARE ROW EXCLUSIVE` lock on the parent and child, briefly held."*

Two things matter for v2's correctness:

1. **`NOT VALID` constraints enforce on NEW rows immediately.** Once Phase 2 commits, every INSERT/UPDATE/COPY on a child table is FK-checked against `ticker_classifications`. A producer attempting to write an orphan ticker fails the constraint and the row never lands. **This is the point of v2.**
2. **`VALIDATE CONSTRAINT` is a separate, weaker-lock operation.** Per the same docs: *"This form validates a foreign key or check constraint that was previously created as `NOT VALID`, by scanning the table to ensure there are no rows for which the constraint is not satisfied. Nothing happens if the constraint is already marked valid. The value of this option is that scanning a large table to verify a new constraint can be slow, and the table must be locked against updates only briefly via an `ACCESS EXCLUSIVE` lock during the `ADD CONSTRAINT NOT VALID`; `VALIDATE CONSTRAINT` requires only `SHARE UPDATE EXCLUSIVE`."* — meaning concurrent reads AND writes proceed during `VALIDATE`.

The two-phase pattern v1 already invokes for `prices_daily` is generalised in v2 to **all 15 tables, run in two separate migrations**.

### 5.2 The critical v1→v2 fix: `ADD CONSTRAINT NOT VALID` and `VALIDATE CONSTRAINT` MUST live in separate migrations

v1's Phase 8 migration template (plan §10.2) conflates the two operations in one transaction:

```text
upgrade():
    op.execute("ALTER TABLE … ADD CONSTRAINT … NOT VALID")
    op.execute("ALTER TABLE … VALIDATE CONSTRAINT …")
```

This is wrong. The two operations require different locks (`ACCESS EXCLUSIVE` brief vs `SHARE UPDATE EXCLUSIVE` long). Wrapping them in one transaction forces Postgres to hold the stronger lock for the *entire* compound operation, forfeiting the very benefit the two-phase pattern was designed for. **v2 mandates two distinct Alembic migrations per FK addition** — one for `NOT VALID`, one for `VALIDATE`. They get distinct `revision` IDs and distinct PRs (Phase 2 bulk for the `NOT VALID` set; Phase 4 per-table for the `VALIDATE` set). Operationally this means concurrent reads + writes are uninterrupted throughout the long `VALIDATE` run on `prices_daily`, exactly as the Postgres docs promise.

### 5.3 Per-FK template (one `NOT VALID` migration covering all 15 tables in Phase 2)

```text
-- Phase 2 (single migration; one `NOT VALID` per in-scope child table).
ALTER TABLE platform.prices_daily
    ADD CONSTRAINT fk_prices_daily_ticker
    FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker)
    ON UPDATE CASCADE ON DELETE RESTRICT
    NOT VALID;

ALTER TABLE platform.insider_transactions
    ADD CONSTRAINT fk_insider_transactions_ticker
    FOREIGN KEY (ticker) REFERENCES platform.ticker_classifications(ticker)
    ON UPDATE CASCADE ON DELETE RESTRICT
    NOT VALID;

-- … repeated for the remaining 13 in-scope tables (sec_material_events,
-- corporate_actions, earnings_events, fundamentals_quarterly,
-- short_interest, borrow_rates, social_sentiment, options_max_pain,
-- insider_sentiment (column: symbol), liquidity_tiers, spread_observations,
-- universe_candidates, insider_mspr_daily).
```

### 5.4 Per-FK template (per-table `VALIDATE` migrations in Phase 4)

```text
-- Phase 4 — one migration per table, ordered light → heavy.
ALTER TABLE platform.<T>
    VALIDATE CONSTRAINT fk_<T>_ticker;
```

This is forward-only and idempotent: if `VALIDATE` fails (an orphan slips through producer cleanup), the constraint remains `NOT VALID` — new rows are still protected. Re-run the cleanup migration for that table, re-run `VALIDATE`. No downgrade needed.

---

## 6. Pre-FK index audit (v2's new mandatory step)

`.claude/agents/db-architect.md` §6: *"Every FK column needs an index (Postgres doesn't auto-index FKs)."* v1's plan §8.2 / §9.1 says "verify with `\d+ platform.<T>` before adding FK" but does not enumerate which tables have a gap. **v2 makes this a Phase 0 deliverable**: a per-table FK-column-index audit, with an explicit `CREATE INDEX CONCURRENTLY` migration scheduled *before* Phase 2 ships for any child where the FK column isn't already covered.

### 6.1 Why this matters operationally

`ON DELETE RESTRICT` enforcement requires the **parent's DELETE** to look up the FK column on every child to confirm no references exist. If the child's FK column isn't indexed, that lookup is a full sequential scan of the child table. For most in-scope tables the FK column is the leading PK column (`prices_daily.ticker` leads `(ticker, date)`, `fundamentals_quarterly.ticker` leads `(ticker, filing_date)`, etc.) and the existing PK B-tree covers it. **Two tables in §3.1 do not have ticker as a leading PK column:**

| Table | PK | Existing index on `ticker`? |
|---|---|---|
| `spread_observations` | `(id)` (BigInteger Identity) | YES — `spread_observations_ticker_observed_idx` on `(ticker, observed_at)` covers ticker as leading column. Verified from `platform/migrations/versions/20260512_2100_spread_observations_and_liquidity_tiers.py`. |
| `universe_candidates` | `(as_of_date, engine, ticker)` — `ticker` is the **third** column, NOT leading | NO standalone index on `ticker`. The `idx_uc_engine_date` index covers `(engine, as_of_date)`, not `ticker`. Verified from `platform/migrations/versions/20260513_1237_create_universe_candidates.py`. **Gap.** |

A parent DELETE on `ticker_classifications.ticker = X` would force a sequential scan of `universe_candidates` (~20K rows today; grows with each engine run). Negligible at current scale but pathological if the operator runs a multi-ticker reclassification. **Phase 0 audit ships the index-add migration for `universe_candidates` before Phase 2 `NOT VALID` lands.**

### 6.2 Phase 0 enumeration query

```sql
-- For each in-scope child table, list FK-column-covering indexes.
SELECT
    c.relname AS table_name,
    i.relname AS index_name,
    pg_get_indexdef(i.oid) AS index_def
FROM pg_class c
JOIN pg_index ix ON c.oid = ix.indrelid
JOIN pg_class i ON ix.indexrelid = i.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'platform'
  AND c.relname = ANY(ARRAY[
      'prices_daily','insider_transactions','sec_material_events',
      'corporate_actions','earnings_events','fundamentals_quarterly',
      'short_interest','borrow_rates','social_sentiment',
      'options_max_pain','insider_sentiment','liquidity_tiers',
      'spread_observations','universe_candidates','insider_mspr_daily'
  ])
ORDER BY c.relname, i.relname;
```

Operator-readable report goes to `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md`. Any child whose FK column is not the **leading column** of at least one B-tree index gets an `CREATE INDEX CONCURRENTLY idx_<table>_ticker ON platform.<T> (ticker)` migration shipped before Phase 2.

### 6.3 `CREATE INDEX CONCURRENTLY` lock budget

`CREATE INDEX CONCURRENTLY` holds `SHARE UPDATE EXCLUSIVE` — concurrent reads and writes proceed. Cannot run inside a transaction (Alembic must run it outside the migration `op.execute` transaction; v2 uses `op.execute("CREATE INDEX CONCURRENTLY …")` with `with op.get_context().autocommit_block():` per Alembic docs).

---

## 7. Updated phase sequence (5 phases vs v1's 11)

### Phase 0 — Pre-flight audit + index audit (READ-ONLY + 0–N CREATE INDEX CONCURRENTLY)

**Goal:** orphan counts per child table; FK-column-index coverage report; statement_timeout verification.

**Deliverables:**
- `docs/superpowers/audits/2026-05-23-referential-integrity-baseline.md` — orphan counts using the audit-template query (v1 §6.1, using `WHERE NOT EXISTS (...)`, see §11 below for the v2 cleanup-template correction).
- `docs/superpowers/audits/2026-05-23-referential-integrity-index-audit.md` — FK-column-index coverage report (§6 above).
- Statement-timeout verification per §9 below.
- 0–N `CREATE INDEX CONCURRENTLY` migrations for any child with an FK-column-index gap. Expected: at least one, for `universe_candidates`.

**Exit gate:** orphan counts captured; index gaps closed; `statement_timeout` capability confirmed.

### Phase 1 — Rename + country + classify_tickers PRECONDITION (no FKs yet)

This is v1's old Phase 1 (rename) + Phase 2 (country) batched, **BUT classify_tickers producer change is deliberately deferred to Phase 3** (after `NOT VALID` lands). See §10 for the producer-ordering reasoning.

**Migrations:**

1. **Rename `sec_insider_transactions` → `insider_transactions` with compatibility view** (see §10 for the compatibility-view pattern). Add `source TEXT NOT NULL DEFAULT 'sec'` column + `CHECK (source IN ('sec', 'fmp'))`. Update primary-path consumers in `tpcore/sec/` + `tpcore/ingestion/handlers.py`. Compatibility view lives at the old name and silently forwards to the new table; lagging consumers continue to function while the rename propagates.
2. **Add `country char(2)` column to `ticker_classifications`** with `CHECK (country IS NULL OR country ~ '^[A-Z]{2}$')` + partial index on `country IS NOT NULL`. Backfill from Alpaca `/v2/assets` response. Accept honest null-rate (§12 below — likely 20–30% for ETFs).

**Exit gate:** schema migrations round-trip green; rename grep-sweep complete; backfill landed; `country` distribution captured in PR body.

### Phase 2 — NOT-VALID-FIRST bulk FK add (ALL 15 FKs in one migration)

**The v2 inversion.** One migration adds the `FOREIGN KEY … NOT VALID` constraint to every in-scope child from v1 §3.1. From the moment this migration commits, producers cannot create new orphans. Per §5.3 template above.

**Migration:** `platform/migrations/versions/<YYYYMMDD_HHMM>_fk_not_valid_all_15.py`. Each `op.execute` is the raw SQL `ADD CONSTRAINT … NOT VALID` (Alembic's `op.create_foreign_key` does not support `NOT VALID` as a kwarg as of Alembic 1.18 — verify at write time; fall back to `op.execute("ALTER TABLE …")`).

**Pre-migration checks:**
- All 15 FK columns indexed per Phase 0 deliverable.
- Rename + country complete per Phase 1.
- `statement_timeout` set high enough for the bulk DDL (each `ADD CONSTRAINT NOT VALID` is sub-second but 15 of them in series + ACCESS EXCLUSIVE per table = potentially a 10–20s window per producer; size carefully). v2 mandates `SET LOCAL statement_timeout = '5min'` inside the migration transaction.

**Exit gate:**
- Every in-scope child has a `pg_constraint` row with `contype='f'` and `convalidated=false` referencing `ticker_classifications`.
- A `pg_locks` snapshot taken during the migration shows the locks were released cleanly (see §9 below).
- A smoke INSERT of an orphan ticker (`INSERT INTO platform.prices_daily (ticker, date, …) VALUES ('NEVER_EXISTED_XYZ', …)`) raises `ForeignKeyViolation`.

### Phase 3 — classify_tickers DELETE-source-tracking + ⊆-prices_daily filter

**Why now, AFTER the NOT VALID FKs:** producer cleanup logic introduces DELETEs on `ticker_classifications`. v1 puts the producer change before any FK (so DELETEs don't trip ON DELETE RESTRICT during the inevitable producer-race window between Phase 5 cleanup and Phase 8 VALIDATE). v2 has no such window: `NOT VALID` FKs from Phase 2 are RESTRICTed by `ON DELETE RESTRICT`, **including during the un-validated period**. RESTRICT triggers on DELETE based on the constraint definition, not its valid/un-valid state. So:

- v1 ordering: producer change → cleanup → FK. Justified by R5 (producer race in the cleanup window).
- v2 ordering: FK NOT VALID → producer change → per-table cleanup. Justified by R1-v2 (producer race window collapses; producer change now runs against a FK-protected parent, so any classify_tickers DELETE that would orphan a child fails loud immediately rather than silently producing an orphan that VALIDATE will catch later).

Concretely: if classify_tickers tries to DELETE ticker `XYZ` from `ticker_classifications` and `prices_daily` has historical bars for `XYZ`, the DELETE fails with `ForeignKeyViolation` — producer logs the constraint violation and the operator either backfills `ticker_classifications` with `XYZ` (path A from v1 §6.2) or whitelists `XYZ` for explicit cleanup (path B/C).

This is **strictly better than v1's ordering** because the failure surfaces immediately and loudly. v1 ordering would have classify_tickers silently produce an orphan during the Phase 5→Phase 8 window, which would then be discovered only when Phase 8's VALIDATE fails on a 20.6M-row scan after 25 minutes of wall-clock.

**Producer change (no schema migration):** per v1 plan §5.1. The dry-run gate stays (`--param dry_run=true`); the `|D| > 1% halt` stays.

**Exit gate:**
- Producer test green.
- Dry-run shows `|D| < 100`.
- Live run produces no `ForeignKeyViolation` (i.e. every ticker in the delete-set has no live children).
- If `ForeignKeyViolation` surfaces: per-table cleanup decision (Phase 4) for the affected child becomes higher priority.

### Phase 4 — Per-table cleanup + VALIDATE (rolling, at-leisure)

The core of v2's at-leisure approach. For each in-scope table T with `orphan_count > 0` (per Phase 0 audit):

1. **Per-table operator decision** — A (BACKFILL), B (DELETE), or C (ARCHIVE-then-DELETE), per v1 §6.2.
2. **Cleanup migration** using the `WHERE NOT EXISTS` template (see §11 below — NOT v1's `ctid`-based template, which is unsafe under concurrent UPDATE).
3. **`VALIDATE CONSTRAINT fk_<T>_ticker`** in a separate migration (per §5.2 above — never co-mingled with cleanup).
4. **Verification gates** per §9 (pg_locks during VALIDATE, pg_stat_user_constraints post-VALIDATE, replica check post-VALIDATE).

**Ordering recommendation** (light → heavy, mirroring v1 §9.2 but covering all 15 tables; operator can re-order based on which tables they care about validating fastest):

1. `liquidity_tiers` (~7K) — Tier-2 derived; small.
2. `options_max_pain` (~5K) — single symbol SPY.
3. `borrow_rates` (~20K).
4. `insider_sentiment` (~10K) — column is `symbol`, not `ticker`.
5. `social_sentiment` (~50K).
6. `short_interest` (~30K).
7. `spread_observations` (~30K).
8. `universe_candidates` (~20K) — note new index from Phase 0.
9. `insider_mspr_daily` (TBD — verify shape in Phase 0).
10. `corporate_actions` (~50K).
11. `earnings_events` (~80K).
12. `sec_material_events` (~200K).
13. `fundamentals_quarterly` (~178K).
14. `insider_transactions` (~1M).
15. `prices_daily` (~20.6M) — THE big one; same `VALIDATE CONSTRAINT` pattern, schedule in off-window, monitor `pg_locks`.

Each table is **independent**. Phase 4 can pause indefinitely between tables — the FKs are already enforcing for new rows. Operator may merge multiple per-table VALIDATEs into a single PR if convenient; one PR per table is the safe default.

**Exit gate (per-table):** `pg_constraint.convalidated = true` for `fk_<T>_ticker`; replica check confirms propagation; the post-VALIDATE orphan-count query returns 0.

### Phase 5 — Post-FK verification (READ-ONLY + producer regression)

Identical in spirit to v1's Phase 10. All 15 FKs validated; producer regression sweep; ERD updated; memory item marked DONE.

---

## 8. Test contracts (UNCHANGED from v1 §8)

v1's six test classes (constraint-presence, DELETE-RESTRICT smoke, INSERT-violation smoke, cross-table-orphan sentinel, source-tag CHECK, country-format CHECK) stand. v2 adds one new test:

### 8.7 NOT-VALID-still-enforces test

```python
async def test_not_valid_fk_still_blocks_new_orphans(pool) -> None:
    """A NOT-VALID FK enforces on INSERT/UPDATE even before VALIDATE runs."""
    # Setup: confirm at least one in-scope table has its FK in NOT VALID state
    row = await pool.fetchrow("""
        SELECT conname, convalidated FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid = 'platform.universe_candidates'::regclass
          AND confrelid = 'platform.ticker_classifications'::regclass
    """)
    assert row is not None
    # Test runs in Phase 2 (NOT VALID) AND Phase 4 (VALIDATED) — both
    # states must reject the orphan insert.
    async with pool.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO platform.universe_candidates "
                    "(as_of_date, engine, ticker) VALUES "
                    "(CURRENT_DATE, 'momentum', 'NEVER_EXISTED_XYZ')"
                )
```

This test is the load-bearing assertion of v2: the `NOT VALID` flag does NOT mean "constraint is inactive". It means "constraint is unverified-for-existing-rows but ACTIVE-for-new-rows". Test belongs in `tpcore/tests/test_referential_integrity.py`.

---

## 9. Migration safety + verification gates (v2 expansions)

### 9.1 Statement-timeout verification (Phase 0 mandatory)

v1 §9.3 says *"set to 30min"*. v2 makes the assumption explicit and verifiable:

```sql
-- Phase 0: confirm current statement_timeout on the migration role.
SHOW statement_timeout;
-- And on the database default:
SELECT name, setting, source, context
FROM pg_settings
WHERE name IN ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout');
-- And on the role used for migrations:
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = current_user;
```

If the role-level cap is below the budget (`5min` for Phase 2 bulk, `30min` for the prices_daily VALIDATE in Phase 4), the operator raises it via Supabase dashboard before Phase 2 ships. **v2 does not assume Supabase Pro tier allows arbitrary `SET LOCAL statement_timeout` overrides** — that's a dashboard setting at the role level on Supabase Pro, not a per-session override available to migrations. Confirm via Phase 0.

### 9.2 `pg_locks` monitoring (Phase 2 + Phase 4 VALIDATE windows)

During Phase 2 bulk and during each Phase 4 VALIDATE, in a separate session run periodically:

```sql
-- Detect lock escalation. AccessExclusiveLock on a child table during
-- VALIDATE (which should hold only ShareUpdateExclusiveLock) means a
-- concurrent ALTER landed mid-migration. Abort + investigate.
SELECT
    l.pid,
    l.locktype,
    l.mode,
    l.granted,
    c.relname AS table_name,
    a.application_name,
    a.query_start,
    a.state,
    LEFT(a.query, 100) AS query_snippet
FROM pg_locks l
LEFT JOIN pg_class c ON l.relation = c.oid
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
LEFT JOIN pg_stat_activity a ON l.pid = a.pid
WHERE n.nspname = 'platform'
  AND c.relname IN (<in-scope tables>)
ORDER BY l.granted, l.pid;
```

Abort conditions:
- Any `AccessExclusiveLock` on a child table appears during VALIDATE (other than the migration's own initial ACCESS EXCLUSIVE for `ADD CONSTRAINT NOT VALID`).
- Any blocked process accumulates > 60s of `wait_event_type = 'Lock'`.
- A second migration session is detected (Alembic head divergence; abort and rebase).

### 9.3 `pg_stat_user_constraints` / `pg_constraint` post-migration confirmation

Post-Phase 2 and post-each-Phase-4-VALIDATE:

```sql
-- Confirm constraint exists, is enforced, and (post-VALIDATE) is validated.
SELECT
    conname,
    conrelid::regclass AS child_table,
    confrelid::regclass AS parent_table,
    convalidated,
    confupdtype,    -- expect 'c' (CASCADE)
    confdeltype,    -- expect 'r' (RESTRICT)
    coninhcount,
    conislocal
FROM pg_constraint
WHERE contype = 'f'
  AND confrelid = 'platform.ticker_classifications'::regclass
ORDER BY conname;
```

Post-Phase-2 expectation: 15 rows, all with `convalidated = false`, `confupdtype = 'c'`, `confdeltype = 'r'`.
Post-Phase-4 (final) expectation: same 15 rows, all with `convalidated = true`.

**Why this matters:** `pg_constraint.convalidated` is the authoritative source. Tooling that reads `\d+` and parses the human-readable output can be misled (the `(NOT VALID)` annotation can disappear in some Postgres versions even when the constraint is in fact unvalidated). The system catalog is truth.

### 9.4 Replica-propagation check (Supabase Pro has read replicas)

Supabase Pro tier provisions optional read replicas. FK additions and validations propagate via streaming replication (WAL). Per replica, run after each phase's migrations:

```sql
-- On each read replica connection (separate DATABASE_URL if configured):
SELECT
    conname,
    convalidated
FROM pg_constraint
WHERE contype = 'f'
  AND confrelid = 'platform.ticker_classifications'::regclass
ORDER BY conname;
```

Compare against the primary's view. Any divergence indicates replication lag (acceptable, transient) or a replication snag (escalate). The post-Phase-2 expectation: replicas converge to "15 NOT VALID FKs" within seconds. Post-Phase-4: replicas converge to "validated" within seconds of each table's VALIDATE commit.

**Operator note:** if no read replica is provisioned (default Supabase Pro state), this gate is a no-op confirmation that `pg_stat_replication` is empty. Capture either way in PR body.

### 9.5 Alembic round-trip requirement (UNCHANGED from v1 §9.1)

Every migration in the v2 rollout must round-trip cleanly. The Phase 2 bulk migration's downgrade is `op.drop_constraint("fk_<T>_ticker", ...)` × 15 (fast). The Phase 4 VALIDATE migrations have no functional downgrade (you cannot "un-validate" a constraint in Postgres — once validated, it stays validated until dropped); their downgrade is a documentation-only no-op.

### 9.6 Heavy-lane gates (UNCHANGED from v1 §9.5)

Same four gates per PR. Plus Alembic round-trip per §9.5.

---

## 10. The compatibility-view pattern for the `sec_insider_transactions` rename

v1 §10 R7 calls the rename "low risk" but acknowledges 68 grep-hits across `tpcore/` + `scripts/`. v2 confirmed 65 grep-hits today (2026-05-23) under the same scope. v1's mitigation: "Pre-push grep + verify dashboard.py touched + forward-only — no SQL view aliasing the old name." That's silent-failure-prone: any missed import becomes a hard producer crash post-rename.

### 10.1 v2's pattern: rename + compatibility view

```sql
-- Step 1: rename the table.
ALTER TABLE platform.sec_insider_transactions RENAME TO insider_transactions;

-- Step 2: create a view at the old name forwarding to the new.
-- Read-through for legacy SELECT consumers.
CREATE VIEW platform.sec_insider_transactions AS
    SELECT * FROM platform.insider_transactions WHERE source = 'sec';

-- Note: producers that INSERT/UPDATE/DELETE against the old name will fail
-- on the view (it's not updatable by default). This is deliberate — INSERT
-- producers MUST be migrated to the new table name as part of Phase 1. The
-- view exists solely for read-side consumers (dashboards, ad-hoc queries,
-- monitoring scripts) that might be missed in the grep sweep.
```

### 10.2 Sequencing

- **Phase 1, migration step A**: rename + create view + add `source` column on the new table.
- **Phase 1, in-PR code change**: update all KNOWN insert/update/delete paths (`tpcore/sec/edgar_adapter.py`, `tpcore/ingestion/handlers.py`, `tpcore/audit/cross_table.py`, `scripts/ops.py`, all 65 grep-hits triaged by call-site).
- **Phase 1, exit gate**: view is queryable (`SELECT COUNT(*) FROM platform.sec_insider_transactions` returns the SEC subset row count) AND `SELECT COUNT(*) FROM platform.insider_transactions` returns full SEC count (same number, since `source='sec'` is the only data at this point).
- **Post-Phase-1, follow-up PR** (operator's pace): migrate any consumer found to be hitting the view rather than the table; once grep shows zero hits against the view, drop it.

### 10.3 Why this matters under FK addition

If Phase 2 lands the FK `NOT VALID` on `insider_transactions` and a missed consumer is still calling `INSERT INTO platform.sec_insider_transactions (…)` — that call now hits the view, which is not updatable, and fails loudly with `ERROR: cannot insert into view "sec_insider_transactions"`. This is exactly the desired failure mode: loud, immediate, traceable. Without the view, the same call would hit a `relation does not exist` error which can be silently caught by some adapters' try/except blocks. The view turns a silent producer-failure into an explicit one.

---

## 11. The cleanup-template `ctid` fix

v1's plan §7.1 cleanup template (path B example):

```text
WITH orphans AS (
    SELECT c.ctid AS row_id, c.ticker
    FROM platform.<T> c
    WHERE NOT EXISTS (...)
)
DELETE FROM platform.<T>
WHERE ctid IN (SELECT row_id FROM orphans)
```

**`ctid` is row-version-volatile.** It changes on UPDATE and VACUUM. If a concurrent producer UPDATEs a row between the CTE materialisation and the DELETE, the captured `ctid` no longer points to the intended row — the DELETE deletes a different (possibly valid) row, or no row at all. v1 acknowledges this in a note (`Note: ctid is row-version-specific. For large tables, prefer WHERE ticker NOT IN (...)`) but presents `ctid` as the default template.

### 11.1 v2 mandates the `WHERE NOT EXISTS` form

```sql
DELETE FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.ticker
);
-- For tables with non-ticker FK column name (insider_sentiment uses 'symbol'):
DELETE FROM platform.insider_sentiment c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p
    WHERE p.ticker = c.symbol
);
```

Postgres MVCC handles the concurrency correctly: the DELETE sees a snapshot of `ticker_classifications` at statement-start; any row in `<T>` whose ticker isn't in that snapshot is deleted. The `NOT EXISTS (...)` form is also semantically clearer than `NOT IN (...)` and handles NULL correctly.

### 11.2 Path-A (BACKFILL) and Path-C (ARCHIVE-then-DELETE) templates

Same pattern — never `ctid`:

```sql
-- Path A: backfill ticker_classifications from prices_daily distinct set.
INSERT INTO platform.ticker_classifications (ticker, source, last_updated)
SELECT DISTINCT p.ticker, 'backfill', now()
FROM platform.prices_daily p
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications c WHERE c.ticker = p.ticker
)
ON CONFLICT (ticker) DO NOTHING;

-- Path C: archive then delete.
INSERT INTO platform.<T>_archive
SELECT * FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker
);

DELETE FROM platform.<T> c
WHERE NOT EXISTS (
    SELECT 1 FROM platform.ticker_classifications p WHERE p.ticker = c.ticker
);
```

---

## 12. Country-backfill null-tolerance — honest numbers

v1 §4.3 sets a *"`country IS NULL` count is < 5% of rows"* exit gate. This is wildly optimistic. Alpaca's `/v2/assets` documents `country` as frequently null for ETFs and SPACs (Alpaca API reference, 2026: *"country: ISO country code for the asset's primary listing exchange; may be null for funds and certain non-equity instruments"*).

### 12.1 Honest expected nulls

| Asset class | Expected null-rate for `country` |
|---|---|
| Common stock (US-listed) | ~0–2% — should resolve to `US` |
| ADRs | ~5–10% — most resolve to home country; some null |
| **ETFs** | **~30–50% — frequently null in Alpaca** |
| **Closed-end funds** | **~40–60% — frequently null** |
| SPACs / shells | ~30% — frequently null |
| Other (preferred, warrants, units) | variable |

### 12.2 v2 exit gate

Phase 1's exit gate for `country` becomes:

- `country IS NULL` count is **logged in the PR body** along with breakdown by `asset_class`. Operator reviews; no hard threshold.
- A follow-up backfill source (Polygon `/v3/reference/tickers`? OpenFIGI? CCAS Citizenship-by-Class?) is **listed as a Phase 1.5 follow-up TODO** in the PR body but NOT shipped as part of Phase 1.
- The CHECK constraint (`country IS NULL OR country ~ '^[A-Z]{2}$'`) and partial index (`WHERE country IS NOT NULL`) ship as planned.

### 12.3 Task-#15 dependency

Task #15 (per-country insider adapters) needs *enough* tickers to have a `country` value to be useful. Operator decision: if Phase 1's backfill yields < 90% of common-stock rows populated, Task #15 unblocks on Phase 1.5 follow-up backfill rather than on Phase 1 directly. v2 documents this dependency explicitly; v1 did not.

---

## 13. Updated dependency graph (v2)

```text
Phase 0 (audit + index audit + statement_timeout verify)
    └─→ Phase 1 (rename + view + country + classify_tickers DEFERRED)
        └─→ Phase 2 (NOT-VALID-FIRST bulk; ALL 15 FKs added in one PR; loud-fail
            on producer-orphan attempts immediately) ──→ Task #15 unblocks
            └─→ Phase 3 (classify_tickers producer fix — DELETEs now safely
                surface ON DELETE RESTRICT loud-fails for any orphan-creating
                situation)
                └─→ Phase 4 (per-table cleanup-then-VALIDATE, rolling at
                    operator's leisure; ordered light → heavy)
                    └─→ Phase 5 (post-FK verification + producer regression
                        sweep + ERD update + memory item marked DONE)
```

**Phase ordering is strict for Phase 0 → 1 → 2 → 3, then Phase 4 is partially-orderable across tables. Phase 5 depends on all 15 VALIDATEs landing.**

Compared to v1's graph (11 sequential phases, Phase 5 cleanup gating Phase 6/7/8/9 FK adds), v2's collapse hinges on Phase 2 being safe to ship before any cleanup completes. Per §5.1: it is.

---

## 14. Updated risk register

Risk numbers retained from v1 where the risk is unchanged; new v2 risks marked with `-v2`.

| # | Risk | Phase | Mitigation | Change vs v1 |
|---|---|---|---|---|
| **R1** | classify_tickers producer change inadvertently DELETEs valid rows | 3 | Dry-run gate; `|D| > 1%` halt; **NEW v2**: any orphan-creating DELETE fails ON DELETE RESTRICT loud-and-immediate, surfacing the problem before it commits | Strictly better in v2 |
| **R2** | Phase 4 cleanup deletes rows the operator wanted to keep (path B vs A misjudgment) | 4 | Per-table operator sign-off; path C default for any uncertain table | Same as v1 R2 |
| **R3** | Phase 4 VALIDATE on `prices_daily` hits statement timeout | 4 | Phase 0 statement_timeout verification; off-window scheduling; orphan_count = 0 pre-check; `VALIDATE CONSTRAINT` uses `SHARE UPDATE EXCLUSIVE` so concurrent reads + writes OK; if VALIDATE fails the constraint stays `NOT VALID` (still enforced for new rows) — re-run after cleanup, no downgrade needed | Strengthened by Phase 0 timeout verify |
| **R4** | Locks blocking engine queries during VALIDATE | 4 | VALIDATE uses SHARE UPDATE EXCLUSIVE (concurrent reads + writes OK by design); Phase 2's ACCESS EXCLUSIVE phase is brief (per-table, sub-second) | Same as v1 R4 |
| ~~**R5**~~ | ~~Orphan reintroduction race — Tier 2 derived compute runs mid-classify_tickers-DELETE and produces a stale row that's then orphaned by the cascade~~ | ~~3, 9~~ | **GONE in v2.** FK NOT VALID from Phase 2 prevents any new orphan from being INSERTed; the race window closes the moment Phase 2 commits. | **Removed by v2 sequence inversion** |
| **R6** | Derived-table refresh breakage post-FK — e.g. liquidity_tiers populator tries to insert a tier row for a ticker that was just dropped from ticker_classifications | 2, 3 | Producer-side ordering: classify_tickers runs FIRST in the daily cycle (memory `feedback_data_update_first`); derived populators run after. In v2 this also triggers ON DELETE RESTRICT if a derived populator clung to a stale ticker — loud immediate failure surfaces the producer-ordering bug. | Strictly better in v2 |
| **R7** | Phase 1 rename breaks an external dashboard query / dbt script / downstream consumer | 1 | **v2: compatibility view** at the old table name forwards reads (§10). Missed INSERT consumers fail loud (`cannot insert into view`); missed SELECT consumers continue to work via the view; drop the view in a follow-up once grep is zero. | Strictly better in v2 |
| **R8** | A row INSERTed via raw SQL (bypassing the producer) violates the new FK | post-2 | **Acceptable — this is the whole point.** Constraint violation is the desired signal. Earlier in v2 than in v1 (Phase 2 vs Phase 6+) | Same correctness; earlier in v2 |
| **R9** | Alembic head divergence — another db-architect PR lands during this rollout | all | Re-base each phase against fresh main; verify `alembic heads` before each push; abort condition in §9.2 pg_locks monitor catches concurrent migration sessions | Same as v1 R9 |
| ~~**R10**~~ | ~~Phase 8 cleanup-precondition fragile — even one orphan from a producer race condition between Phase 5 and Phase 8 will fail VALIDATE~~ | ~~8~~ | **GONE in v2.** The producer-race window does not exist — FK NOT VALID enforces continuously from Phase 2. | **Removed by v2 sequence inversion** |
| **R11** | Edge-case operator decision drift — `open_orders` / `tradier_options_chains` FK opinion changes mid-rollout | 2 (bulk), 4 (validate) | Operator decision recorded in Phase 0 before Phase 2 bulk begins | Same as v1 R11 |
| **R12** | aar_events / application_log / data_quality_log / risk_state ALSO have ticker references inside JSONB payloads, not columns — operator might expect them in scope | post-5 | Documented as out-of-scope (v1 §3.2); payload-level integrity is a forensics question. | Same as v1 R12 |
| **R13** | A new Tier 2 derived table not in §3.1 list emerges between this spec and Phase 4 final VALIDATE | 4 | Phase 0 audit includes `\dt platform.*` enumeration; reconcile against §3.1 list and adjust before Phase 4 final | Same as v1 R13 |
| **R14-v2** | `pg_constraint.convalidated` mis-reads due to tooling — operator looks at `\d+` and assumes a constraint is invalid when it's actually validated (or vice-versa) | 4 | Use `pg_constraint.convalidated` directly per §9.3, not `\d+` human-readable output | New v2 risk |
| **R15-v2** | Read-replica drift — primary has NOT VALID FK or VALIDATED FK; replica is lagged | 2, 4 | Per §9.4: query each replica post-each-phase; capture lag in PR body; acceptable if transient (< 60s); escalate if persistent | New v2 risk |
| **R16-v2** | Compatibility view discipline lapses — operator forgets to drop the `sec_insider_transactions` view long after grep is zero, leaving a stale view in the schema | 1+ | View existence is monitored; drop-view follow-up task added to Phase 5 acceptance criteria; operator can drop at any point after `grep` returns zero hits | New v2 risk |
| **R17-v2** | Country-backfill null-rate > 50% for an asset class operator cares about (e.g. ADRs) blocks Task #15 unexpectedly | 1, 1.5 | Honest null-rate logged per §12; Phase 1.5 follow-up backfill source enumerated as TODO in PR body; Task #15 unblocks on Phase 1.5 rather than Phase 1 if needed | New v2 risk |

**Net change vs v1 risk register:** removed R5 + R10 (producer-race-window risks eliminated by inversion); strengthened R1/R6/R7 (loud-fail behavior earlier); added R14-v2 / R15-v2 / R16-v2 / R17-v2.

---

## 15. Acceptance criteria — v2 Phase 1 is "done" when

1. **All 15 in-scope tables (§3.1, inherited from v1) have a VALIDATED FK to `ticker_classifications`.** `SELECT COUNT(*) FROM pg_constraint WHERE contype='f' AND confrelid='platform.ticker_classifications'::regclass AND convalidated = true` returns ≥15. (v2-specific: `convalidated = true` check is mandatory, not just constraint presence.)
2. **Phase 2's NOT-VALID-FIRST guarantee verified mid-rollout.** A point-in-time snapshot taken between Phase 2 commit and Phase 4 final shows: at least one FK with `convalidated = false`; smoke test `INSERT INTO <T> (ticker, …) VALUES ('NEVER_EXISTED_XYZ', …)` raises `ForeignKeyViolationError`. (v2-specific: proves the "enforces for new rows while old rows un-validated" property.)
3. **Pre-FK prep work complete** (unchanged from v1 acceptance criterion 2):
   - `sec_insider_transactions` renamed; compatibility view created; `source` column + CHECK constraint exist; v2: drop-view follow-up tracked.
   - `ticker_classifications.country` column + CHECK + partial index exist; null-rate logged per §12.
   - classify_tickers producer applies `A ∩ P` filter (verified by unit test) and DELETEs dropped tickers in same transaction as UPSERT.
4. **Zero orphan rows post-VALIDATE** (unchanged from v1 acceptance criterion 3).
5. **Drift invariant holds** (unchanged from v1 acceptance criterion 4).
6. **DELETE-RESTRICT smoke test passes** for three sample tickers (unchanged from v1).
7. **Test contracts (§8) all pass** including v2's §8.7 NOT-VALID-still-enforces test.
8. **Producer regression sweep clean** (unchanged from v1 acceptance criterion 7).
9. **`tpcore/auditheal` cross-table-audit reads all-green** (unchanged from v1 acceptance criterion 8).
10. **Alembic round-trip verified** for every migration shipped in Phases 0–4 (v2-adjusted; Phase 4's per-table VALIDATE migrations have documentation-only downgrades per §9.5).
11. **Heavy-lane gates green** on every PR (unchanged).
12. **Documentation reconciled** (unchanged from v1 acceptance criterion 11). v2-specific: `docs/DATABASE_AND_DATAFLOW.md` §2.2 note now reads *"Every ticker column FK → `platform.ticker_classifications(ticker)` with `ON UPDATE CASCADE ON DELETE RESTRICT`; landed as NOT VALID in Phase 2 then per-table VALIDATED in Phase 4."*
13. **Verification gates captured in PR bodies** per §9: Phase 0 statement_timeout report; Phase 2 + each-Phase-4 `pg_locks` snapshot + `pg_constraint` confirmation + replica check.

---

## 16. Open questions (resolve in Phase 0)

v1's six open questions (§13) all remain. v2 adds three:

1. **(v2) Read-replica provisioning state.** Does the current Supabase Pro project have read replicas provisioned? If yes, enumerate connection URLs for §9.4 replica checks; if no, document that the replica check is a no-op confirmation that `pg_stat_replication` is empty.
2. **(v2) Role-level `statement_timeout` cap.** Per §9.1: confirm the migration role's `statement_timeout` ceiling; raise via dashboard if needed.
3. **(v2) Country-backfill follow-up source.** Per §12: enumerate candidate sources for Phase 1.5 backfill (Polygon, OpenFIGI, FMP company-profile, manual ETF curation) and operator picks one or defers Task #15 dependency on Phase 1.5.

---

## 17. References

- **Operating contract:** `.claude/agents/db-architect.md`
- **v1 of this spec (superseded by this doc on sequence; preserved as historical record):** `docs/superpowers/specs/2026-05-23-referential-integrity-design.md`
- **v1's implementation plan (sequence superseded by this v2 spec; a follow-up plan v2 will re-template phases):** `docs/superpowers/plans/2026-05-23-referential-integrity-implementation-plan.md`
- **Prior referential work (detection layer):** `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md` + PR #167 (`tpcore/auditheal`).
- **Schema state memory:** `project_database_architecture_state_2026_05_23.md`
- **Schema reference:** `docs/DATABASE_AND_DATAFLOW.md` §2
- **Postgres canonical `NOT VALID` + `VALIDATE CONSTRAINT` pattern:** <https://www.postgresql.org/docs/current/sql-altertable.html#SQL-ALTERTABLE-NOTES>
- **Supabase Pro tier statement_timeout:** memory `project_supabase_pro_tier.md`

---

**END OF SPEC v2.**
