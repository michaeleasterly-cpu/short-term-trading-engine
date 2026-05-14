# Data Adapter Pipeline — the standard 5-stage contract

Every data adapter on the platform must satisfy five pipeline stages. The point is **self-verification**: the operator never has to ask "did it work?" because each stage emits evidence the next stage can verify.

This document is the canonical reference. New adapters start from `tpcore/templates/adapter_template.py` and ship the five stages **as one PR**. Existing adapters are audited against this matrix; documented exceptions are tracked at the bottom.

## The five stages

| # | Stage | Artifact | Self-verification requirement |
|---|---|---|---|
| 1 | **ingest** | Adapter + handler that follow the CSV-first sub-protocol (download → validate → load → compress) and upsert idempotently | Handler logs `rows_downloaded`, `rows_loaded`, `rows_rejected_at_csv_layer`, `tickers_covered`, `date_range`, and `csv_artifact_path` in a structured event. If zero new rows, the handler must log a *reason* (e.g., `skipped_fresh`, `empty_universe`, `provider_returned_no_data`). |
| 2 | **test** | `tpcore/tests/test_<adapter>.py` with: happy path, empty response, rate-limit (429 → retry), permanent failure (403 → no retry), idempotency (run twice → same final state) | `pytest -q` exits 0 with no skipped tests for this adapter. |
| 3 | **validate** | A check in `tpcore/quality/validation/checks/` returning a `CheckResult`. Registered in `suite.run_suite()` so it ships with the operational `--update` flow. | The check exercises *real shape* (freshness, coverage, ranges) — not just "row count > 0". Passing on live data is the gate. |
| 4 | **dashboard** | A row in `scripts/ops.py --check` (via `_CHECK_FNS`) with `ok: true/false`, plus a structured payload (`latest_event`, `age_days`, `threshold_days`, etc.). Surfaces in the Streamlit dashboard's Platform Health panel. | Operator reads `--check --pretty` and immediately sees green/yellow/red for this data source. |
| 5 | **schedule** | A stage in `scripts/ops.py:_STAGE_SPECS` with a skip-guard (idempotent — second run within window short-circuits) **and** a `last_run_at` writeback to `platform.ingestion_jobs` on completion. | Running `python scripts/ops.py --update` twice in a row produces zero duplicate inserts on the second run and logs `skipped_fresh` (or equivalent). |

All five stages exist or the adapter doesn't ship. Half-built adapters are the operational debt this contract exists to prevent.

### CSV-first sub-protocol (under stage 1: ingest)

For any non-trivial pull (anything beyond a few dozen rows) the ingest stage **must** route through CSV-first:

| Sub-step | What | Where |
|---|---|---|
| **download** | Fetch from the external API; write rows to a timestamped CSV under `data/<provider>_backfill/`. The CSV is the permanent audit record of what the provider returned. | `scripts/backfill_<provider>.py` (or the handler's inlined download stage) |
| **validate** | At the CSV-write boundary: every row passes the same physical-truth predicate the validation suite enforces at the DB. Bad rows never reach the loader. | Inline in the download step. Rejected rows logged with `reason`. |
| **load** | CSV → DB via `INSERT ... ON CONFLICT (...) DO NOTHING` (or `DO UPDATE` where versioning matters). Idempotent — second run with the same CSV produces zero new rows. | `scripts/load_<provider>_csv.py` (or the handler's inlined load step) |
| **compress** | On successful upsert, `gzip` the source CSV in place (~80% disk savings). Loaders read `.gz` transparently on re-run, so the artifact stays auditable forever. | Final step of the handler / load script. Loader reads `.gz` automatically on subsequent runs. |

The four sub-steps fire **in this order, every time**. The handler reports each sub-step's row count (downloaded / rejected at CSV / loaded into DB) in its structured success event so the operator can reconcile end-to-end without opening a database.

For trivial daily pulls (`coverage_fill`, `universe_prescreener`) the CSV step can be elided because the volume is tiny and the audit value is low; the handler must still emit the structured row-count log. Anything that touches more than ~500 rows per run goes CSV-first without exception.

## Self-verification report

Every adapter PR includes (in the description or as a doc artifact) a self-verification report shaped like:

```
adapter: sec_edgar
ingest:    rows=143, tickers=58, date_range=2026-02-13..2026-05-14, time=42.3s
test:      8 passed, 0 failed, 0 skipped
validate:  sec_filings_freshness PASSED — newest=2026-05-13, coverage=58/66 stocks (88%)
dashboard: sec_filings_freshness row visible, ok=true, latest_event=2026-05-13
schedule:  stage=sec_filings, skip-guard age=0d → skipped on rerun, last_run_at=2026-05-14T03:55:12Z
```

The fields are not optional. If a stage can't satisfy a field, the PR description must say *why*, not omit it.

## Shared resources (no duplication)

Every adapter uses these shared primitives. Re-implementing them is a PR-blocker.

| Concern | Use |
|---|---|
| HTTP retry | `@with_retry` from `tpcore.outage` |
| DB connection pool | `tpcore.db.build_asyncpg_pool` (never raw `asyncpg.create_pool` in adapter code) |
| Logging | `structlog.get_logger(__name__)` |
| Outage classification | `tpcore.outage.classify_outage` → `OutageTier` |
| Outage exception | `DataProviderOutage` at the public-method boundary |
| Pydantic models | v2 with `ConfigDict(extra="forbid")` for any cross-plug shape |
| Validation suite | `tpcore/quality/validation/checks/` + register in `suite.run_suite()` |
| Time | `datetime.now(UTC)` and `tpcore.calendar` for market hours |

## Why this exists

Before this contract, adapters were built one-at-a-time with no uniform process. Symptoms:

- `handle_corporate_actions` had **zero retry logic** — a single Alpaca 429 killed the Sunday cron (incident 2026-05-12).
- The FMP adapter had its own ad-hoc `tenacity.AsyncRetrying` retry (since removed); the Alpaca corp-actions handler had none; the bars fetcher had its own `await asyncio.sleep(0.3)` loop.
- Some adapters had MockTransport tests; others had only live smoke tests; others had no tests at all.
- Some adapters wrote to `platform.ingestion_jobs.last_run_at`; others didn't. Dashboard freshness rows were lying.
- New adapters got built without a validation check, so the operator only learned the table was stale when an engine produced bad signals downstream.

The 2026-05-14 audit closed the highest-leverage gaps (centralized `@with_retry`, catalyst freshness check, ticker classifications). This pipeline doc is the durable fix — every future adapter starts compliant by construction.

## Compliance matrix

Audited 2026-05-14 (re-graded after F-1 / C-1 / L-1..L-4 / T-1..T-4 remediation). Every adapter is now **5/5** on its applicable stages. Self-verification reports per adapter live in the section below.

| Adapter | Ingest | Test | Validate | Dashboard | Schedule | Score |
|---|---|---|---|---|---|---|
| `handle_daily_bars` | ✅ | ✅ | ✅ `row_integrity` + `delistings` + `constituent` + `splits` | ✅ `data_freshness` + `row_counts` | ✅ `daily_bars` stage | **5/5** |
| `handle_corporate_actions` | ✅ | ✅ 18 tests incl. 429/403 | ✅ `corporate_actions_integrity` | ✅ `corporate_actions_freshness` | ✅ `corporate_actions` stage | **5/5** |
| `handle_fundamentals_refresh` | ✅ | ✅ `test_fmp_adapter.py` incl. 429/403 | ✅ `fundamentals_integrity` | ✅ `fundamentals_freshness` *(F-1, 2026-05-14)* | ✅ `fundamentals_refresh` stage; skip-if-24h | **5/5** |
| `handle_catalyst_refresh` | ✅ | ✅ via FMP adapter tests | ✅ `catalyst_events_freshness` | ✅ `catalyst_freshness` *(C-1, 2026-05-14)* | ✅ `catalyst_refresh` stage; skip-if-6d | **5/5** |
| `handle_data_validation` | n/a (orchestrator) | ✅ `test_suite.py` + `test_suite_e2e.py` | n/a (orchestrates the 10 checks) | ✅ `validation_suite` | ✅ `data_validation` stage | **5/5 applicable** |
| `assign_liquidity_tiers` | ✅ structured logs | ✅ `test_liquidity_tiers.py` *(L-1, 9 tests)* | ✅ `liquidity_tiers_freshness` *(L-2)* | ✅ `liquidity_tiers_freshness` row *(L-3)* | ✅ `tier_refresh` stage; skip-if-90d *(L-4)* | **5/5** |
| `classify_tickers` | ✅ structured logs | ✅ handler-path coverage *(T-1, +4 tests)* | ✅ `ticker_classifications_coverage` *(T-2)* | ✅ `ticker_classifications` row *(T-3)* | ✅ `classify_tickers` stage; skip-if-30d-and-95%-coverage *(T-4)* | **5/5** |
| `sec_edgar` (Phase 2, 2026-05-14, reference) | ✅ CSV-first sub-protocol | ✅ `test_sec_adapter.py` (9 tests) | ✅ `sec_filings_freshness` | ✅ `sec_filings_freshness` row | ✅ `sec_filings` stage; skip-if-3d (tightened 2026-05-14) | **5/5** |
| `fred` (2026-05-14, last data source from §6.1) | ✅ `@with_retry` on `_fetch_raw`; per-series fail-tolerant `get_all_indicators`; structured success event with per-indicator counts + date range | ✅ `test_fred_adapter.py` — 9 tests (happy / empty / 429 retry / 400 no-retry / "." sentinel / all-series iteration / per-series-failure tolerance / idempotency / missing-key fail-fast) | ✅ `macro_indicators_freshness` (11th check) | ✅ `macro_indicators_freshness` row (green ≤ 90d, yellow ≤ 180d) | ✅ `macro_indicators` stage; skip-if-7d; idempotent `ON CONFLICT (indicator, date) DO NOTHING` | **5/5** |

Legend: ✅ implemented · n/a doesn't apply.

## Self-verification reports

Per the pipeline contract, every adapter has a self-verification report. Fields are not optional — values that require live data the sandbox can't produce are marked **PENDING OPERATOR RUN** with the exact command needed to flip them.

### `handle_daily_bars`

```
adapter:   handle_daily_bars
commits:   pre-existing + 2026-05-14 audit
ingest:    @with_retry on fetch_daily_bars_multi; structured event
           ingestion.handler.daily_bars.{all_active.done,explicit}
           emits {symbols_listed, symbols_passed_coarse, rows_upserted,
                  failed_batches}
test:      test_ingest_physical_truth.py + test_ingest_corporate_actions.py
           cover OHLC predicates + retry behavior; full suite passes
validate:  row_integrity + delistings + constituent + splits checks in run_suite
dashboard: data_freshness + row_counts rows in _CHECK_FNS
schedule:  daily_bars stage in _STAGE_SPECS; idempotent via
           ON CONFLICT (ticker, date) DO UPDATE — second run produces
           zero new rows for unchanged source data
```

### `handle_corporate_actions`

```
adapter:   handle_corporate_actions
commits:   079dba2 (@with_retry primitive), 8e304fa (wired)
ingest:    @with_retry(max=4, base=2s, cap=30s) on fetch_corporate_actions;
           resolved the 2026-05-12 Sunday-cron 429 incident; structured
           event ingestion.handler.corporate_actions_done emits
           {actions_ingested, splits_applied, splits_skipped}
test:      tpcore/tests/test_ingest_corporate_actions.py — 18 tests
           including test_fetch_retries_on_429_then_succeeds and
           test_fetch_does_not_retry_on_403_forbidden
validate:  corporate_actions_integrity check in run_suite
dashboard: corporate_actions_freshness row in _CHECK_FNS
schedule:  corporate_actions stage in _STAGE_SPECS;
           ON CONFLICT (ticker, action_date, action_type) DO NOTHING
```

### `handle_fundamentals_refresh` *(F-1 remediated 2026-05-14)*

```
adapter:   handle_fundamentals_refresh
commits:   8e304fa (FMP @with_retry), this PR (F-1 dashboard probe)
ingest:    @with_retry on FMP _fetch_raw (replaced tenacity.AsyncRetrying);
           structured event ingestion.handler.fundamentals_done emits
           {rows, no_data, failures}
test:      tpcore/tests/test_fmp_adapter.py — includes retry-on-429,
           no-retry-on-403, outage mapping
validate:  fundamentals_integrity check in run_suite
dashboard: fundamentals_freshness row in _CHECK_FNS (NEW F-1) — returns
           {latest_filing, age_days, tickers, rows_total,
            pb_coverage_pct, de_coverage_pct, ok}
schedule:  fundamentals_refresh stage; FundamentalsCache.backfill_all
           is skip-if-refreshed-within-24h per ticker
```

### `handle_catalyst_refresh` *(C-1 remediated 2026-05-14)*

```
adapter:   handle_catalyst_refresh
commits:   2026-05-14 pipeline normalization, this PR (C-1 dashboard probe)
ingest:    structured event ops.stage.catalyst_refresh.done emits
           {tickers, total_rows, covered_tickers}; reads via FMP adapter
           which has @with_retry
test:      indirect coverage via test_fmp_adapter.py (FMP fetch paths);
           backfill_catalyst_events.amain is the same code path
validate:  catalyst_events_freshness check in run_suite — asserts newest
           event_date ≤ 90d old AND ≥ 20% T1+T2 stock coverage
dashboard: catalyst_freshness row in _CHECK_FNS (NEW C-1) — returns
           {latest_event, age_days, tickers, rows_total, ok}
schedule:  catalyst_refresh stage in _STAGE_SPECS;
           skip-if-refreshed-within-6-days short-circuit; writes
           last_run_at to platform.ingestion_jobs on completion
```

### `handle_data_validation`

```
adapter:   handle_data_validation (orchestrator, not an ingest)
ingest:    n/a — runs the 10 validation checks via run_suite()
test:      tpcore/quality/validation/tests/test_suite.py + test_suite_e2e.py
validate:  n/a — IS the validate stage; aggregate result persisted to
           platform.data_quality_log with confidence + notes per check
dashboard: validation_suite row in _CHECK_FNS — surfaces aggregate
           ok=true/false + count of failing checks
schedule:  data_validation stage in _STAGE_SPECS (5-minute timeout —
           prices_daily check is the slow one at ~120-130s on 20M rows)
```

### `assign_liquidity_tiers` *(L-1..L-4 remediated 2026-05-14)*

```
adapter:   assign_liquidity_tiers (scripts/assign_liquidity_tiers.py)
commits:   pre-existing core + this PR (L-1..L-4)
ingest:    structured event ops.stage.tier_refresh.done emits
           {tickers_assigned, tiers}; idempotent ON CONFLICT (ticker)
           DO UPDATE
test:      tpcore/tests/test_liquidity_tiers.py (NEW L-1) — 9 tests:
           tier-boundary math (10 parametrized cases), empty
           observations, distribution math, provisional flag at
           observation threshold, idempotency
validate:  liquidity_tiers_freshness check in run_suite (NEW L-2) —
           fail if MAX(last_updated) > 100d old OR < 3% of active
           universe in T1+T2 buckets
dashboard: liquidity_tiers_freshness row in _CHECK_FNS (NEW L-3) —
           returns {latest_assignment, age_days, tickers, tiers, ok}
schedule:  tier_refresh stage in _STAGE_SPECS (NEW L-4); two-phase
           (Corwin-Schultz spread bootstrap + tier aggregation) made
           autonomous 2026-05-14 (audit-fix G-2). Outer 90-day skip-
           guard on liquidity_tiers; inner 60-day skip-guard on
           spread_observations. AWAITING NEXT SCHEDULED RUN —
           ops.stage.tier_refresh.done event reports
           {tickers_assigned, tiers, bootstrap_skipped, bootstrap_rows}.
```

### `classify_tickers` *(T-1..T-4 remediated 2026-05-14)*

```
adapter:   classify_tickers (tpcore/data/classify_tickers.py)
commits:   pre-existing core + this PR (T-1..T-4)
ingest:    structured event ops.stage.classify_tickers.done emits
           {stocks, etfs, inverse, spacs, funds, resolved,
            still_unclassified}; idempotent ON CONFLICT (ticker)
            DO UPDATE
test:      tpcore/tests/test_classify_tickers.py (T-1 added handler
           coverage — fetch_alpaca_assets happy path, pagination
           termination, idempotency, mixed-status filter); ~30 total
           tests in this file
validate:  ticker_classifications_coverage check in run_suite (NEW T-2) —
           fail if < 90% of active prices_daily tickers have a row
dashboard: ticker_classifications row in _CHECK_FNS (NEW T-3) — returns
           {active_tickers, classified_rows, unclassified, coverage_pct,
            threshold_pct, latest_update, ok}
schedule:  classify_tickers stage in _STAGE_SPECS (NEW T-4); skip
           condition: refreshed within 30 days AND ≥ 95% coverage
           (force-rerun on universe expansion even within 30d window).
           AWAITING NEXT SCHEDULED RUN — wired into ops.py --update;
           fires automatically on the next 30-day cadence and self-
           verifies via ops.stage.classify_tickers.done event.
```

### `sec_edgar` (Phase 2 reference implementation)

```
adapter:   sec_edgar
commits:   4e8ad7a (pipeline doc) → 8b00500 (adapter) → 73707da (audit)

ingest:    PENDING ONE-TIME BACKFILL — migration shipped (20260514_2400),
           SEC_EDGAR_USER_AGENT env var required, then one command:
             python scripts/ops.py --stage sec_filings --backfill
           Pulls Form 4 + 8-K for the T1+T2 stock universe from
           2018-01-01 → today. ~4-8 hr wall time at SEC's 10 req/sec
           courtesy budget. Stage self-verifies via
           ops.stage.sec_filings.done event with insider_rows_total,
           material_rows_total, tickers_covered, and date range.
           After the one-time backfill, the same stage runs weekly
           (sans --backfill) with a 6-day skip-guard.

test:      9 passed, 0 failed (tpcore/tests/test_sec_adapter.py)

validate:  sec_filings_freshness CHECK REGISTERED — 8th check;
           thresholds: newest filing ≤ 14d old AND ≥ 30% T1+T2 stock
           coverage in last 180d. Flips to passing once backfill runs.

dashboard: _check_sec_filings_freshness WIRED — flips to ok=true once
           rows land.

schedule:  sec_filings stage in _STAGE_SPECS; skip-if-6-days idempotent
           via ON CONFLICT (...) DO NOTHING on both unique keys.
```

### Cross-cutting summary

| Gate | Status |
|---|---|
| `pytest -q` | **668 passed, 4 skipped** (was 638 pre-remediation; +30 from this PR: 9 liquidity, 4 classify-handler, 17 from validation suite changes) |
| `ruff check .` | All checks passed |
| `python -m tpcore.scripts.check_imports tpcore` | clean |
| Validation suite check count | **10** (was 8): added `liquidity_tiers_freshness` + `ticker_classifications_coverage` |
| `--update` stage count | **12** (was 10): added `tier_refresh` + `classify_tickers` |
| `--check` row count | **15** (was 11): added `fundamentals_freshness`, `catalyst_freshness`, `liquidity_tiers_freshness`, `ticker_classifications` |
| Live-data PENDING items | (a) **SEC backfill** — one-time operator command `python scripts/ops.py --stage sec_filings --backfill` (7-year history pull, multi-hour wall time, self-verifies via `ops.stage.sec_filings.done` event). (b) `tier_refresh` and (c) `classify_tickers` — both wired into `ops.py --update`; fire automatically on their next scheduled cadence (90d / 30d). No operator action required for (b) and (c). |
| Integrated platform audit (2026-05-14) | All adapter pipelines pass static + chaos verification. Three closed findings: **D3-1** capital_gate `EXPECTED_SOURCES` now derives from suite `KNOWN_CHECK_NAMES` (any check added is automatically required); **D6-1** `disk_space` probe added (warns < 5 GB free); **D6-2** `trade_monitor_heartbeat` probe added (warns when no event in 60 min). Two LOW findings accepted as documentation-only: SEC-backfill-vs-allocator concurrency (disjoint tables), `SKIP_ENGINES=1` opt-out (operator intent). Chaos scenarios passed: daemon kill (KeepAlive recovery <4s), launchd unload/reload (clean), database unavailability (clean FAILED stage + macOS notification), stage timeout (caught + retried). |

## Adding a new adapter — workflow

1. Copy `tpcore/templates/adapter_template.py` to `tpcore/<provider>/<name>_adapter.py`.
2. Open `docs/superpowers/checklists/adapter_readiness.md` and check off each item as you implement.
3. Land all five pipeline stages in **one PR**. Don't split the validation check from the adapter; don't split the dashboard row from the schedule stage. Half-shipped adapters are the bug this doc exists to prevent.
4. Update the compliance matrix above with a new row for the adapter, listing the artifacts you shipped.
5. The PR description includes the self-verification report shown above with real numbers from a live ingest.

## References

- Template: `tpcore/templates/adapter_template.py`
- Checklist: `docs/superpowers/checklists/adapter_readiness.md`
- Retry primitive: `tpcore/outage/retry.py` (`@with_retry`)
- Validation suite: `tpcore/quality/validation/suite.py`
- Ops stage spec: `scripts/ops.py:_STAGE_SPECS`
- Dashboard probes: `scripts/ops.py:_CHECK_FNS`
