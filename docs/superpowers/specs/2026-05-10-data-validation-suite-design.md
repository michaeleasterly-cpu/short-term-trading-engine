# Data Validation Suite — Design

**Date:** 2026-05-10
**Status:** Approved (design); ready for implementation plan
**Master plan reference:** §6.3 Data Quality Gates
**Scope:** Build the `DataValidationSuite` that gates engine graduation from paper to live. MVP only — manual-fixture sources; EDGAR-backed sources deferred behind an interface.

---

## 1. Goals & non-goals

### Goals
- Enforce three correctness checks on `platform.prices_daily` before any engine graduates to live capital: delistings, S&P 500 constituent snapshot, splits.
- Run on a weekly Railway cron, write per-check results to `platform.data_quality_log`, expose a `capital_gate.assert_passed` helper for the engines.
- Ship with hand-curated YAML fixtures as the source of truth for ground-truth events.
- Keep the splits source behind an interface so an EDGAR-backed implementation can replace it later.

### Non-goals (deferred)
- Real-time data feed health checks — that is the role of the existing `DataQualityScore` model, separate from this suite.
- Automated S&P 500 constituent scraping (Wikipedia / SPDR holdings) — fixture refresh stays manual.
- Historical month-end constituent comparison — master plan only commits to "snapshot + recent removals" for MVP.
- Reverse splits, complex split histories with multiple events per ticker.
- A new schema migration — results reuse `platform.data_quality_log` via documented field-mapping.

---

## 2. Architecture & module layout

```
tpcore/quality/validation/
    __init__.py              # re-exports run_suite, CheckResult, SuiteResult
    models.py                # Pydantic v2: CheckResult, SuiteResult
    suite.py                 # run_suite() orchestrator; writes to data_quality_log
    cli.py                   # python -m tpcore.quality.validation
    capital_gate.py          # assert_passed(); ValidationStaleError, ValidationFailedError
    sources/
        __init__.py
        splits.py            # SplitsSource ABC + FixtureSplitsSource
        delistings.py        # DelistingsSource ABC + FixtureDelistingsSource
        constituents.py      # ConstituentSource ABC + FixtureConstituentSource
    checks/
        __init__.py
        delistings.py        # check_delistings(pool, source) -> CheckResult
        constituent.py       # check_constituent_snapshot(pool, source) -> CheckResult
        splits.py            # check_splits(pool, source) -> CheckResult
    fixtures/
        delistings.yaml      # 10–15 known delistings
        splits.yaml          # 10–15 known splits
        constituents.yaml    # current S&P 500 + ~20 recent removals
    tests/
        __init__.py
        conftest.py
        test_models.py
        test_check_delistings.py
        test_check_constituent.py
        test_check_splits.py
        test_capital_gate.py
        test_suite_e2e.py

ops/
    cron_validation.py       # Railway cron entry point
```

### Boundaries
- **`checks/*`** are pure async functions: `(pool, source) -> CheckResult`. Easy to test against a fake pool.
- **`sources/*`** is the swap point: each source has an ABC plus a `Fixture*` concrete implementation. Future EDGAR adapters slot in here.
- **`suite.py`** owns wiring (instantiate sources, run checks in parallel, persist results).
- **`capital_gate.py`** is one async function the engines import to gate live graduation.
- **`ops/cron_validation.py`** is the Railway entry point. Mirrors the shape of `sigma/scheduler.py` and `reversion/scheduler.py`.

---

## 3. The three checks

### 3.1 `check_delistings`

For each entry in `fixtures/delistings.yaml`, assert:
1. The ticker (or any of `alt_tickers`) exists in `platform.prices_daily`.
2. At least one row for the matched ticker has `delisted = true`.
3. `delisting_date` is non-null and within ±5 trading days of the fixture's recorded date.
4. The last bar's date for the matched ticker is within ±5 trading days of the recorded delisting date (catches "we have the row but only ancient bars").

**`alt_tickers` semantics.** Some delistings change the ticker between active and inactive states (e.g. `SIVB` → `SIVBQ` post-bankruptcy). Each fixture entry has a primary `ticker` and an optional `alt_tickers` list. The check passes if **any** of `[ticker] + alt_tickers` satisfies all four conditions above. Failure is reported against the primary ticker.

**Why ±5 trading days.** Alpaca's inactive-asset endpoint does not expose delisting dates (`tpcore/data/ingest_alpaca_bars.py:64`). The bootstrap infers from the last bar, so an exact match is unrealistic. ±5 trading days absorbs the inference error while still catching real bugs (months of drift, missing rows, etc.).

### 3.2 `check_constituent_snapshot`

Load `fixtures/constituents.yaml` containing (a) current S&P 500 tickers and (b) ~20 hand-picked recent removals (the would-be survivorship-bias set).

For each **current S&P 500** ticker:
1. Must exist in `platform.prices_daily`.
2. Must have a bar dated within the last 5 trading days from `now()` (proves the daily ingestion is actually running).

For each **recent removal**:
1. Must exist in `platform.prices_daily`.
2. If the fixture says `expect_delisted: true`, must have `delisted = true` for at least one row.

The current S&P 500 list is vendored once (manual fixture edit) and refreshed by the operator when desired. The master plan only commits to "snapshot + recent removals" for MVP, so an out-of-date snapshot is acceptable as long as the recent-removal set covers the survivorship cases the operator most cares about.

### 3.3 `check_splits`

For each entry in `fixtures/splits.yaml`, assert:
1. The ticker has bars on both `split_date - 1` (last trading day before) and `split_date`.
2. The ratio `close[split_date - 1] / close[split_date]` is in `[0.85, 1.15]`.

**Why `[0.85, 1.15]`.** Alpaca's ingestion uses `adjustment="all"` (`tpcore/data/ingest_alpaca_bars.py:88`), which *should* return split- and dividend-adjusted prices. The signal we want from this check is "is the data adjusted at all?" A raw, unadjusted feed yields a ratio equal to the split factor (4.0 for a 4:1, 20.0 for AMZN 20:1, etc.) — orders of magnitude outside `[0.85, 1.15]`. The wider ±15% band tolerates *real* day-over-day price action on split days, which can be substantial (TSLA's 5:1 in 2020 had a +12.5% real return across the split day, yielding an adjusted ratio of 0.888). The original ±1% spec false-positived on ordinary split-day moves and added no extra signal — anything in `[0.85, 1.15]` is plainly adjusted, anything near the split factor is plainly raw.

**Known Alpaca free-tier limitation:** the IEX feed is inconsistent about which symbols' historical bars get split-adjusted. AAPL's 2020-08-31 4:1 split is *not* applied at the API level even with `adjustment="all"` (verified all four `adjustment` modes return the raw value), while TSLA's, NVDA's, AMZN's, GOOGL's, and WMT's are correctly adjusted. This is a documented residual; addressing it requires either the paid SIP feed or pulling splits from `/v2/corporate_actions` and applying them during ingestion.

**Reverse splits and multi-event histories** are out of MVP scope — the fixture only includes simple forward splits.

---

## 4. Data model

### `CheckResult` (Pydantic v2, frozen)

```python
class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str                        # "delistings" | "constituent" | "splits"
    passed: bool
    total: int                       # fixture entries evaluated
    failed: int                      # fixture entries that failed
    duration_ms: int
    failures: list[FailureDetail]    # detailed reasons; serialized to data_quality_log.notes
```

### `FailureDetail` (Pydantic v2)

```python
class FailureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    reason: str                      # "missing" | "not_delisted" | "date_drift" | "ratio_off" | etc.
    expected: str | None
    observed: str | None
```

### `SuiteResult` (Pydantic v2, frozen)

```python
class SuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    started_at: datetime             # UTC
    finished_at: datetime            # UTC
    checks: list[CheckResult]
    passed: bool                     # all(c.passed for c in checks)
```

---

## 5. Data flow

### Cron + CLI runtime

```
Railway cron (Sunday 06:00 UTC, weekly)
    │
    └─> ops/cron_validation.py
          ├─ load .env
          ├─ pool = build_asyncpg_pool(DATABASE_URL)
          ├─ result = await run_suite(pool)
          ├─ print_report(result)              (human-readable to stdout)
          ├─ ping Healthchecks                 (success URL on pass, /fail on fail)
          └─ exit 0 if result.passed else 1
```

### `run_suite(pool)`

1. Build `FixtureDelistingsSource`, `FixtureConstituentSource`, `FixtureSplitsSource`.
2. `asyncio.gather(check_delistings, check_constituent_snapshot, check_splits)` — parallelism is safe; reads only, different rows.
3. For each `CheckResult`, call `DataQualityWriter.write(score)` — one row per check.
4. Build and return `SuiteResult`.

### `data_quality_log` field mapping

The existing `DataQualityScore` model is shaped for streaming feed health, so each field is mapped explicitly:

| Field | Validation suite usage |
|---|---|
| `source` | `"validation.delistings"`, `"validation.constituent"`, `"validation.splits"` |
| `timestamp` | `SuiteResult.started_at` |
| `latency_ms` | `CheckResult.duration_ms` |
| `missing_bars` | `CheckResult.failed` (re-purposed; documented in code comment) |
| `stale` | `not CheckResult.passed` |
| `confidence` | `Decimal(total - failed) / Decimal(total)` (or `Decimal(0)` when `total == 0`) |
| `source_freshness_days` | `None` (not meaningful for the suite) |
| `notes` | JSON-serialized `CheckResult.failures` |

This avoids an Alembic migration. The `notes` JSON is consumed only by the suite's CLI and `capital_gate.assert_passed` — no other reader looks inside.

### Capital Gate path

```python
# tpcore/quality/validation/capital_gate.py
async def assert_passed(pool, *, max_age_days: int = 7) -> None:
    """Raises if the most recent suite run is older than max_age_days,
    or if any of its checks failed."""
```

Implementation: query `platform.data_quality_log` for the latest timestamp grouped by `source LIKE 'validation.%'`. Two distinct error types:
- `ValidationStaleError` — no row newer than `max_age_days`, or no rows at all.
- `ValidationFailedError` — recent run exists but any check has `stale = true`.

Sigma's and Reversion's `capital_gate.py` plugs gain one line — `await assert_passed(pool)` — before returning the "graduated" capital tier. The pre-grad cap path is unaffected; the gate only kicks in on graduation.

`max_age_days = 7` pairs with weekly cron: Sunday 06:00 UTC run covers the entire following week.

---

## 6. Error handling

Per `STYLE_GUIDE.md` (fail loud; never `except Exception: pass`).

| Failure mode | Behavior |
|---|---|
| DB connection error | Bubble up; cron exits 1; Healthchecks /fail. |
| Fixture file missing or malformed | Bubble up at suite startup. Config bug, fail loud. |
| Fixture file empty (no entries) | Treated as malformed — bubble up at suite startup. An empty fixture would otherwise trivially "pass" the check and silently disable the gate. |
| Single check raises an unexpected exception | `run_suite` wraps each check in a try/except and produces a `CheckResult(passed=False, failures=[FailureDetail(reason="exception", ...)])` with the traceback in `failures`. Other checks still run. The suite as a whole returns `passed=False`. A `structlog` event is logged: `validation.check.exception`. |
| `DataQualityWriter.write` fails | Bubble up; suite exits 1. If we cannot persist the result, the gate has no record and downstream checks would silently let the engine graduate. |
| Healthchecks ping fails (network) | Log a `validation.healthcheck.ping_failed` event; continue. The exit code already encodes pass/fail. |

### Implicit prerequisite

`tpcore.quality.data_quality.DataQualityWriter.write` is currently a stub (`tpcore/quality/data_quality.py:34` raises `NotImplementedError`). Implementing it — a small INSERT against `platform.data_quality_log` — is part of this work, not a separate task. Bundling it here avoids a dependency loop where the suite is built but cannot persist results.

---

## 7. Fixtures

### `fixtures/delistings.yaml`

```yaml
- ticker: BBBYQ
  alt_tickers: [BBBY]
  delisting_date: 2023-04-23
  reason: bankruptcy
  notes: "Bed Bath & Beyond Chapter 11"
- ticker: SIVBQ
  alt_tickers: [SIVB]
  delisting_date: 2023-03-17
  reason: bankruptcy
  notes: "SVB Financial Group"
# ... 10–15 entries total
```

**Inclusion criteria** (locked):
1. Within Alpaca free-tier coverage (≥ 2018 in practice).
2. Reachable via Alpaca's `status=inactive` asset listing — public delistings only.
3. Mix of causes: bankruptcies, acquisitions, mergers, liquidations.
4. At least one from each of: 2018–2020, 2021–2023, 2024+ (catches "ingestion silently stopped pulling inactive symbols after date X").

The actual ticker selection is implementation detail.

### `fixtures/splits.yaml`

```yaml
- ticker: AAPL
  split_date: 2020-08-31
  ratio: "4:1"
- ticker: TSLA
  split_date: 2022-08-25
  ratio: "3:1"
- ticker: NVDA
  split_date: 2024-06-10
  ratio: "10:1"
# ... 10–15 forward-split entries; ≥ 1 per year where possible
```

Reverse splits excluded from MVP.

### `fixtures/constituents.yaml`

```yaml
current_sp500_snapshot_date: 2026-05-10
current_sp500:
  - AAPL
  - MSFT
  # ... ~500 tickers, vendored once from a public source
recent_removals:
  - ticker: SIVBQ
    removed_date: 2023-03-15
    reason: bankruptcy
    expect_delisted: true
  - ticker: FRC
    removed_date: 2023-05-01
    reason: acquired
    expect_delisted: true
  # ... ~20 entries
```

Refreshed manually by the operator. The master plan commits only to "snapshot + recent removals" for MVP; an out-of-date snapshot is acceptable.

---

## 8. Testing

Per `STYLE_GUIDE.md`: pytest + pytest-asyncio (auto), deterministic fixtures, no live API.

### Mocking strategy

Two options were considered:
1. Real Postgres in CI via `pytest-postgresql` or testcontainers — high fidelity, slow.
2. In-memory fake pool wrapping `dict[(ticker, date), row]`, stubbing the asyncpg `fetch`/`fetchrow` methods used by the checks.

**Decision: option 2 for unit tests** — the queries are simple `SELECT`s with no JSONB ops or Postgres-specific operators. Optionally one SQLite-backed integration test if the actual SQL strings need exercising; not required for MVP.

### Per-check test matrix

| Check | Happy path | Failure modes covered |
|---|---|---|
| delistings | All fixture tickers (or alts) present, `delisted=true`, dates within ±5d | (a) ticker missing, (b) `delisted=false`, (c) `delisting_date` NULL, (d) date drift > 5 trading days, (e) bars stop > 5 days before recorded delisting, (f) only an `alt_ticker` present — primary missing — should pass |
| constituent | All current S&P names present + recent_removals correctly delisted | (a) current S&P ticker missing, (b) current S&P present but stale (no recent bar), (c) recent removal not marked delisted |
| splits | All fixture splits show post-adjust ratio in [0.99, 1.01] | (a) ratio in [0.20, 0.30] (4:1 unadjusted), (b) ratio in [0.05, 0.15] (10:1 unadjusted), (c) ticker missing on `split_date - 1` |

### Capital Gate tests

- Most recent run within 7 days, all passed → `assert_passed` returns silently.
- Most recent run within 7 days, one failed → `ValidationFailedError`, message names the failing source.
- Most recent run > 7 days old → `ValidationStaleError`.
- No runs ever → `ValidationStaleError`.

### End-to-end test (`test_suite_e2e.py`)

- Synthetic `prices_daily` populated to satisfy the fixtures.
- Run `run_suite(pool)`, assert `passed = True`, assert 3 rows written to `data_quality_log` with the expected `source` values.
- Mutate one synthetic bar to break a split, re-run, assert `passed = False` with the right `source` flagged in `data_quality_log`.

### Out of scope for tests

- No tests against live Alpaca / FMP / EDGAR.
- No tests for Healthchecks ping integration.
- No tests for the cron entry point itself (too thin to be worth mocking the env).

---

## 9. Operations

### Railway cron service

A new `validation-scheduler` service in `railway.json`, peer to `sigma-scheduler` and `reversion-scheduler`:

```json
"validation-scheduler": {
    "startCommand": "python ops/cron_validation.py",
    "cronSchedule": "0 6 * * SUN",
    "restartPolicyType": "NEVER",
    "source": {"repo": "michaeleasterly-cpu/short-term-trading-engine"}
}
```

After editing `railway.json`, the operator runs `python ops/apply_railway_service_config.py --all` (per existing pattern in `ops/`).

### Healthchecks

A new Healthchecks check, `validation-suite`, with the URL stored in `HEALTHCHECKS_VALIDATION_URL`. Mirrors the per-engine pattern.

### Forward look

`SplitsSource`, `DelistingsSource`, and `ConstituentSource` ABCs are the interface seam. Future EDGAR-backed implementations slot in without disturbing checks, suite, or capital gate. EDGAR work is a separate spec.
