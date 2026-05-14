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

Audited 2026-05-14 against the live codebase. Each row: which stages are wired and which are exceptions. Updated as part of every adapter PR.

| Adapter | Ingest | Test | Validate | Dashboard | Schedule | Notes |
|---|---|---|---|---|---|---|
| `handle_daily_bars` | ✅ `@with_retry` on `fetch_daily_bars_multi`; structured success log with `rows_upserted`, `failed_batches`, `symbols_passed_coarse` | ✅ `test_ingest_physical_truth.py` covers OHLC + delistings paths | ✅ `row_integrity`, `delistings`, `constituent`, `splits` | ✅ `data_freshness`, `row_counts` | ✅ `daily_bars` stage; idempotent — upsert on `(ticker, date)` PK | Reference-grade. The `all_active` discovery sweep batches 50 symbols at 0.3s sleep — well-budgeted under Alpaca's free-tier rate cap. |
| `handle_corporate_actions` | ✅ `@with_retry` on `fetch_corporate_actions` (fixed the 2026-05-12 Sunday-cron 429); structured success log with `actions_ingested`, `splits_applied`, `splits_skipped` | ✅ `test_ingest_corporate_actions.py` — 18 tests including retry-on-429 + no-retry-on-403 | ✅ `corporate_actions_integrity` | ✅ `corporate_actions_freshness` (dedicated `_check_corp_actions_freshness`) | ✅ `corporate_actions` stage; idempotent `ON CONFLICT DO NOTHING` | Reference-grade. |
| `handle_fundamentals_refresh` | ✅ `@with_retry` on FMP `_fetch_raw` (replaced `tenacity.AsyncRetrying`); structured success log with `rows`, `no_data`, `failures` | ✅ `test_fmp_adapter.py` — includes retry-on-429 + no-retry-on-403 | ✅ `fundamentals_integrity` | ⚠️ generic `row_counts`; no dedicated `_check_fundamentals_freshness` probe | ✅ `fundamentals_refresh` stage; skip-if-refreshed-within-24h | **Follow-up F-1:** dedicated freshness probe so the dashboard surfaces stale fundamentals without operator drilling into the validation-suite output. |
| `handle_catalyst_refresh` | ✅ structured success log; reads FMP earnings-history (uses FMP adapter's `@with_retry`) | ⚠️ no direct handler unit test — `backfill_catalyst_events.amain` covered indirectly via `test_fmp_adapter.py` paths; no dedicated coverage of the skip-guard branch | ✅ `catalyst_events_freshness` (registered in `run_suite`) | ⚠️ covered by `validation_suite` aggregate row; no dedicated `_check_catalyst_freshness` probe | ✅ `catalyst_refresh` stage; skip-if-refreshed-within-6-days; writes `last_run_at` to `ingestion_jobs` | **Follow-up C-1:** add `tpcore/tests/test_handle_catalyst_refresh.py` exercising the skip-guard. **C-2:** add a dedicated `_check_catalyst_freshness` row in ops dashboard. |
| `handle_data_validation` | n/a (orchestrator, not an ingest) | ✅ `test_suite.py` + `test_suite_e2e.py` (FakePool-backed) | n/a (orchestrates the 8 checks) | ✅ `validation_suite` | ✅ `data_validation` stage | Exception is structural — this adapter's "ingest" is running the other adapters' validate checks. |
| `assign_liquidity_tiers` | ✅ writes `platform.liquidity_tiers` from `spread_observations`; structured logs | ❌ no `test_assign_liquidity_tiers.py` | ❌ no `liquidity_tiers_freshness` check | ❌ no dedicated `_check_liquidity_tiers_freshness` row | ⚠️ manual via `scripts/run_tier_refresh.sh`; no `ops.py` stage | **Follow-ups L-1..L-4:** test, validate, dashboard, schedule. Quarterly cadence is fine — but needs an explicit `liquidity_tiers_freshness` check (e.g., warn if `MAX(last_updated) > 100 days ago`) so the dashboard catches operator inaction. Cross-table audit (`scripts/run_audit_all_tables.sh`) already catches stale-30d as a side effect — that's the interim guard. |
| `classify_tickers` | ✅ writes `platform.ticker_classifications` from Alpaca `/v2/assets` + name-pattern classifier; structured logs | ⚠️ `test_classify_tickers.py` covers the *classifier logic* (Apple→stock, iShares→ETF, inverse detection) but not the full handler / Alpaca-fetch path | ❌ no `ticker_classifications_freshness` check | ❌ no dedicated row | ⚠️ manual via `python scripts/classify_tickers.py`; no `ops.py` stage | **Follow-ups T-1..T-4:** test handler path, validate, dashboard, schedule. Asset-class taxonomy is near-static — accept manual-on-universe-expansion as the interim cadence, but add an explicit dashboard row warning if `(SELECT COUNT(*) FROM platform.prices_daily WHERE ticker NOT IN (SELECT ticker FROM platform.ticker_classifications)) > 100`. |
| `sec_edgar` (Phase 2, 2026-05-14, **reference implementation**) | ✅ `@with_retry` on both `_fetch_raw` paths; CSV-first sub-protocol (download → validate-at-CSV → load → compress); structured success log with `rows_downloaded`, `rows_rejected_at_csv_layer`, `rows_loaded`, `insider_loaded`, `material_loaded`, `tickers_with_filings`, `date_range`, `csv_artifact` paths | ✅ `test_sec_adapter.py` — 9 tests (happy path / empty / 429 retry / 403 no-retry / malformed XML / BUY-SELL extraction / idempotency / 8-K item parsing / missing-UA fail-fast) | ✅ `sec_filings_freshness` (8th check in `run_suite`) | ✅ `_check_sec_filings_freshness` (dedicated row in `_CHECK_FNS`) | ✅ `sec_filings` stage in `_STAGE_SPECS`; skip-if-refreshed-within-6-days; idempotent `ON CONFLICT DO NOTHING` on both unique keys | none — reference for all future adapters |

Legend: ✅ implemented · ⚠️ partial · ❌ missing · n/a doesn't apply.

### Outstanding follow-ups

Tracked here as the post-audit punch list. Each item gates that adapter's row from going fully ✅ in a future audit pass.

| ID | Adapter | Gap | Acceptance |
|---|---|---|---|
| F-1 | `handle_fundamentals_refresh` | dedicated dashboard freshness probe | `_check_fundamentals_freshness` returns `ok=true` when `MAX(filing_date) > today - 95d` (one quarter + grace); appears as a top-level row in `--check` output |
| C-1 | `handle_catalyst_refresh` | direct handler test (skip-guard branch) | `tpcore/tests/test_handle_catalyst_refresh.py` exercises skipped_fresh vs forced-refresh paths via a fake pool |
| C-2 | `handle_catalyst_refresh` | dedicated dashboard probe | `_check_catalyst_freshness` row in `_CHECK_FNS`; mirrors the validation-check thresholds |
| L-1 | `assign_liquidity_tiers` | test file | covers tier-boundary math + stale-tier detection |
| L-2 | `assign_liquidity_tiers` | validation check | `liquidity_tiers_freshness` — fail when `MAX(last_updated) < today - 100d` |
| L-3 | `assign_liquidity_tiers` | dashboard row | `_check_liquidity_tiers_freshness` |
| L-4 | `assign_liquidity_tiers` | ops.py stage (quarterly cadence) | `tier_refresh` stage with skip-if-refreshed-within-90d guard |
| T-1 | `classify_tickers` | handler-path test | covers Alpaca-fetch happy path + name-pattern fallback for unseen tickers |
| T-2 | `classify_tickers` | validation check | `ticker_classifications_coverage` — fail when > 100 prices_daily tickers lack a classification |
| T-3 | `classify_tickers` | dashboard row | `_check_ticker_classifications_coverage` |
| T-4 | `classify_tickers` | ops.py stage (universe-expansion trigger) | `classify_tickers_refresh` stage; idempotent; skip-if-no-new-tickers guard |

None of these gaps block any current engine — the existing cross-table audit (`scripts/run_audit_all_tables.sh`) catches the worst symptoms (stale tiers, orphan tickers) as defense in depth. The follow-ups close the gaps proactively so the dashboard surfaces problems before they cascade.

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
