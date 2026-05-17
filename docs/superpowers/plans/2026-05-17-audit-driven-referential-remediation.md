# Audit-Driven Referential Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the detect→act loop on the cross-table referential layer: make `audit_all_tables.py` structured + persisted, then drive its proven-fixable subset (the `cross_ref_cleanup` class) to green via a generic `tpcore/auditheal` loop symmetric to `tpcore/selfheal`, and make the cross-table gate genuinely enforcing.

**Architecture:** Extract the cross-table checks into a tpcore SoT (`tpcore/audit/cross_table.py`) that persists structured rows to `platform.data_quality_log` under `cross_table_audit.<table>.<check_name>` (reusing the `audit_data_pipeline._persist` confidence/stale convention). A new `tpcore/auditheal/` package mirrors `tpcore/selfheal/` 1:1 (RemediationSpec / drift-guarded registry / generic orchestrator / thin `__main__`), reusing `tpcore.selfheal.runner.make_canonical_runner`. The Step-3 wrapper calls `python -m tpcore.auditheal` instead of the print-only script.

**Tech Stack:** Python 3.11, asyncpg, pydantic v2, structlog, pytest (`asyncio_mode=auto`), ruff. Spec: `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `tpcore/audit/__init__.py` | new package marker | P1 |
| `tpcore/audit/cross_table.py` | `CrossTableCheck` model, `CROSS_TABLE_CHECKS` SoT, `CrossTableFinding`, `run_cross_table_audit(pool, *, persist)` | P1 |
| `scripts/audit_all_tables.py` | thin caller: structured checks via the tpcore module + the informational `dump` sections; stdout roll-up preserved | P1 |
| `tpcore/tests/test_cross_table_audit.py` | fake-pool tests for the SoT + persistence convention | P1 |
| `tpcore/auditheal/__init__.py` | package marker | P2 |
| `tpcore/auditheal/spec.py` | `RemediationSpec` (pydantic, frozen) | P2 |
| `tpcore/auditheal/registry.py` | `REMEDIATION_SPECS` + `spec_for` + `registry_drift` | P2 |
| `tpcore/auditheal/orchestrator.py` | generic `run_audit_heal` + `AuditHealOutcome` | P2 |
| `tpcore/auditheal/__main__.py` | thin CLI caller (exit 0/1) | P2 |
| `tpcore/tests/test_auditheal.py` | fake-pool/fake-runner/fake-audit tests, mirrors `test_selfheal.py` | P2 |
| `scripts/run_data_operations.sh` | Step 3 calls `python -m tpcore.auditheal` | P3 |
| `CLAUDE.md`, `TODO.md`, audit docs | reconciliation | P4 |

Each phase = one gated PR. Branch off `origin/main` per phase; CI green before merge; verify branch name before every commit.

---

## Ground truth (verified, do not re-assume)

- `platform.data_quality_log` columns used by `audit_data_pipeline._persist`: `(source, timestamp, latency_ms, missing_bars, stale, confidence, notes)`, unique constraint `(source, timestamp)`, write is `INSERT … ON CONFLICT (source, timestamp) DO NOTHING`.
- Severity→row convention (reuse exactly): `OK → stale=False, confidence=Decimal("1.000")`; `FAIL → stale=True, confidence=Decimal("0.000")`. (WARN is unused for cross-table — every check is binary count==0 / count>0.)
- `cross_ref_cleanup` stage (`scripts/ops.py` `_stage_cross_ref_cleanup`) deletes EXACTLY: (a) `tradier_options_chains WHERE expiration_date < CURRENT_DATE`; (b) `tradier_options_chains tc WHERE NOT EXISTS (SELECT 1 FROM platform.prices_daily_tickers t WHERE t.ticker = tc.ticker)`. Returns `{"deleted_expired_options": int, "deleted_orphan_options": int}`. Takes pool only (no params).
- **Predicate parity (load-bearing, spec §7):** the legacy `audit_all_tables.py` orphan check joined `prices_daily` (`SELECT DISTINCT ticker`). The structured check MUST use the *same predicate `cross_ref_cleanup` deletes* (`NOT EXISTS … prices_daily_tickers`) or remediate→re-audit never converges. The SQL below is already aligned.
- `make_canonical_runner(run_id)` lives in `tpcore/selfheal/runner.py` and is reused as-is.
- `build_asyncpg_pool` is imported `from tpcore.db import build_asyncpg_pool`.
- pytest `asyncio_mode=auto` → `async def test_*` needs no marker.

---

## Phase 1 — Structured cross-table audit (PR 1)

### Task 1.1: Create the `tpcore/audit` package + the cross-table SoT module

**Files:**
- Create: `tpcore/audit/__init__.py`
- Create: `tpcore/audit/cross_table.py`
- Test: `tpcore/tests/test_cross_table_audit.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_cross_table_audit.py`:

```python
"""Unit tests for the structured cross-table audit SoT.

Pure: a fake asyncpg pool whose fetchval returns a scripted count per
check and whose executemany records the persisted rows. No DB.
"""
from __future__ import annotations

from decimal import Decimal

from tpcore.audit.cross_table import (
    CROSS_TABLE_CHECKS,
    CrossTableCheck,
    run_cross_table_audit,
)


class _Conn:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self.persisted: list[tuple] = []

    async def fetchval(self, sql: str):
        # Map the SQL back to a check by identity: the test scripts
        # counts keyed by check_name, embedded as a marker comment.
        for cn, n in self._counts.items():
            if f"/*{cn}*/" in sql:
                return n
        return 0

    async def executemany(self, sql: str, rows) -> None:
        self.persisted.extend(rows)


class _CM:
    def __init__(self, conn: _Conn) -> None:
        self._c = conn

    async def __aenter__(self) -> _Conn:
        return self._c

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, counts: dict[str, int]) -> None:
        self.conn = _Conn(counts)

    def acquire(self) -> _CM:
        return _CM(self.conn)


def test_sot_is_nonempty_and_well_formed() -> None:
    assert CROSS_TABLE_CHECKS, "cross-table SoT must not be empty"
    for c in CROSS_TABLE_CHECKS:
        assert isinstance(c, CrossTableCheck)
        assert c.kind == "violation_count"
        assert c.table and c.check_name and c.sql
        # Each check's SQL carries a /*<check_name>*/ marker so the
        # detector/remediation parity is greppable and test-routable.
        assert f"/*{c.check_name}*/" in c.sql


def test_tradier_orphan_predicate_matches_cross_ref_cleanup() -> None:
    # Convergence guarantee: the orphan check must use the SAME
    # predicate cross_ref_cleanup deletes (NOT EXISTS prices_daily_tickers).
    orphan = next(
        c for c in CROSS_TABLE_CHECKS
        if c.table == "tradier_options_chains"
        and c.check_name == "orphan_no_prices"
    )
    assert "prices_daily_tickers" in orphan.sql
    assert "NOT EXISTS" in orphan.sql.upper()
    expired = next(
        c for c in CROSS_TABLE_CHECKS
        if c.table == "tradier_options_chains"
        and c.check_name == "expiration_in_past"
    )
    assert "expiration_date < CURRENT_DATE" in expired.sql


async def test_run_persists_fail_and_ok_rows_with_convention() -> None:
    # One check red (n=3), everything else green (n=0).
    target = CROSS_TABLE_CHECKS[0]
    pool = _Pool({target.check_name: 3})
    findings = await run_cross_table_audit(pool, persist=True)

    by_key = {(f.table, f.check_name): f for f in findings}
    red = by_key[(target.table, target.check_name)]
    assert red.count == 3 and red.severity == "FAIL"

    # Persisted rows follow the _persist convention exactly.
    persisted = {r[0]: r for r in pool.conn.persisted}
    red_src = f"cross_table_audit.{target.table}.{target.check_name}"
    assert red_src in persisted
    row = persisted[red_src]
    # (source, timestamp, latency_ms, missing_bars, stale, confidence, notes)
    assert row[2] == 0 and row[3] == 0
    assert row[4] is True
    assert row[5] == Decimal("0.000")
    # A green check persists stale=False / confidence=1.000.
    green = next(f for f in findings if f.severity == "OK")
    g_src = f"cross_table_audit.{green.table}.{green.check_name}"
    assert persisted[g_src][4] is False
    assert persisted[g_src][5] == Decimal("1.000")


async def test_persist_false_skips_writes() -> None:
    pool = _Pool({})
    findings = await run_cross_table_audit(pool, persist=False)
    assert findings and pool.conn.persisted == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_cross_table_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.audit'`.

- [ ] **Step 3: Create `tpcore/audit/__init__.py`**

```python
"""Structured platform audits (tpcore SoT for the cross-table layer)."""
```

- [ ] **Step 4: Create `tpcore/audit/cross_table.py`**

```python
"""Structured cross-table referential audit — the single SoT.

Replaces the print-only inline ``q()`` calls in
``scripts/audit_all_tables.py`` with a declared list of checks that
ALSO persist to ``platform.data_quality_log`` (so the auditheal loop
can detect reds), reusing ``audit_data_pipeline._persist``'s exact
severity convention. The stdout roll-up is preserved by the thin
script caller; the informational ``dump`` sections (risk_state /
open_orders / ingestion_jobs) stay in the script — they are not
pass/fail and are intentionally NOT modelled here.

Convergence contract: a check whose violation has a proven canonical
remediation (today: the two ``tradier_options_chains`` checks fixed by
``cross_ref_cleanup``) MUST use the exact predicate that stage deletes,
or remediate→re-audit can never converge. The orphan check therefore
uses ``NOT EXISTS … prices_daily_tickers`` — identical to
``_stage_cross_ref_cleanup``'s delete — not a ``prices_daily`` join.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CrossTableCheck(BaseModel):
    """One declared cross-table violation check. ``sql`` MUST return a
    single integer violation count and MUST embed a ``/*<check_name>*/``
    marker (greppable; lets the detector/remediation parity be audited
    and keeps the SQL self-identifying)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    check_name: str
    sql: str
    kind: Literal["violation_count"] = "violation_count"

    @property
    def key(self) -> str:
        return f"{self.table}/{self.check_name}"


@dataclass(frozen=True)
class CrossTableFinding:
    table: str
    check_name: str
    count: int
    severity: str  # OK | FAIL

    @property
    def source_key(self) -> str:
        return f"cross_table_audit.{self.table}.{self.check_name}"


# Declared SoT. Every entry is a violation_count (0 == clean). The
# tradier_options_chains expired/orphan checks are predicate-aligned
# with _stage_cross_ref_cleanup (see module docstring). Adding a check
# here makes the auditheal registry-drift test fail until a
# remediate-or-escalate decision is recorded (clockwork).
CROSS_TABLE_CHECKS: tuple[CrossTableCheck, ...] = (
    CrossTableCheck(table="earnings_events", check_name="null_ticker",
        sql="SELECT COUNT(*) /*null_ticker*/ FROM platform.earnings_events WHERE ticker IS NULL"),
    CrossTableCheck(table="earnings_events", check_name="null_event_date",
        sql="SELECT COUNT(*) /*null_event_date*/ FROM platform.earnings_events WHERE event_date IS NULL"),
    CrossTableCheck(table="earnings_events", check_name="event_date_far_future",
        sql="SELECT COUNT(*) /*event_date_far_future*/ FROM platform.earnings_events WHERE event_date > CURRENT_DATE + INTERVAL '365 days'"),
    CrossTableCheck(table="earnings_events", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.earnings_events ce LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ce.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="liquidity_tiers", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.liquidity_tiers lt LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = lt.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="liquidity_tiers", check_name="stale_30d",
        sql="SELECT COUNT(*) /*stale_30d*/ FROM platform.liquidity_tiers WHERE last_updated < now() - INTERVAL '30 days'"),
    CrossTableCheck(table="liquidity_tiers", check_name="negative_median_spread",
        sql="SELECT COUNT(*) /*negative_median_spread*/ FROM platform.liquidity_tiers WHERE median_spread_pct < 0"),
    CrossTableCheck(table="liquidity_tiers", check_name="negative_p95_spread",
        sql="SELECT COUNT(*) /*negative_p95_spread*/ FROM platform.liquidity_tiers WHERE p95_spread_pct < 0"),
    CrossTableCheck(table="liquidity_tiers", check_name="nonpositive_observations",
        sql="SELECT COUNT(*) /*nonpositive_observations*/ FROM platform.liquidity_tiers WHERE observations <= 0"),
    CrossTableCheck(table="universe_candidates", check_name="null_engine",
        sql="SELECT COUNT(*) /*null_engine*/ FROM platform.universe_candidates WHERE engine IS NULL"),
    CrossTableCheck(table="universe_candidates", check_name="as_of_date_future",
        sql="SELECT COUNT(*) /*as_of_date_future*/ FROM platform.universe_candidates WHERE as_of_date > CURRENT_DATE"),
    CrossTableCheck(table="universe_candidates", check_name="nonpositive_last_close",
        sql="SELECT COUNT(*) /*nonpositive_last_close*/ FROM platform.universe_candidates WHERE last_close IS NOT NULL AND last_close <= 0"),
    CrossTableCheck(table="universe_candidates", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.universe_candidates uc LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = uc.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="spread_observations", check_name="negative_spread",
        sql="SELECT COUNT(*) /*negative_spread*/ FROM platform.spread_observations WHERE spread_pct < 0"),
    CrossTableCheck(table="spread_observations", check_name="extreme_spread",
        sql="SELECT COUNT(*) /*extreme_spread*/ FROM platform.spread_observations WHERE spread_pct > 0.5"),
    CrossTableCheck(table="spread_observations", check_name="future_observed_at",
        sql="SELECT COUNT(*) /*future_observed_at*/ FROM platform.spread_observations WHERE observed_at > now()"),
    CrossTableCheck(table="risk_state", check_name="null_engine",
        sql="SELECT COUNT(*) /*null_engine*/ FROM platform.risk_state WHERE engine IS NULL"),
    CrossTableCheck(table="corporate_actions", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.corporate_actions ca LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ca.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="fundamentals_quarterly", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.fundamentals_quarterly fq LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = fq.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="tradier_options_chains", check_name="null_ticker",
        sql="SELECT COUNT(*) /*null_ticker*/ FROM platform.tradier_options_chains WHERE ticker IS NULL"),
    # Predicate-aligned with _stage_cross_ref_cleanup (convergence).
    CrossTableCheck(table="tradier_options_chains", check_name="expiration_in_past",
        sql="SELECT COUNT(*) /*expiration_in_past*/ FROM platform.tradier_options_chains WHERE expiration_date < CURRENT_DATE"),
    CrossTableCheck(table="tradier_options_chains", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*orphan_no_prices*/ FROM platform.tradier_options_chains tc WHERE NOT EXISTS (SELECT 1 FROM platform.prices_daily_tickers t WHERE t.ticker = tc.ticker)"),
)

_OK = Decimal("1.000")
_FAIL = Decimal("0.000")


async def run_cross_table_audit(
    pool: asyncpg.Pool, *, persist: bool = True
) -> list[CrossTableFinding]:
    """Run every declared check; optionally persist structured rows to
    data_quality_log under ``cross_table_audit.<table>.<check_name>``
    using the audit_data_pipeline._persist severity convention."""
    run_ts = datetime.now(UTC)
    findings: list[CrossTableFinding] = []
    rows: list[tuple] = []
    async with pool.acquire() as conn:
        for c in CROSS_TABLE_CHECKS:
            raw = await conn.fetchval(c.sql)
            n = int(raw) if raw is not None else 0
            sev = "OK" if n == 0 else "FAIL"
            findings.append(
                CrossTableFinding(c.table, c.check_name, n, sev)
            )
            rows.append((
                f"cross_table_audit.{c.table}.{c.check_name}",
                run_ts, 0, 0,
                sev != "OK",
                _OK if sev == "OK" else _FAIL,
                __import__("json").dumps({
                    "table": c.table, "check_name": c.check_name,
                    "count": n, "severity": sev,
                })[:8000],
            ))
        if persist and rows:
            await conn.executemany(
                """
                INSERT INTO platform.data_quality_log
                    (source, timestamp, latency_ms, missing_bars,
                     stale, confidence, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (source, timestamp) DO NOTHING
                """,
                rows,
            )
    n_red = sum(1 for f in findings if f.severity != "OK")
    logger.info("cross_table_audit.done", checks=len(findings), red=n_red)
    return findings


__all__ = [
    "CROSS_TABLE_CHECKS",
    "CrossTableCheck",
    "CrossTableFinding",
    "run_cross_table_audit",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tpcore/tests/test_cross_table_audit.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint**

Run: `ruff check tpcore/audit/ tpcore/tests/test_cross_table_audit.py`
Expected: `All checks passed!` (fix the `__import__("json")` if ruff objects — replace with a top-level `import json` and use `json.dumps`).

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "feat/auditheal-p1-structured" || exit 1
git add tpcore/audit/__init__.py tpcore/audit/cross_table.py tpcore/tests/test_cross_table_audit.py
git commit -m "feat(audit): structured cross-table audit SoT + persistence"
```

### Task 1.2: Make `scripts/audit_all_tables.py` a thin caller (behaviour preserved)

**Files:**
- Modify: `scripts/audit_all_tables.py` (full rewrite of `main()`; keep the file a script)

- [ ] **Step 1: Rewrite `scripts/audit_all_tables.py`**

Replace the entire file with:

```python
"""Comprehensive audit of every platform table.

Thin caller: the structured cross-table violation checks now live in
``tpcore.audit.cross_table`` (the SoT, persisted to data_quality_log
so the auditheal loop can act on them). This script preserves the
operator-facing stdout roll-up and the informational dump sections
(risk_state / open_orders / ingestion_jobs).

Exit code is intentionally still 0 on violations in this phase — the
honest gate flip is wired in Phase 3 via ``python -m tpcore.auditheal``
(isolated, independently reviewable). A crash / missing DSN still
exits 1.
"""
from __future__ import annotations

import asyncio
import os
import sys

from tpcore.audit.cross_table import run_cross_table_audit
from tpcore.db import build_asyncpg_pool


async def main() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        findings = await run_cross_table_audit(pool, persist=True)
        last_table = None
        for f in findings:
            if f.table != last_table:
                print(f"\n=== {f.table} ===")
                last_table = f.table
            tag = "🟢" if f.severity == "OK" else "🔴"
            print(f"  {tag} {f.check_name:40s} n={f.count}")

        async with pool.acquire() as conn:
            print("\n=== risk_state (dump) ===")
            for r in await conn.fetch(
                "SELECT * FROM platform.risk_state ORDER BY engine"
            ):
                print(f"  • {dict(r)}")
            print("\n=== open_orders (dump) ===")
            for r in await conn.fetch("SELECT * FROM platform.open_orders"):
                print(f"  • {dict(r)}")
            print("\n=== ingestion_jobs (dump) ===")
            for r in await conn.fetch(
                "SELECT job_name, last_status, last_run_at, last_error "
                "FROM platform.ingestion_jobs ORDER BY job_name"
            ):
                err = (r["last_error"] or "")[:80]
                status = r["last_status"] or "<none>"
                print(f"  • {r['job_name']:30s} status={status:10s} "
                      f"last_run={r['last_run_at']}  err={err}")

        n_red = sum(1 for f in findings if f.severity != "OK")
        print(f"\nTOTAL cross-table checks={len(findings)}  🔴 {n_red}")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Verify the script imports cleanly**

Run: `source .venv/bin/activate && python -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('a','scripts/audit_all_tables.py'); m=importlib.util.module_from_spec(s); sys.modules['a']=m; s.loader.exec_module(m); print('audit_all_tables import OK')"`
Expected: `audit_all_tables import OK`.

- [ ] **Step 3: Lint + full collection**

Run: `ruff check scripts/audit_all_tables.py && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1`
Expected: ruff clean; collection succeeds (no import errors).

- [ ] **Step 4: Commit + open PR**

```bash
git add scripts/audit_all_tables.py
git commit -m "feat(audit): audit_all_tables -> thin caller of the structured SoT"
git push -u origin feat/auditheal-p1-structured
gh pr create --title "feat(audit): #186(5) P1 — structured cross-table audit + persistence" --body "Spec docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md §5 P1. audit_all_tables.py now persists structured cross_table_audit.* rows (reusing the _persist convention); stdout + exit-0 behaviour preserved (gate flip is P3). Predicate-aligned with cross_ref_cleanup for convergence."
```

- [ ] **Step 5: Merge on green CI** (`gh pr checks <N> --watch`; squash-merge; `git checkout main && git pull`).

---

## Phase 2 — `tpcore/auditheal` generic loop, landed dark (PR 2)

> Branch: `feat/auditheal-p2-loop` off fresh `main`.

### Task 2.1: `RemediationSpec`

**Files:**
- Create: `tpcore/auditheal/__init__.py` (`"""Audit-driven referential remediation."""`)
- Create: `tpcore/auditheal/spec.py`
- Test: `tpcore/tests/test_auditheal.py` (created here, extended in 2.2–2.3)

- [ ] **Step 1: Write the failing test** — create `tpcore/tests/test_auditheal.py`:

```python
"""Unit tests for tpcore.auditheal — mirrors test_selfheal.py.

Pure: fake pool whose red-set advances per re-audit cycle, fake
run_stage + fake run_audit recorders. No DB, no subprocess.
"""
from __future__ import annotations

import pytest

from tpcore.auditheal.registry import REMEDIATION_SPECS, registry_drift, spec_for
from tpcore.auditheal.spec import RemediationSpec


def test_remediable_requires_stage() -> None:
    with pytest.raises(ValueError, match="remediable=True requires a stage"):
        RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=True)


def test_unremediable_requires_reason() -> None:
    with pytest.raises(ValueError, match="escalate_reason"):
        RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=False)


def test_valid_specs_construct() -> None:
    a = RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=True, stage="cross_ref_cleanup")
    b = RemediationSpec(check_key="t/d", table="t", check_name="d",
                        remediable=False, escalate_reason="no safe delete")
    assert a.stage == "cross_ref_cleanup" and b.remediable is False
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.auditheal`).

Run: `python -m pytest tpcore/tests/test_auditheal.py -q`

- [ ] **Step 3: Create `tpcore/auditheal/__init__.py`** with the one-line docstring above.

- [ ] **Step 4: Create `tpcore/auditheal/spec.py`**

```python
"""``RemediationSpec`` — the declarative per-cross-table-check
remediation contract. Mirrors tpcore.selfheal.spec.HealSpec.

The orchestrator holds zero check-specific logic; all knowledge lives
here as data: whether the violation class has a proven canonical
remediation (``remediable``), which ``ops.py --stage`` performs it,
and (if not) the honest ``escalate_reason``. ``remediable=False`` is
honest, not lazy — most cross-table reds (other-table orphans,
integrity) have NO proven-safe auto-delete.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RemediationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # "<table>/<check_name>" — matches CrossTableCheck.key and the
    # data_quality_log source cross_table_audit.<table>.<check_name>.
    check_key: str
    table: str
    check_name: str
    remediable: bool
    # Canonical ops.py stage performing the bounded remediation.
    stage: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    max_attempts: int = 3
    # Required when remediable is False (honest escalation).
    escalate_reason: str = ""

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        if self.remediable:
            if not self.stage:
                raise ValueError(
                    f"RemediationSpec[{self.check_key}]: remediable=True "
                    "requires a stage"
                )
        elif not self.escalate_reason:
            raise ValueError(
                f"RemediationSpec[{self.check_key}]: remediable=False "
                "requires escalate_reason (honest escalation, not a gap)"
            )


__all__ = ["RemediationSpec"]
```

- [ ] **Step 5: Run the 3 spec tests** — they still fail (`registry` import in the test header is unresolved). That is expected; Task 2.2 adds the registry. Temporarily comment the `from tpcore.auditheal.registry import …` line is NOT allowed (no placeholders) — instead, proceed directly to Task 2.2 in the same PR; the test file is completed there. Run after 2.2.

### Task 2.2: `REMEDIATION_SPECS` registry + drift guard

**Files:**
- Create: `tpcore/auditheal/registry.py`
- Test: append to `tpcore/tests/test_auditheal.py`

- [ ] **Step 1: Append registry tests to `tpcore/tests/test_auditheal.py`**

```python
def test_registry_in_lockstep_with_cross_table_sot() -> None:
    """Clockwork: every CROSS_TABLE_CHECKS key has a deliberate
    RemediationSpec; no missing, no extras. Adding a cross-table check
    fails the build until a remediate/escalate decision is recorded."""
    missing, extra = registry_drift()
    assert missing == set(), f"checks with no RemediationSpec: {missing}"
    assert extra == set(), f"RemediationSpecs for unknown checks: {extra}"


def test_only_tradier_cross_ref_class_is_remediable() -> None:
    remediable = {k for k, s in REMEDIATION_SPECS.items() if s.remediable}
    assert remediable == {
        "tradier_options_chains/expiration_in_past",
        "tradier_options_chains/orphan_no_prices",
    }
    for k in remediable:
        assert REMEDIATION_SPECS[k].stage == "cross_ref_cleanup"


def test_every_spec_self_consistent() -> None:
    for key, s in REMEDIATION_SPECS.items():
        assert s.check_key == key
        if s.remediable:
            assert s.stage
        else:
            assert s.escalate_reason
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.auditheal.registry`).

- [ ] **Step 3: Create `tpcore/auditheal/registry.py`**

```python
"""The single RemediationSpec registry — one entry per cross-table
check. Clockwork: ``test_auditheal`` asserts the key set is EXACTLY
the CROSS_TABLE_CHECKS key set, so adding a cross-table check fails
the build until a deliberate remediate/escalate decision is recorded.

Launch scope (spec §3): ONLY the two tradier_options_chains checks
that ``cross_ref_cleanup`` proves-safe to delete are remediable.
Everything else escalates honestly — no proven-safe auto-delete
exists (deleting an earnings/fundamentals row for a transiently
prices-missing ticker would destroy correct data).
"""
from __future__ import annotations

from tpcore.audit.cross_table import CROSS_TABLE_CHECKS

from .spec import RemediationSpec

_CROSS_REF = {}  # cross_ref_cleanup takes no params

_NO_SAFE_DELETE = (
    "no proven-safe canonical remediation — deleting these rows is not "
    "additive-safe (a ticker transiently absent from prices_daily, a "
    "real integrity defect to investigate, etc.); escalate to the "
    "operator. Honest, not a rollout gap."
)

_REMEDIABLE = {
    "tradier_options_chains/expiration_in_past",
    "tradier_options_chains/orphan_no_prices",
}


def _spec_for_check(table: str, check_name: str) -> RemediationSpec:
    key = f"{table}/{check_name}"
    if key in _REMEDIABLE:
        return RemediationSpec(
            check_key=key, table=table, check_name=check_name,
            remediable=True, stage="cross_ref_cleanup",
            params=dict(_CROSS_REF), max_attempts=2,
        )
    return RemediationSpec(
        check_key=key, table=table, check_name=check_name,
        remediable=False, escalate_reason=_NO_SAFE_DELETE,
    )


REMEDIATION_SPECS: dict[str, RemediationSpec] = {
    c.key: _spec_for_check(c.table, c.check_name)
    for c in CROSS_TABLE_CHECKS
}


def spec_for(check_key: str) -> RemediationSpec | None:
    """RemediationSpec for a ``<table>/<check_name>`` key, or None if
    unknown (treated as escalate — never silently ignored)."""
    return REMEDIATION_SPECS.get(check_key)


def registry_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) vs the CROSS_TABLE_CHECKS key set."""
    known = {c.key for c in CROSS_TABLE_CHECKS}
    have = set(REMEDIATION_SPECS)
    return known - have, have - known


__all__ = ["REMEDIATION_SPECS", "registry_drift", "spec_for"]
```

- [ ] **Step 4: Run all tests so far**

Run: `python -m pytest tpcore/tests/test_auditheal.py -q`
Expected: PASS (6 tests: 3 spec + 3 registry).

- [ ] **Step 5: Commit**

```bash
test "$(git branch --show-current)" = "feat/auditheal-p2-loop" || exit 1
git add tpcore/auditheal/__init__.py tpcore/auditheal/spec.py tpcore/auditheal/registry.py tpcore/tests/test_auditheal.py
git commit -m "feat(auditheal): RemediationSpec + drift-guarded registry"
```

### Task 2.3: Generic `run_audit_heal` orchestrator + `__main__`

**Files:**
- Create: `tpcore/auditheal/orchestrator.py`
- Create: `tpcore/auditheal/__main__.py`
- Test: append to `tpcore/tests/test_auditheal.py`

- [ ] **Step 1: Append orchestrator tests** to `tpcore/tests/test_auditheal.py`:

```python
from tpcore.auditheal.orchestrator import run_audit_heal  # noqa: E402


class _Conn:
    def __init__(self, pool: "_Pool") -> None:
        self._p = pool

    async def fetch(self, sql: str):
        reds = self._p.red_sequence[self._p.cycle]
        self._p.cycle = min(self._p.cycle + 1,
                            len(self._p.red_sequence) - 1)
        return [{"source": f"cross_table_audit.{k.replace('/', '.', 1)}"}
                for k in reds]


class _ACM:
    def __init__(self, c: _Conn) -> None:
        self._c = c

    async def __aenter__(self) -> _Conn:
        return self._c

    async def __aexit__(self, *e) -> None:
        return None


class _Pool:
    """red_sequence[i] = remediable/escalate keys red after the i-th
    re-audit."""

    def __init__(self, red_sequence: list[list[str]]) -> None:
        self.red_sequence = red_sequence or [[]]
        self.cycle = 0

    def acquire(self) -> _ACM:
        return _ACM(_Conn(self))


def _audit(rc: int = 0):
    calls = []

    async def run_audit() -> int:
        calls.append("audit")
        return rc

    run_audit.calls = calls  # type: ignore[attr-defined]
    return run_audit


def _runner(*, fail_stage: str | None = None):
    calls: list[tuple[str, dict]] = []

    async def run_stage(stage: str, params: dict) -> int:
        calls.append((stage, dict(params)))
        return 1 if stage == fail_stage else 0

    run_stage.calls = calls  # type: ignore[attr-defined]
    return run_stage


async def test_green_first_pass() -> None:
    out = await run_audit_heal(_Pool([[]]), _runner(), _audit())
    assert out.green and out.iterations == 1 and out.remediated == []


async def test_remediates_then_green() -> None:
    rs = _runner()
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/expiration_in_past"], []]),
        rs, _audit(),
    )
    assert out.green and out.iterations == 2
    assert ("cross_ref_cleanup", {}) in rs.calls


async def test_unremediable_escalates_immediately() -> None:
    rs = _runner()
    out = await run_audit_heal(
        _Pool([["earnings_events/orphan_no_prices"]]), rs, _audit()
    )
    assert out.green is False
    assert any("no proven-safe" in r for _, r in out.escalated)
    assert rs.calls == []  # never attempted a delete


async def test_unknown_red_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["mystery_table/mystery_check"]]), _runner(), _audit()
    )
    assert out.green is False
    assert any("unknown cross-table red" in r for _, r in out.escalated)


async def test_audit_failure_escalates() -> None:
    out = await run_audit_heal(_Pool([[]]), _runner(), _audit(rc=2))
    assert out.green is False
    assert out.escalated and "cross_table_audit" in out.escalated[0][0]


async def test_failed_remediation_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/orphan_no_prices"]]),
        _runner(fail_stage="cross_ref_cleanup"), _audit(),
    )
    assert out.green is False
    assert any("exited 1" in r for _, r in out.escalated)


async def test_exhaustion_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/expiration_in_past"]]),
        _runner(), _audit(), max_iterations=3,
    )
    assert out.green is False and out.iterations == 3
    assert any("exhausted" in r for _, r in out.escalated)
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.auditheal.orchestrator`).

- [ ] **Step 3: Create `tpcore/auditheal/orchestrator.py`**

```python
"""The generic audit-heal orchestrator — zero check-specific logic.
Mirrors tpcore.selfheal.orchestrator.

Flow (bounded): re-run the structured cross-table audit (refreshes
``cross_table_audit.*`` rows) → read the red set → all green: done →
map each red to its RemediationSpec; any unremediable/unknown red →
escalate the full picture now → else run each distinct canonical
remediation (injected run_stage) → loop up to max_iterations → still
red → escalate ("exhausted"). ``run_audit`` and ``run_stage`` are
injected so the engine is pure + unit-testable.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from .registry import spec_for

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

RunStage = Callable[[str, dict[str, str]], Awaitable[int]]
RunAudit = Callable[[], Awaitable[int]]

DEFAULT_MAX_ITERATIONS = 4

_RED_SQL = """
    WITH latest AS (
        SELECT source, MAX(timestamp) AS t
        FROM platform.data_quality_log
        WHERE source LIKE 'cross_table_audit.%'
        GROUP BY source
    )
    SELECT q.source
    FROM platform.data_quality_log q
    JOIN latest l ON l.source = q.source AND l.t = q.timestamp
    WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
    ORDER BY q.source
"""


class AuditHealOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    green: bool
    iterations: int
    remediated: list[str] = Field(default_factory=list)
    escalated: list[tuple[str, str]] = Field(default_factory=list)


def _source_to_key(source: str) -> str:
    # cross_table_audit.<table>.<check_name> -> "<table>/<check_name>"
    rest = source.removeprefix("cross_table_audit.")
    table, _, check = rest.partition(".")
    return f"{table}/{check}"


async def _red_keys(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RED_SQL)
    return [_source_to_key(r["source"]) for r in rows]


async def run_audit_heal(
    pool: asyncpg.Pool,
    run_stage: RunStage,
    run_audit: RunAudit,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> AuditHealOutcome:
    """Drive the cross-table layer to 100% green or honest escalation."""
    remediated: list[str] = []

    for iteration in range(1, max_iterations + 1):
        arc = await run_audit()
        if arc != 0:
            logger.error("auditheal.audit_failed", rc=arc, iteration=iteration)
            return AuditHealOutcome(
                green=False, iterations=iteration, remediated=remediated,
                escalated=[("cross_table_audit",
                            f"structured audit exited {arc}")],
            )

        reds = await _red_keys(pool)
        if not reds:
            logger.info("auditheal.green", iterations=iteration,
                        remediated=remediated)
            return AuditHealOutcome(green=True, iterations=iteration,
                                    remediated=remediated)

        unremediable: list[tuple[str, str]] = []
        actions: dict[tuple[str, frozenset], tuple[str, dict[str, str]]] = {}
        for key in reds:
            spec = spec_for(key)
            if spec is None:
                unremediable.append(
                    (key, "unknown cross-table red — no RemediationSpec "
                          "(never silently ignored; add a spec)"))
            elif not spec.remediable:
                unremediable.append((key, f"{key}: {spec.escalate_reason}"))
            else:
                k = (spec.stage, frozenset(spec.params.items()))
                actions[k] = (spec.stage, spec.params)

        if unremediable:
            logger.warning("auditheal.escalate_unremediable",
                           iteration=iteration, unremediable=unremediable)
            return AuditHealOutcome(
                green=False, iterations=iteration, remediated=remediated,
                escalated=unremediable,
            )

        for stage, params in actions.values():
            logger.info("auditheal.remediate", stage=stage, params=params,
                        iteration=iteration)
            hrc = await run_stage(stage, params)
            if hrc != 0:
                logger.error("auditheal.remediation_failed", stage=stage,
                             rc=hrc)
                return AuditHealOutcome(
                    green=False, iterations=iteration,
                    remediated=remediated,
                    escalated=[(stage, f"bounded remediation exited {hrc} "
                                       "— cannot heal through a failing "
                                       "remediation")],
                )
            remediated.append(stage)

    final = await _red_keys(pool)
    logger.error("auditheal.exhausted", iterations=max_iterations,
                 still_red=final)
    return AuditHealOutcome(
        green=False, iterations=max_iterations, remediated=remediated,
        escalated=[(k, f"auto-remediation exhausted after "
                       f"{max_iterations} iterations") for k in final],
    )


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "AuditHealOutcome",
    "RunAudit",
    "RunStage",
    "run_audit_heal",
]
```

- [ ] **Step 4: Run the orchestrator tests**

Run: `python -m pytest tpcore/tests/test_auditheal.py -q`
Expected: PASS (13 tests total).

- [ ] **Step 5: Create `tpcore/auditheal/__main__.py`**

```python
"""Thin CLI entrypoint — ``python -m tpcore.auditheal``.

Phase 3 wires this into run_data_operations.sh Step 3 instead of the
print-only run_audit_all_tables.sh. Exit code IS the contract:

* ``0`` — cross-table layer 100% green (after 0+ canonical
  remediations). Step 3 proceeds.
* ``1`` — escalation: a cross-table red auto-remediation could not (or
  must not) fix. Step 3 hard-stops; engines must not trade.

All per-check policy lives in REMEDIATION_SPECS; this file only wires
pool + the in-process structured audit + the canonical runner into the
generic orchestrator. The re-audit is the SAME in-process code path as
the detector (cannot drift).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import structlog

from tpcore.audit.cross_table import run_cross_table_audit
from tpcore.db import build_asyncpg_pool
from tpcore.selfheal.runner import make_canonical_runner

from .orchestrator import run_audit_heal

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("auditheal: DATABASE_URL not set", file=sys.stderr)
        return 1

    run_id = str(uuid.uuid4())
    pool = await build_asyncpg_pool(db_url)

    async def run_audit() -> int:
        # Refresh the structured cross_table_audit.* rows. Same code
        # path the orchestrator's detector reads — no drift.
        await run_cross_table_audit(pool, persist=True)
        return 0

    try:
        outcome = await run_audit_heal(
            pool, make_canonical_runner(run_id), run_audit
        )
    finally:
        await pool.close()

    print("=" * 64)
    print(f"AUDIT-HEAL  green={outcome.green}  "
          f"iterations={outcome.iterations}")
    if outcome.remediated:
        print(f"  remediated via canonical stage(s): "
              f"{', '.join(outcome.remediated)}")
    if outcome.escalated:
        print("  ESCALATED (operator must investigate — engines will "
              "NOT trade):")
        for src, reason in outcome.escalated:
            print(f"    - {src}: {reason}")
    print("=" * 64)
    return 0 if outcome.green else 1


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 6: Import smoke + ruff + full collection**

Run:
```
python -c "import tpcore.auditheal.__main__ as m; print('auditheal __main__ OK', hasattr(m,'main'))"
ruff check tpcore/auditheal/ tpcore/tests/test_auditheal.py
python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1
```
Expected: import OK; ruff clean; full collection succeeds.

- [ ] **Step 7: Commit + PR + merge on green**

```bash
test "$(git branch --show-current)" = "feat/auditheal-p2-loop" || exit 1
git add tpcore/auditheal/orchestrator.py tpcore/auditheal/__main__.py tpcore/tests/test_auditheal.py
git commit -m "feat(auditheal): generic run_audit_heal + thin __main__ (dark)"
git push -u origin feat/auditheal-p2-loop
gh pr create --title "feat(auditheal): #186(5) P2 — generic remediation loop (dark)" --body "Spec §5 P2. tpcore/auditheal mirrors tpcore/selfheal 1:1; reuses make_canonical_runner; landed dark (not wired). Drift-guarded registry == CROSS_TABLE_CHECKS. 13 fake-pool tests."
```
Then `gh pr checks <N> --watch`; squash-merge; `git checkout main && git pull`.

---

## Phase 3 — Wire Step 3 + honest gate (PR 3)

> Branch: `feat/auditheal-p3-wire` off fresh `main`.

### Task 3.1: Step 3 calls `python -m tpcore.auditheal`

**Files:**
- Modify: `scripts/run_data_operations.sh` (Step 3 block, lines ~154-167)

- [ ] **Step 1: Read the current Step 3 block**

Run: `sed -n '154,167p' scripts/run_data_operations.sh` — confirm it matches:
```
# Step 3 — cross-reference audit.
echo ""
echo "▶ STEP 3 / 6  verify cross-table integrity"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_audit
scripts/run_audit_all_tables.sh
AUDIT_RC=$?
if [[ $AUDIT_RC -ne 0 ]]; then
    _log_event INGESTION_FAILED wrapper_audit "audit exited $AUDIT_RC"
    echo "✗ audit_all_tables exited $AUDIT_RC"
    _notify_failure "audit_all_tables" $AUDIT_RC
    exit $AUDIT_RC
fi
_log_event INGESTION_COMPLETE wrapper_audit
```

- [ ] **Step 2: Replace the `scripts/run_audit_all_tables.sh` invocation line**

Replace exactly this line:
```
scripts/run_audit_all_tables.sh
```
with:
```
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m tpcore.auditheal
```
(Leave `AUDIT_RC=$?` and the `-ne 0` hard-stop block unchanged — the thin caller's exit `1` now means "cross-table red the loop could not remediate", which correctly triggers the existing `exit $AUDIT_RC` hard stop. Exit `0` = green after 0+ remediations.)

- [ ] **Step 3: Update the Step 3 comment + echo to reflect the loop**

Replace `# Step 3 — cross-reference audit.` with:
```
# Step 3 — cross-table referential audit + auto-remediation
# (#186(5)). tpcore.auditheal runs the structured cross-table audit,
# auto-runs the proven cross_ref_cleanup remediation for the
# tradier_options_chains expired/orphan class, re-audits, and exits
# 1 on any unremediated/escalate-only red (now an ENFORCED gate —
# previously audit_all_tables always exited 0).
```
and replace the echo text `verify cross-table integrity` with `cross-table audit + auto-remediation`, and the two `audit_all_tables` strings in the failure branch with `auditheal`.

- [ ] **Step 4: Shell sanity check**

Run: `bash -n scripts/run_data_operations.sh && echo "syntax OK"`
Expected: `syntax OK`.

- [ ] **Step 5: Verify the module entrypoint resolves with the repo env**

Run: `source .venv/bin/activate && python -m tpcore.auditheal --help 2>&1 | head -1 || true`
(There is no `--help`; this just confirms `python -m tpcore.auditheal` imports without `ModuleNotFoundError`. Expected: the auditheal banner or a clean DATABASE_URL message — NOT an import traceback.)

- [ ] **Step 6: Commit + PR + merge on green**

```bash
test "$(git branch --show-current)" = "feat/auditheal-p3-wire" || exit 1
git add scripts/run_data_operations.sh
git commit -m "feat(auditheal): #186(5) P3 — Step 3 closes the loop + honest gate"
git push -u origin feat/auditheal-p3-wire
gh pr create --title "feat(auditheal): #186(5) P3 — wire Step 3 + enforce the cross-table gate" --body "Spec §5 P3. Step 3 now calls python -m tpcore.auditheal: structured audit -> auto-remediate the proven cross_ref_cleanup class -> re-audit -> exit 1 on any unremediated red. Net: cross-table gate is now ENFORCED (was detection theatre — always exit 0) and the proven subset auto-clears. run_audit_all_tables.sh/audit_all_tables.py retained (operator can still run manually)."
```
Then `gh pr checks <N> --watch`; squash-merge; `git checkout main && git pull`.

> Note: CI cannot exercise the live cycle. State explicitly in the PR that the behaviour change is verified by the P2 unit tests + the shell syntax check; the first live cycle is the integration proof (monitor the next `run_data_operations.sh` run's Step 3 output).

---

## Phase 4 — Documentation reconciliation (PR 4)

> Branch: `docs/auditheal-p4` off fresh `main`.

### Task 4.1: Reconcile the docs to the shipped reality

**Files:**
- Modify: `CLAUDE.md` (the Step-3 / cross-table-audit + data-layer-acceptance-gate descriptions)
- Modify: `TODO.md` (#186 status)
- Modify: `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md` (Status → BUILT, build record)

- [ ] **Step 1: CLAUDE.md** — find the cross-table-audit sentence in the "Data-layer acceptance gate" bullet:

Run: `grep -n "Cross-table audit\|run_audit_all_tables\|0 violations" CLAUDE.md`

Edit that sentence to state the gate is now enforced via `python -m tpcore.auditheal` (structured audit + bounded `cross_ref_cleanup` auto-remediation + honest exit-1 escalation), symmetric to Step-4 `tpcore.selfheal`. Keep it one sentence, factual, no emojis.

- [ ] **Step 2: TODO.md** — find the #186 reference:

Run: `grep -n "#186\|deterministic data agents\|audit-driven referential" TODO.md`

Add a line under the #186 area: candidate (5) audit-driven referential remediation **DONE 2026-05-17** (`tpcore/auditheal`; structured cross-table audit + bounded cross_ref_cleanup loop + enforced Step-3 gate; PRs P1–P4). Note candidates (3)/(4) largely realized by #165; (6) schema-drift sentinel still open.

- [ ] **Step 3: Spec status** — edit the spec header `**Status:**` line to `**Status:** BUILT 2026-05-17` and append a one-paragraph build record listing the 4 PRs (mirrors the #185 spec's build-record style).

- [ ] **Step 4: Commit + PR + merge on green**

```bash
test "$(git branch --show-current)" = "docs/auditheal-p4" || exit 1
git add CLAUDE.md TODO.md docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md
git commit -m "docs: #186(5) auditheal — reconcile CLAUDE.md/TODO/spec to shipped reality"
git push -u origin docs/auditheal-p4
gh pr create --title "docs: #186(5) auditheal build record + gate reconciliation" --body "Spec §5 P4. CLAUDE.md cross-table gate now described as enforced via tpcore.auditheal; TODO #186(5) marked DONE; spec Status -> BUILT."
```
Then `gh pr checks <N> --watch`; squash-merge; `git checkout main && git pull`.

---

## Self-Review

**1. Spec coverage:**
- §1 problem (theatre + no detect→act) → P1 (persistence) + P3 (honest gate) + P2 (loop). ✓
- §2 architecture 1:1 with selfheal → Tasks 2.1–2.3 mirror spec/registry/orchestrator/__main__; runner reused (`from tpcore.selfheal.runner import make_canonical_runner`). ✓
- §2 structured audit prerequisite → Task 1.1/1.2. ✓
- §3 remediable boundary (strictly the 2 tradier checks; drift-guarded) → `registry.py` `_REMEDIABLE` + `test_only_tradier_cross_ref_class_is_remediable` + `registry_drift`. ✓
- §4 safety (never suppress; mandatory re-audit; strengthens gate; bounded; no double-act — disjoint `cross_table_audit.%` vs `validation.%`) → orchestrator re-audits each iteration; `_RED_SQL` namespace-disjoint from selfheal's; P3 honest exit. ✓
- §5 phasing → 4 phases, one PR each. ✓
- §7 predicate-parity → baked into `CROSS_TABLE_CHECKS` orphan SQL + `test_tradier_orphan_predicate_matches_cross_ref_cleanup`. In-process re-audit → `__main__.run_audit` uses the same `run_cross_table_audit`. source-key format `cross_table_audit.<table>.<check_name>` consistent across module, orchestrator `_source_to_key`, registry keys. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code; Task 2.1 Step 5 explicitly forbids a placeholder and routes to 2.2 in the same PR. ✓

**3. Type consistency:** `CrossTableCheck.key` = `"<table>/<check_name>"`; `CrossTableFinding.source_key` = `cross_table_audit.<table>.<check_name>`; orchestrator `_source_to_key` inverts it (`partition('.')` after stripping prefix → first `.` splits table/check, matching keys with no dots in table/check names — all declared names are dot-free); registry keyed by `CrossTableCheck.key`; `spec_for(check_key)`; `RemediationSpec.check_key` matches. `run_audit_heal(pool, run_stage, run_audit, *, max_iterations)` signature consistent between orchestrator, tests, and `__main__`. ✓

(One consistency note carried forward to execution: every `check_name` and `table` in `CROSS_TABLE_CHECKS` must contain no `.` so `_source_to_key`'s single `partition('.')` is unambiguous — all 22 declared names satisfy this; the drift test + `test_sot_is_nonempty_and_well_formed` guard additions.)
