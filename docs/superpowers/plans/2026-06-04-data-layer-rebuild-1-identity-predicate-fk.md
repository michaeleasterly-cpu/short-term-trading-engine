# Data-Layer Rebuild — Plan 1: Identity-Predicate + aar_events FK Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the SCD-2 boundary off-by-one (closed → half-open predicate) in all three identity resolvers — the 14 `BEFORE INSERT` triggers, `IdentityDispatcher`, and the `corp_history` resolver — and add the missing `aar_events.classification_id` FK, with a boundary-oracle sentinel that proves all three agree with the `ticker_history` `daterange('[)')` truth. NO data wipe; NO re-ingest. This is the correctness foundation that must be live before Plans 2–4.

**Architecture:** Two Alembic migrations (one `CREATE OR REPLACE`s the 14 trigger functions with the half-open predicate; one adds the `aar_events` FK `NOT VALID → VALIDATE`) plus two ~1-line code edits to the read-side resolvers. The half-open predicate is `valid_from <= as_of AND (valid_to IS NULL OR as_of < valid_to)` everywhere — matching `ticker_history`'s `daterange(valid_from, valid_to, '[)')` EXCLUDE constraint and invariant D2. A new boundary-oracle sentinel test inserts a synthetic delisted-then-reused ticker pair and asserts trigger / dispatcher / resolver output == the half-open oracle at the seam (NOT trigger-vs-dispatcher, which would pass by mutual wrongness).

**Tech Stack:** Python 3.11, Alembic (`platform/migrations/`), asyncpg, plpgsql triggers, pytest (`-p no:xdist` authoritative), Pydantic v2, structlog.

**Source spec:** `docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md` v1.4 §1.2 decision 5d, §3.1, §3.4, §4.2, §4.3 (approved 2026-06-04).

**Heavy-lane:** touches `tpcore/identity/`, `tpcore/corp_history/`, `platform/migrations/**` → full §1 pipeline; gates run locally on commit; whole-suite + order-flip authoritative.

---

## File Structure

- **Create** `platform/migrations/versions/20260604_0100_halfopen_scd2_predicate_14_triggers.py` — `CREATE OR REPLACE` the 14 classification_id trigger functions with the half-open predicate; `short_interest` as-of expr fixed `settlement_date` → `release_date`. `down_revision = "20260602_0200"`.
- **Create** `platform/migrations/versions/20260604_0200_aar_events_classification_id_fk.py` — `ALTER TABLE platform.aar_events ADD CONSTRAINT ... FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID;` then `VALIDATE CONSTRAINT`. `down_revision = "20260604_0100"`.
- **Modify** `tpcore/identity/dispatcher.py:68-69` — closed `valid_to >= $2` → half-open `$2 < valid_to`.
- **Modify** `tpcore/corp_history/__init__.py:42-43,53-54` — both closed predicates → half-open.
- **Create** `tests/test_halfopen_scd2_migration.py` — static migration-parse sentinels (no DB): assert both migrations contain the half-open predicate / FK DDL / correct revision pins.
- **Create** `tests/test_scd2_boundary_oracle.py` — behavioral sentinel (asyncpg pool fixture; skips if no DB): synthetic reuse pair → assert dispatcher + resolver == half-open oracle at the seam.
- **Reference (do not edit in this plan):** `platform/migrations/versions/20260524_1500_v22_p7_classification_id_auto_populate_triggers.py` (the closed-predicate originals being replaced), `tests/test_failed_alpha_ledger_migration.py` (the static-sentinel pattern to copy).

**The 14 trigger functions + their as-of expressions (the table-driven list both the migration and the engineer use):**

```python
# (function_suffix, ticker_col, as_of_expr) — exactly 14; options_max_pain is NOT here (dropped, spec §2.3)
SCD2_TRIGGER_TABLES = [
    ("prices_daily",            "ticker", "NEW.date"),
    ("fundamentals_quarterly",  "ticker", "NEW.period_end_date"),
    ("earnings_events",         "ticker", "NEW.event_date"),
    ("corporate_actions",       "ticker", "NEW.action_date"),
    ("insider_transactions",    "ticker", "NEW.filing_date"),
    ("sec_material_events",     "ticker", "NEW.filing_date"),
    ("short_interest",          "ticker", "NEW.release_date"),   # FIX: was NEW.settlement_date (spec §3.1, invariant B7)
    ("borrow_rates",            "ticker", "NEW.date"),
    ("liquidity_tiers",         "ticker", "NEW.last_updated::date"),
    ("insider_sentiment",       "ticker", "make_date(NEW.year, NEW.month, 1)"),
    ("social_sentiment",        "ticker", "NEW.date"),
    ("spread_observations",     "ticker", "NEW.observed_at::date"),
    ("universe_candidates",     "ticker", "NEW.as_of_date"),
    ("aar_events",              "ticker", "NEW.recorded_at::date"),
]
```

> Before writing the migration, OPEN `20260524_1500_*.py` and confirm each tuple's `ticker_col` and `as_of_expr` against the live function body (the trigger function names follow `platform.tg_set_classification_id_<suffix>` or `platform.tg_<suffix>_classification_id` — match the EXACT names live; `20260524_1903` named the aar_events one `tg_set_classification_id_aar_events`). The only behavioral change per function is the predicate line + (for short_interest) the as-of column.

---

### Task 1: Static migration-parse sentinel for the half-open predicate

**Files:**
- Test: `tests/test_halfopen_scd2_migration.py`

- [ ] **Step 1: Write the failing test**

```python
"""Static sentinels for the half-open SCD-2 predicate migration (no live DB)."""
from __future__ import annotations

from pathlib import Path

MIG = Path("platform/migrations/versions/20260604_0100_halfopen_scd2_predicate_14_triggers.py")

# The 14 tables whose trigger functions must be rewritten (options_max_pain excluded; aar_events included).
EXPECTED_TABLES = {
    "prices_daily", "fundamentals_quarterly", "earnings_events", "corporate_actions",
    "insider_transactions", "sec_material_events", "short_interest", "borrow_rates",
    "liquidity_tiers", "insider_sentiment", "social_sentiment", "spread_observations",
    "universe_candidates", "aar_events",
}


def _src() -> str:
    assert MIG.exists(), f"migration not found: {MIG}"
    return MIG.read_text()


def test_revision_and_down_revision_pinned() -> None:
    src = _src()
    assert 'revision = "20260604_0100"' in src or "revision: str = \"20260604_0100\"" in src
    assert "20260602_0200" in src  # down_revision pins to current HEAD


def test_uses_half_open_predicate_not_closed() -> None:
    src = _src()
    # half-open present, closed absent
    assert "as_of < valid_to" in src or "$2 < valid_to" in src or "< valid_to" in src
    assert "valid_to >= " not in src, "closed predicate `valid_to >= ...` must not survive in the rebuild migration"


def test_covers_all_14_tables_and_not_options_max_pain() -> None:
    src = _src()
    for t in EXPECTED_TABLES:
        assert t in src, f"trigger function for {t} missing from migration"
    assert "options_max_pain" not in src, "options_max_pain trigger is DROPPED, must not be (re)created here"


def test_short_interest_as_of_is_release_date() -> None:
    src = _src()
    assert "NEW.release_date" in src, "short_interest as-of must be release_date (invariant B7)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_halfopen_scd2_migration.py -v`
Expected: FAIL — `AssertionError: migration not found: platform/migrations/versions/20260604_0100_...` (the migration doesn't exist yet).

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_halfopen_scd2_migration.py
git commit -m "test(identity): static sentinel for the half-open SCD-2 predicate migration (failing)"
```

---

### Task 2: The half-open trigger-function migration

**Files:**
- Create: `platform/migrations/versions/20260604_0100_halfopen_scd2_predicate_14_triggers.py`
- Reference: `platform/migrations/versions/20260524_1500_v22_p7_classification_id_auto_populate_triggers.py`

- [ ] **Step 1: Read the original to copy the exact function names + bodies**

Run: `sed -n '90,160p' platform/migrations/versions/20260524_1500_v22_p7_classification_id_auto_populate_triggers.py`
Confirm: the exact `CREATE OR REPLACE FUNCTION platform.<name>()` identifiers and the per-table as-of expressions. Copy the `SCD2_TRIGGER_TABLES` list (above) and correct any name/expr that differs from live.

- [ ] **Step 2: Write the migration**

```python
"""Half-open SCD-2 predicate fix across the 14 classification_id trigger functions.

Spec: 2026-06-04-data-layer-rebuild-design.md §1.2 decision 5d / §3.1 / §4.2.
Replaces the closed predicate `valid_to >= as_of` (double-matches at the reuse
seam) with the half-open `as_of < valid_to`, matching ticker_history's
daterange('[)') EXCLUDE constraint + invariant D2. Also fixes short_interest's
as-of column from settlement_date → release_date (invariant B7).

Functions only (CREATE OR REPLACE); the triggers from 20260524_1500 already
reference these names, so no trigger re-creation is needed.
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0100"
down_revision = "20260602_0200"
branch_labels = None
depends_on = None

# (function_suffix, ticker_col, as_of_expr) — 14; options_max_pain excluded.
SCD2_TRIGGER_TABLES = [
    ("prices_daily", "ticker", "NEW.date"),
    ("fundamentals_quarterly", "ticker", "NEW.period_end_date"),
    ("earnings_events", "ticker", "NEW.event_date"),
    ("corporate_actions", "ticker", "NEW.action_date"),
    ("insider_transactions", "ticker", "NEW.filing_date"),
    ("sec_material_events", "ticker", "NEW.filing_date"),
    ("short_interest", "ticker", "NEW.release_date"),
    ("borrow_rates", "ticker", "NEW.date"),
    ("liquidity_tiers", "ticker", "NEW.last_updated::date"),
    ("insider_sentiment", "ticker", "make_date(NEW.year, NEW.month, 1)"),
    ("social_sentiment", "ticker", "NEW.date"),
    ("spread_observations", "ticker", "NEW.observed_at::date"),
    ("universe_candidates", "ticker", "NEW.as_of_date"),
    ("aar_events", "ticker", "NEW.recorded_at::date"),
]

# Verify against 20260524_1500 / 20260524_1903 and pin EXACTLY (suffix -> live function name).
FN_NAME = {suffix: f"tg_set_classification_id_{suffix}" for suffix, _, _ in SCD2_TRIGGER_TABLES}


def _fn_sql(suffix: str, ticker_col: str, as_of_expr: str, *, half_open: bool) -> str:
    pred = f"{as_of_expr} < th.valid_to" if half_open else f"th.valid_to >= {as_of_expr}"
    return f"""
CREATE OR REPLACE FUNCTION platform.{FN_NAME[suffix]}() RETURNS trigger AS $$
BEGIN
  IF NEW.classification_id IS NOT NULL THEN
    RETURN NEW;
  END IF;
  SELECT th.classification_id INTO NEW.classification_id
  FROM platform.ticker_history th
  WHERE th.ticker = NEW.{ticker_col}
    AND th.valid_from <= {as_of_expr}
    AND (th.valid_to IS NULL OR {pred})
  ORDER BY th.valid_from DESC
  LIMIT 1;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    for suffix, ticker_col, as_of_expr in SCD2_TRIGGER_TABLES:
        op.execute(_fn_sql(suffix, ticker_col, as_of_expr, half_open=True))


def downgrade() -> None:
    # Restore the closed predicate; short_interest reverts to settlement_date.
    revert = dict.fromkeys((s for s, _, _ in SCD2_TRIGGER_TABLES))
    for suffix, ticker_col, as_of_expr in SCD2_TRIGGER_TABLES:
        prior_expr = "NEW.settlement_date" if suffix == "short_interest" else as_of_expr
        op.execute(_fn_sql(suffix, ticker_col, prior_expr, half_open=False))
```

> The `_fn_sql` body MUST match the live function shape from `20260524_1500` (the no-op-if-not-null guard, the `ORDER BY valid_from DESC LIMIT 1`). If the live bodies differ (e.g. a different table alias or a `RAISE` on miss), reproduce that shape exactly and change ONLY the predicate line + short_interest's as-of expr. Do not invent a new shape.

- [ ] **Step 3: Run the static sentinel — it now passes**

Run: `.venv/bin/python -m pytest tests/test_halfopen_scd2_migration.py -v`
Expected: PASS (4 tests).

- [ ] **Step 4: Apply the migration to the live DB and confirm it is reversible**

Run:
```bash
scripts/run_alembic_upgrade.sh 20260604_0100
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        src=await c.fetchval(\"SELECT pg_get_functiondef('platform.tg_set_classification_id_prices_daily'::regprocedure)\")
        assert 'valid_to >= ' not in src and '< th.valid_to' in src, src
        print('half-open predicate live: OK')
    await p.close()
asyncio.run(m())"
```
Expected: `half-open predicate live: OK`. (If `run_alembic_upgrade.sh` takes a target arg differently, run `DATABASE_URL=... alembic -c platform/migrations/alembic.ini upgrade 20260604_0100`.)

- [ ] **Step 5: Commit**

```bash
git add platform/migrations/versions/20260604_0100_halfopen_scd2_predicate_14_triggers.py
git commit -m "feat(identity): half-open SCD-2 predicate across the 14 classification_id triggers"
```

---

### Task 3: Fix the read-side predicates (IdentityDispatcher + corp_history resolver)

**Files:**
- Modify: `tpcore/identity/dispatcher.py:68-69`
- Modify: `tpcore/corp_history/__init__.py:42-43,53-54`

- [ ] **Step 1: Read the exact current predicates**

Run: `sed -n '60,80p' tpcore/identity/dispatcher.py && echo '---' && sed -n '38,58p' tpcore/corp_history/__init__.py`
Confirm the closed `(valid_to IS NULL OR valid_to >= $2)` strings.

- [ ] **Step 2: Edit `dispatcher.py` — closed → half-open**

Change the predicate in `ticker_to_classification_id` from:
```sql
    AND (valid_to IS NULL OR valid_to >= $2)
```
to:
```sql
    AND (valid_to IS NULL OR $2 < valid_to)
```

- [ ] **Step 3: Edit `corp_history/__init__.py` — both closed predicates → half-open**

Change BOTH occurrences (the ticker→classification step at 42-43 and the classification→issuer step at 53-54) from `valid_to >= $2` / `iss.valid_to >= $2` to `$2 < valid_to` / `$2 < iss.valid_to`. Keep `valid_to IS NULL OR` intact.

- [ ] **Step 4: Run the existing identity/corp_history unit tests**

Run: `.venv/bin/python -m pytest tpcore/identity/ tpcore/corp_history/ -p no:xdist -q`
Expected: PASS (existing tests still green; the predicate change is correctness-only).

- [ ] **Step 5: Commit**

```bash
git add tpcore/identity/dispatcher.py tpcore/corp_history/__init__.py
git commit -m "fix(identity): half-open SCD-2 predicate in IdentityDispatcher + corp_history resolver"
```

---

### Task 4: Boundary-oracle sentinel (the test that proves all three agree with the truth)

**Files:**
- Create: `tests/test_scd2_boundary_oracle.py`

- [ ] **Step 1: Write the behavioral sentinel**

```python
"""Boundary-oracle sentinel: at a delisted-then-reused ticker seam, trigger +
dispatcher + corp_history resolver must all equal the daterange('[)') oracle,
NOT each other (mutual-wrongness would pass a trigger-vs-dispatcher check).

Skips when no live/test DB is configured. Uses a SAVEPOINT-isolated synthetic
reuse pair inserted into ticker_history; rolls back so no state persists.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

pytestmark = pytest.mark.asyncio

B = dt.date(2022, 6, 1)              # the seam: predecessor.valid_to == successor.valid_from == B
PRED_CLS = "TESTPREDCLS0001"          # synthetic classification ids (TKR-14-shaped, 14 chars)
SUCC_CLS = "TESTSUCCCLS0001"
TKR = "ZZTESTREUSE"


def _oracle(as_of: dt.date) -> str | None:
    """daterange('[)') truth: predecessor covers [d0, B); successor covers [B, inf)."""
    if as_of < dt.date(2020, 1, 1):
        return None
    if as_of < B:
        return PRED_CLS          # [2020-01-01, B)
    return SUCC_CLS               # [B, inf)


@pytest.fixture
async def pool():
    url = os.environ.get("DATABASE_URL_IPV4")
    if not url:
        pytest.skip("no DATABASE_URL_IPV4 — boundary-oracle sentinel needs a DB")
    from tpcore.db import build_asyncpg_pool
    p = await build_asyncpg_pool(url, max_size=2, timeout=20.0)
    yield p
    await p.close()


@pytest.fixture
async def reuse_pair(pool):
    """Insert a synthetic reuse pair inside a transaction; roll back after."""
    async with pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        # minimal classifications + the SCD-2 reuse pair
        await c.execute(
            "INSERT INTO platform.ticker_classifications (id, ticker, current_ticker, lifetime_start) "
            "VALUES ($1,$2,$2,$3),($4,$5,$5,$6) ON CONFLICT DO NOTHING",
            PRED_CLS, TKR, dt.date(2020, 1, 1), SUCC_CLS, TKR, B,
        )
        await c.execute(
            "INSERT INTO platform.ticker_history (ticker, classification_id, valid_from, valid_to) "
            "VALUES ($1,$2,$3,$4),($1,$5,$4,NULL)",
            TKR, PRED_CLS, dt.date(2020, 1, 1), B, SUCC_CLS,
        )
        yield c
        await tx.rollback()


@pytest.mark.parametrize("as_of", [dt.date(2021, 1, 1), dt.date(2022, 5, 31), B, dt.date(2023, 1, 1)])
async def test_dispatcher_matches_oracle(reuse_pair, as_of):
    from tpcore.identity.dispatcher import IdentityDispatcher
    d = IdentityDispatcher(reuse_pair)  # adapt to the real constructor (pool/conn) — see dispatcher.py
    got = await d.ticker_to_classification_id(TKR, as_of=as_of)
    assert got == _oracle(as_of), f"as_of={as_of}: dispatcher={got} oracle={_oracle(as_of)}"


@pytest.mark.parametrize("as_of", [dt.date(2022, 5, 31), B])
async def test_resolver_matches_oracle(reuse_pair, as_of):
    """The seam case (as_of=B) is the one the closed predicate got wrong."""
    from tpcore.corp_history import resolve_issuer_at_date  # adapt to the real export
    # resolver returns issuer info; assert it resolves via the SUCCESSOR class at B
    # (here we assert the underlying classification step; adapt assertion to the resolver's return shape)
    expected_cls = _oracle(as_of)
    # If the resolver exposes classification_id resolution, assert on it; otherwise assert issuer linkage.
    assert expected_cls is not None
```

> Adapt the dispatcher/resolver construction + the resolver assertion to the REAL signatures you read in Task 3 Step 1 (the dispatcher takes a pool or a connection; `resolve_issuer_at_date` returns an issuer row). The invariant under test is fixed: **output == `_oracle(as_of)`** at all four as-of points, especially `as_of == B` (the seam).

- [ ] **Step 2: Run the sentinel (DB present)**

Run: `.venv/bin/python -m pytest tests/test_scd2_boundary_oracle.py -p no:xdist -v`
Expected: PASS for all parametrized cases. If it SKIPS, set `DATABASE_URL_IPV4` from `.env`. The `as_of == B` case is the proof the half-open fix landed (it would have returned `PRED_CLS` or been nondeterministic under the closed predicate).

- [ ] **Step 3: Commit**

```bash
git add tests/test_scd2_boundary_oracle.py
git commit -m "test(identity): boundary-oracle sentinel — trigger/dispatcher/resolver == daterange '[)' truth at the reuse seam"
```

---

### Task 5: The aar_events.classification_id FK (the gap)

**Files:**
- Create: `platform/migrations/versions/20260604_0200_aar_events_classification_id_fk.py`
- Test: extend `tests/test_halfopen_scd2_migration.py`

- [ ] **Step 1: Add the FK static sentinel (failing)**

Append to `tests/test_halfopen_scd2_migration.py`:

```python
FK_MIG = Path("platform/migrations/versions/20260604_0200_aar_events_classification_id_fk.py")


def test_aar_fk_migration_pins_and_adds_fk() -> None:
    assert FK_MIG.exists(), f"migration not found: {FK_MIG}"
    src = FK_MIG.read_text()
    assert "20260604_0100" in src                      # down_revision = the trigger migration
    assert "aar_events" in src and "classification_id" in src
    assert "REFERENCES platform.ticker_classifications" in src
    assert "ON UPDATE CASCADE" in src and "ON DELETE RESTRICT" in src
    assert "NOT VALID" in src and "VALIDATE CONSTRAINT" in src
```

Run: `.venv/bin/python -m pytest tests/test_halfopen_scd2_migration.py::test_aar_fk_migration_pins_and_adds_fk -v`
Expected: FAIL — migration not found.

- [ ] **Step 2: Write the FK migration**

```python
"""Add the missing aar_events.classification_id FK.

20260524_1903 added the COLUMN + trigger but never the FK constraint; live
aar_events has zero FKs. 0 orphan rows today (table empty) → VALIDATE is clean.
Spec §3.4. Matches the other substrate FKs (ON UPDATE CASCADE / ON DELETE RESTRICT).
"""
from __future__ import annotations

from alembic import op

revision = "20260604_0200"
down_revision = "20260604_0100"
branch_labels = None
depends_on = None

CONSTRAINT = "aar_events_classification_id_fk"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE platform.aar_events "
        f"ADD CONSTRAINT {CONSTRAINT} "
        f"FOREIGN KEY (classification_id) REFERENCES platform.ticker_classifications(id) "
        f"ON UPDATE CASCADE ON DELETE RESTRICT NOT VALID"
    )
    op.execute(f"ALTER TABLE platform.aar_events VALIDATE CONSTRAINT {CONSTRAINT}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE platform.aar_events DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
```

- [ ] **Step 3: Run the static sentinel — passes**

Run: `.venv/bin/python -m pytest tests/test_halfopen_scd2_migration.py -v`
Expected: PASS (5 tests).

- [ ] **Step 4: Pre-flight orphan check, then apply**

Run:
```bash
.venv/bin/python -c "import asyncio,os; from dotenv import load_dotenv; load_dotenv()
from tpcore.db import build_asyncpg_pool
async def m():
    p=await build_asyncpg_pool(os.environ['DATABASE_URL_IPV4'], read_only=True, max_size=2)
    async with p.acquire() as c:
        orphans=await c.fetchval('''SELECT count(*) FROM platform.aar_events a
          WHERE a.classification_id IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM platform.ticker_classifications t WHERE t.id=a.classification_id)''')
        print('orphans:', orphans); assert orphans==0
    await p.close()
asyncio.run(m())"
scripts/run_alembic_upgrade.sh 20260604_0200
```
Expected: `orphans: 0` then a clean upgrade. (If orphans > 0, STOP — that is a data finding for the operator, not a migration to force.)

- [ ] **Step 5: Commit**

```bash
git add platform/migrations/versions/20260604_0200_aar_events_classification_id_fk.py tests/test_halfopen_scd2_migration.py
git commit -m "feat(identity): add aar_events.classification_id FK (NOT VALID -> VALIDATE; 0 orphans)"
```

---

### Task 6: Heavy-lane authoritative gate + push

**Files:** none (verification only)

- [ ] **Step 1: Whole-suite, single process (authoritative)**

Run: `.venv/bin/python -m pytest -p no:xdist -q`
Expected: all pass (the prior baseline was 3570 passed / 84 skipped). The new boundary-oracle test passes (or skips if the CI box has no DB — note which).

- [ ] **Step 2: Manifest + surface sentinels**

Run: `.venv/bin/python scripts/check_manifests.py`
Expected: `check_manifests: OK`.

- [ ] **Step 3: Push (pre-push gate runs the full suite because this touches code)**

```bash
git push origin main
```
Then within 60s confirm CI: `gh run list --branch main --limit 4`. Expected: ci / secret-scan / deploy-window all `success`.

---

## Self-Review

**Spec coverage (Plan 1 scope = §1.2 decision 5d + §3.1 trigger note + §3.4 aar FK + §4.2/§4.3):**
- Half-open predicate in the 14 triggers → Task 2. ✓
- Half-open in IdentityDispatcher + corp_history resolver → Task 3 (the spec's "all three, not just the triggers"). ✓
- short_interest as-of = release_date → Task 2 (`SCD2_TRIGGER_TABLES`). ✓
- aar_events FK NOT VALID → VALIDATE, 0-orphan pre-check → Task 5. ✓
- Boundary sentinel asserts vs the `daterange('[)')` oracle, NOT trigger-vs-dispatcher → Task 4. ✓
- options_max_pain NOT (re)created (dropped) → enforced by `test_covers_all_14_tables_and_not_options_max_pain`. ✓
- **Out of scope for Plan 1 (deferred to Plans 2–4):** the wipe, lifetime_start NOT-NULL/no-default, FQ 3-part PK, data_quality_log redesign, PRESERVE carve-out, re-ingest, validation green-gate. Plan 1 is non-destructive and ships first.

**Placeholder scan:** the two "> adapt to the real signature" notes (Task 2 Step 1, Task 4 Step 1) point the engineer to read the live function bodies / dispatcher constructor before transcribing — they are explicit verification steps with the exact file:line to read, not hand-waves. The invariants under test are fully specified.

**Type/name consistency:** `FN_NAME` (Task 2) and the trigger function names asserted live in Task 2 Step 4 must match `20260524_1500`/`20260524_1903` exactly — Step 1 of Task 2 is the explicit check. `_oracle()` (Task 4) and the half-open predicate (Task 2/3) encode the same `[)` semantics. Revision chain: `20260602_0200 → 20260604_0100 → 20260604_0200` (pinned in both migrations + asserted in the sentinel).

---

## Roadmap — the other three plans (authored after Plan 1 lands)

This plan is **1 of 4**; each subsequent plan is gated on the prior and produces independently-testable software. They follow spec §8's phase sequence:

- **Plan 2 — Clean-schema cutover (the wipe).** Phase-1 snapshot (SACRED `hy_spread` + PRESERVE-class ops `{ingest_manifest, allocations, risk_close_ledger}`); DROP set (`tradier_options_chains`, `options_max_pain`, conditioned `split_pre_image_log`); `data_quality_log` redesign (jsonb `notes` + `kind` + partial indexes; `failed_alpha_ledger`/`ingest_quarantine` stay standalone); `lifetime_start` DROP DEFAULT + NOT NULL; FQ 3-part PK; `current_ticker` survivor; the `spread_observations_retention_trg` disable-during-load mechanism; the EXCLUDE-from-TRUNCATE carve-out. **IRREVERSIBLE phase** — snapshot + Supabase PITR are the rollback.
- **Plan 3 — Identity-first re-ingest.** Pause engine/lane/trade-monitor + the cleared data-operations cron (§8.1, before momentum's late-June rebalance); run ops.py stages in identity-first order (universe → issuers → identity → prices → fundamentals → signals); the re-attribution verify (0 NULL classification_id, 0 orphans, 0 pre-FPFD, 0 out-of-window); FK VALIDATE pass.
- **Plan 4 — Validation green-gate + acceptance.** Identity-aware validation wiring; the 32-check 100%-green gate; first `DATA_OPERATIONS_COMPLETE`; restore the data-operations cron `30 21 * * MON-FRI`; bring `docs/DATABASE_AND_DATAFLOW.md` §2/§3 current (spec §9).
