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

Updated as part of every adapter PR. Each row: which stages are wired and which are exceptions.

| Adapter | Ingest | Test | Validate | Dashboard | Schedule | Exceptions |
|---|---|---|---|---|---|---|
| `handle_daily_bars` | ✅ | ✅ | ✅ (`row_integrity`, `delistings`, `constituent`, `splits`) | ✅ (`data_freshness`, `row_counts`) | ✅ (`daily_bars` stage; idempotent — upsert on PK) | none |
| `handle_corporate_actions` | ✅ | ✅ | ✅ (`corporate_actions_integrity`) | ✅ (`corporate_actions_freshness`) | ✅ (`corporate_actions` stage; idempotent — `ON CONFLICT DO NOTHING`) | none |
| `handle_fundamentals_refresh` | ✅ | ✅ | ✅ (`fundamentals_integrity`) | ✅ (`row_counts`) | ✅ (`fundamentals_refresh` stage; skip-if-refreshed-within-24h) | Dashboard row is generic `row_counts` not a per-source freshness probe — follow-up. |
| `handle_catalyst_refresh` | ✅ | ✅ | ✅ (`catalyst_events_freshness`) | ⚠️ (covered by `validation_suite` row, no dedicated probe) | ✅ (`catalyst_refresh` stage; skip-if-refreshed-within-6-days) | Dedicated dashboard probe (`catalyst_freshness`) is a follow-up. |
| `handle_data_validation` | n/a (it *is* the validate stage) | ✅ | n/a | ✅ (`validation_suite`) | ✅ (`data_validation` stage) | "Ingest" doesn't apply — orchestrates other validation checks. |
| `assign_liquidity_tiers` | ✅ (writes `platform.liquidity_tiers`) | ⚠️ (limited test coverage) | ❌ (no `liquidity_tiers_freshness` check) | ❌ (no dedicated row) | ⚠️ (manual via `scripts/run_tier_refresh.sh`; no `ops.py` stage) | Dedicated validate + dashboard + scheduled stage are open follow-ups. Tier assignments drift slowly so manual-quarterly is acceptable interim; needs explicit cadence guard. |
| `classify_tickers` | ✅ (writes `platform.ticker_classifications`) | ⚠️ (limited test coverage) | ❌ (no `ticker_classifications_freshness` check) | ❌ (no dedicated row) | ⚠️ (manual via `python scripts/classify_tickers.py`; no `ops.py` stage) | Near-static taxonomy; manual-on-universe-expansion accepted. Validate + dashboard rows are open follow-ups. |
| `sec_edgar` (Phase 2, 2026-05-14) | ✅ | ✅ | ✅ (`sec_filings_freshness`) | ✅ (`sec_filings_freshness` row) | ✅ (`sec_filings` stage; skip-if-refreshed-within-6-days) | none — reference implementation |

Legend: ✅ implemented · ⚠️ partial · ❌ missing · n/a doesn't apply.

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
