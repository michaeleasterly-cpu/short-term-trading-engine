# `excluded_confirmed_data_gap` validator-semantics — implementation plan

**Status:** PLAN ONLY. No implementation. No DB writes. No live SEC
fallback. Direct successor to spec
`docs/superpowers/specs/2026-06-02-excluded-confirmed-data-gap-validator-semantics.md`
(PR #450, merged at `2be9dec`).

Drafted: 2026-06-02. Owner: operator. Author: validator-semantics /
data-quality track.

## 1. Verdict

The implementation is **one coherent heavy-lane PR**:

1. Alembic migration creating `platform.fundamentals_period_source_evidence`.
2. `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
   wired to read the evidence substrate and route dual-source-evidenced
   periods to `excluded_confirmed_data_gap`.
3. New `scripts/ops.py` stage `confirmed_data_gap_evidence_populator`
   that runs FMP cascade + SEC fallback against currently-FAILing
   periods and writes evidence rows. `dry_run=true` default; **dry-run
   never writes**.
4. FMP cascade (`historical_fundamentals_quarterly`) + SEC fallback
   (`sec_fundamentals_fallback`) writes are extended so that their
   normal-mode operation ALSO writes evidence rows. This is the daily
   substrate.
5. Hermetic tests for migration, populator stage, validator wiring,
   and the FMP/SEC evidence-write extensions.

Spec §15 open operator decisions are now RESOLVED (§2 below). No
behavioral surprise from the spec; this plan is implementation
sequencing + interface details.

## 2. Resolved operator decisions (from spec §15)

| Decision | Operator resolution | Implementation impact |
|---|---|---|
| **Freshness window** | **180 days** | Constant `CONFIRMED_DATA_GAP_FRESHNESS_DAYS = 180`. Validator's evidence-join SQL filters `last_attempted_at >= NOW() - INTERVAL '180 days'` |
| **Dry-run population** | **Dry-run MUST NOT write** | `confirmed_data_gap_evidence_populator` honors `dry_run=true` (default) → preview-only; counters reported; zero rows inserted |
| **Inference clamp** | **SPLIT to separate arc** unless evidence forces bundling | This PR does NOT modify `_infer_missing_period_ends`. If the implementation's test fixtures surface a case where inference produces dates structurally impossible (e.g., pre-`lifetime_start`), the implementation PR records a follow-up TODO but does not block on the clamp |
| **Evidence backfill cadence** | **Operator-on-demand one-shot first.** No scheduler / background service integration. | The stage is registered in `_STAGE_SPECS` like any other on-demand stage. Future-work TODO for daily-background-top-up via `data_repair_service` is documented but out of scope |

## 3. Migration shape

### 3.1 Alembic revision

File: `platform/migrations/versions/20260602_0200_fundamentals_period_source_evidence.py`
(timestamp slot adjacent to today's other migrations; pick the next free
slot during impl).

`revision: str = "20260602_0200"`. `down_revision: str | None =` the
current alembic head as of impl start.

### 3.2 DDL

```sql
CREATE TABLE platform.fundamentals_period_source_evidence (
    ticker            text        NOT NULL,
    period_end_date   date        NOT NULL,
    source            text        NOT NULL,
    outcome           text        NOT NULL,
    attempted_at      timestamptz NOT NULL,
    notes             text        NULL,
    created_at        timestamptz NOT NULL DEFAULT NOW(),
    updated_at        timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT fundamentals_period_source_evidence_pk
        PRIMARY KEY (ticker, period_end_date, source),
    CONSTRAINT fundamentals_period_source_evidence_outcome_check
        CHECK (outcome IN (
            'yielded',         -- source returned the row
            'empty',           -- FMP cascade fetched but no row for this period
            'extract_none',    -- SEC companyfacts fetched but extract_period returned None
            'fetch_failure'    -- SEC 404 / FMP 5xx / DataProviderOutage
        )),
    CONSTRAINT fundamentals_period_source_evidence_source_check
        CHECK (source IN (
            'fmp_historical',     -- historical_fundamentals_quarterly
            'fmp_refresh',        -- fundamentals_refresh
            'sec_companyfacts'    -- sec_fundamentals_fallback
        ))
);

CREATE INDEX fundamentals_period_source_evidence_ticker_period_idx
    ON platform.fundamentals_period_source_evidence (ticker, period_end_date);

-- Trigger to maintain updated_at on UPSERT
CREATE OR REPLACE FUNCTION platform.fundamentals_period_source_evidence_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER fundamentals_period_source_evidence_updated_at_trg
    BEFORE UPDATE ON platform.fundamentals_period_source_evidence
    FOR EACH ROW
    EXECUTE FUNCTION platform.fundamentals_period_source_evidence_touch_updated_at();
```

### 3.3 Migration discipline

- Idempotent via `CREATE TABLE IF NOT EXISTS` only if matching the
  existing migration precedent — otherwise standard `CREATE TABLE` and
  let Alembic's migration-version table prevent re-apply.
- No back-fill of historical evidence from this migration; the
  populator stage handles that.
- `down_revision` chain MUST match the current alembic head at the
  moment the implementation PR is opened.

## 4. Outcome enum semantics

| Outcome | Producer | Meaning | Qualifies for exclusion? |
|---|---|---|---|
| `yielded` | any source | provider returned a row for this `(ticker, period_end_date)`; landed in `fundamentals_quarterly` | **NO** — row should be in `fundamentals_quarterly`; validator's gap inference shouldn't even surface it |
| `empty` | `fmp_historical` / `fmp_refresh` | FMP fetched the universe pull successfully; no row for this period in the returned set | **YES (FMP leg)** |
| `extract_none` | `sec_companyfacts` | SEC `companyfacts/CIK<n>.json` fetched cleanly; `sec.extract_period(facts, pe)` returned `None` | **YES (SEC leg)** |
| `fetch_failure` | any | HTTP 404 / 5xx / `DataProviderOutage` / non-200 status | **NO** — needs re-attempt, not exclusion |

The validator's freshness-gated join requires `outcome IN ('empty',
'extract_none')` for BOTH a `fmp_*` row AND a `sec_companyfacts` row,
each `attempted_at >= NOW() - INTERVAL '180 days'`. The validator
explicitly checks that no `fetch_failure` row exists in the freshness
window for either provider.

## 5. Evidence-populator stage

### 5.1 Stage name + registration

Name: `confirmed_data_gap_evidence_populator`. Registered in
`scripts/ops.py::_STAGE_SPECS` with `HEAVY_STAGE_TIMEOUT_SEC` (heavy-lane
by adjacency).

### 5.2 Knobs

| Knob | Default | Purpose |
|---|---|---|
| `dry_run` | `True` (str `"true"`) | **Hard true** at the stage layer. When `True`, the stage computes the per-ticker per-period scope, runs the FMP cascade + SEC fallback, but **does NOT write evidence rows.** When `False`, evidence rows are written. |
| `tickers` | none (full FAIL universe) | Comma-separated subset. Scopes the populator to specific tickers. |
| `limit` | `0` (no cap) | Bound the number of tickers processed in one run (operator-on-demand smoke). |
| `use_bulk_zip` | `True` | Bulk-first invariant; SEC cross-walk and FMP archive lookup honor archive-first read. `False` raises. |
| `archive_max_age_days` | `7` | Archive freshness floor (matches symbol-history convention). |

### 5.3 Run mechanics

1. Universe query: `SELECT ticker, period_end_date FROM` the validator's
   current FAIL set (re-runs the gap-inference SQL filtered to
   `evaluated_routed` tickers with non-empty `ticker_gaps`).
2. For each `(ticker, period_end_date)`:
   - If `outcome` already recorded fresh for **both** FMP and SEC: skip
     (idempotent).
   - Else attempt FMP cascade for that period (single bulk-zip path);
     record `outcome` and `attempted_at`.
   - Then attempt SEC fallback (via the post-PR-448 dry-run-knob'd
     handler); record `outcome` and `attempted_at`.
3. `dry_run=true`: counters reported; zero writes. `dry_run=false`:
   `INSERT INTO fundamentals_period_source_evidence (...) ON CONFLICT
   (ticker, period_end_date, source) DO UPDATE SET outcome=EXCLUDED.outcome,
   attempted_at=EXCLUDED.attempted_at, notes=EXCLUDED.notes` (upsert with
   latest-write-wins).

### 5.4 Manifest

CSV at `data/confirmed_data_gap_evidence_manifest_<UTC-stamp>.csv`
per the precedent. Columns: `ticker, period_end_date, fmp_outcome,
sec_outcome, would_exclude` (where `would_exclude` is `true` iff both
are `empty`/`extract_none` AND no `fetch_failure`).

## 6. Bulk/S3-first invariants

- FMP cascade leg: uses the existing `historical_fundamentals_quarterly`
  archive-first read path (PR #437 precedent). No new provider crawl
  pattern.
- SEC leg: uses the post-PR-448 `handle_sec_fundamentals_fallback`
  with `dry_run=false` ONLY when the populator stage's `dry_run=false`
  (operator-authorized live run). The SEC handler's existing
  archive-cached pattern via `data/sec_submissions/` is reused.
- No per-row crawl. Per-ticker iteration is the existing handler
  design; preserved.
- `use_bulk_zip=false` raises before any HTTP call (sentinel-test
  enforced).

## 7. FMP/SEC handler extensions (evidence write)

Both the FMP cascade and the SEC fallback handlers extend to write
evidence rows on every requested period:

### 7.1 `handle_sec_fundamentals_fallback` (extend)

After the per-period loop:

```python
for pe in missing:
    extracted = sec.extract_period(facts, pe)
    if extracted is None:
        evidence_rows_pending.append((ticker, pe, "sec_companyfacts", "extract_none", today_ts))
        continue
    archive_rows.append(...)
    evidence_rows_pending.append((ticker, pe, "sec_companyfacts", "yielded", today_ts))
```

If `dry_run=false`, `evidence_rows_pending` is upserted to
`fundamentals_period_source_evidence` inside the same transaction as
the `manifest_lifecycle` write. If `dry_run=true`, evidence rows are
NOT written (preserving the dry-run contract).

### 7.2 `handle_historical_fundamentals_quarterly` (extend)

Mirror semantic: for each (ticker, period_end_date) in the request
set, record `outcome='empty'` for each period NOT in the FMP payload,
`outcome='yielded'` for each period that WAS in the payload.

Both extensions land in the same implementation PR.

## 8. Validator join logic

In `fundamentals_quarterly_completeness._evaluate`, BEFORE the
`if ticker_gaps: gaps[ticker] = ...` line at ~399:

```python
if ticker_gaps:
    # Query evidence substrate for dual-source-confirmed-unavailable.
    evidence_rows = await conn.fetch(
        """
        SELECT period_end_date
        FROM platform.fundamentals_period_source_evidence
        WHERE ticker = $1
          AND period_end_date = ANY($2::date[])
          AND attempted_at >= NOW() - INTERVAL '180 days'
          AND outcome IN ('empty', 'extract_none')
        GROUP BY period_end_date
        HAVING bool_or(source IN ('fmp_historical', 'fmp_refresh'))
           AND bool_or(source = 'sec_companyfacts')
           AND NOT bool_or(outcome = 'fetch_failure')
        """,
        ticker, list(ticker_gaps),
    )
    evidenced = {r["period_end_date"] for r in evidence_rows}
    excluded_confirmed_data_gap_evidenced += len(evidenced)
    excluded_confirmed_data_gap += len(evidenced)
    ticker_gaps = [d for d in ticker_gaps if d not in evidenced]

# Existing logic continues:
if ticker_gaps:
    gaps[ticker] = (sorted(ticker_gaps), primary)
```

The validator is **async-aware** already (per the existing
`async def` signatures). No new async surface introduced.

## 9. Sub-counter reporting

`_Evaluation` gains `excluded_confirmed_data_gap_evidenced: int = 0`.
At completion, the structlog `info` log adds:

```python
log.info(
    "validation.fundamentals_quarterly_completeness.evaluation_done",
    excluded_confirmed_data_gap=ev.excluded_confirmed_data_gap,
    excluded_confirmed_data_gap_sparse=ev.excluded_confirmed_data_gap - ev.excluded_confirmed_data_gap_evidenced,
    excluded_confirmed_data_gap_evidenced=ev.excluded_confirmed_data_gap_evidenced,
    ...
)
```

The frozen `CheckResult` itself is unchanged. Dashboard reads from the
log + the diagnostic counter API (see §10).

## 10. Dashboard surface

`dashboard_components/data_quality_panel.py` (or the equivalent
panel — verify path in impl) gains a "Confirmed-data-gap evidence"
breakdown showing:

- Total `excluded_confirmed_data_gap` count.
- Split: `_sparse` (< 2 filings) vs `_evidenced` (dual-source).
- Latest 5 tickers entering the `_evidenced` bucket (linkable to
  the evidence table).

Excluded rows are operator-visible. No silent hiding.

## 11. Edge cases (validator side)

| Case | Implementation handling |
|---|---|
| **AEVA SPAC-merger Q1 2021-03-31** | FMP populator records `outcome='empty'`; SEC populator records `outcome='yielded'` (extract returned non-None). The validator's join requires both legs `empty`/`extract_none`, but SEC is `yielded` → period does NOT qualify → row enters `evaluated_routed` as today. AEVA's gap drops by 1 because SEC fallback's normal write path landed the period |
| **ARDT physical_truth anomaly** | FMP rows rejected by `physical_truth` gate → FMP cascade records `outcome='empty'` (no row landed). SEC may also record `outcome='extract_none'` if SEC lacks the period. Both empty → exclusion qualifies. **But operator's spec §8 explicitly excludes ARDT from this path until the underlying FMP issue is triaged.** Implementation PR adds a `ticker IN (ARDT_WATCHLIST)` override that forces `excluded_dark` instead of `excluded_confirmed_data_gap`. The watchlist is a constant; spec future-work to make it dynamic |
| **AGPU `asset_class='spac'`** | The SEC fallback handler excludes AGPU from its universe (`asset_class='stock'`). The FMP cascade also excludes SPACs. → No evidence rows accrue for AGPU. AGPU's inferred gaps stay in FAIL. **No change in implementation PR.** Deferred classifier triage handles this |
| **Outage during FMP cascade** | `DataProviderOutage` → `outcome='fetch_failure'` → period does NOT qualify for exclusion → row stays in FAIL → re-attempt on next populator run |

## 12. Acceptance gates (implementation PR — not this PR)

| Gate | Target |
|---|---|
| Migration runs cleanly on a test DB and the down-revision lineage matches | ✓ |
| `confirmed_data_gap_evidence_populator --param dry_run=true` smoke against 10 tickers | runs to completion; manifest produced; zero writes |
| `confirmed_data_gap_evidence_populator --param dry_run=false` bounded against 10 tickers | first run inserts N rows; second run is idempotent (upsert ON CONFLICT DO UPDATE) |
| Validator re-run reads new evidence | per-ticker FAIL count drops by exactly the number of qualifying periods |
| `CheckResult` frozen-shape sentinel | PASS (no new fields on `CheckResult`) |
| `_infer_missing_period_ends` byte-freeze sentinel | PASS (the inference function is unchanged) |
| `gh pr checks` | green; Claude review heavy-lane PASS |
| Full pytest single-process + order-flip | green |
| Vulture / ruff / gitleaks | clean against the diff |
| Dashboard panel renders the new sub-counter | manual operator verification |

## 13. Hermetic test plan (implementation PR)

* **Migration sentinel test** — assert `revision`, `down_revision`,
  PK shape, CHECK constraint payload (string-match against the
  migration source).
* **Populator dry-run test** — fixture: 3 tickers × 2 periods each;
  with `dry_run=true`, assert manifest produced + zero evidence rows
  written (mock pool); `would_exclude` correctly computed per row.
* **Populator live-run test** — `dry_run=false`; assert UPSERT
  executed; second invocation is no-op (`ON CONFLICT DO UPDATE` with
  same payload).
* **Validator join test** — fixture: ticker T has 3 inferred missing
  periods; 2 have dual-source evidence (`empty`/`extract_none`,
  fresh); 1 has only FMP `empty`. Validator excludes 2 periods, keeps
  1 in FAIL.
* **Validator freshness gate** — fixture with evidence > 180 days
  ago → period stays in FAIL (does NOT exclude).
* **Validator fetch_failure rejection** — fixture with `fetch_failure`
  in window → period stays in FAIL.
* **AEVA-shape test** — fixture: FMP empty + SEC yielded → does NOT
  exclude; row enters via the normal `yielded` write path.
* **ARDT watchlist override** — fixture: ARDT meets the dual-source
  criteria; assertion routes to `excluded_dark` not
  `excluded_confirmed_data_gap`.
* **`CheckResult` frozen-shape sentinel** — assert `CheckResult.__init__`
  signature unchanged from current main.
* **`_infer_missing_period_ends` byte-freeze sentinel** — sha256
  pin of the function source.

All hermetic; stdlib + pytest + `unittest.mock` only. No DB, no
network.

## 14. Rollback / no-op

* The stage is **additive-only** to `fundamentals_period_source_evidence`.
* The validator change is non-destructive — periods move from FAIL
  to `excluded_confirmed_data_gap_evidenced`; data is not lost.
* Rollback: `DROP TABLE platform.fundamentals_period_source_evidence`
  via Alembic down-revision. Validator continues to function (the
  join returns empty when the table doesn't exist — but actually the
  join would fail. **Spec correction:** the validator's read of the
  evidence table MUST gracefully handle "table doesn't exist" by
  treating it as an empty result. Implementation PR adds a
  `pg_class` existence check at evaluator startup; if missing, the
  evidence-join path is skipped entirely.
* No-op rerun: idempotent UPSERT keeps the freshest evidence per
  `(ticker, period_end_date, source)`.

## 15. Operator live-run sequence (post-impl merge)

1. **Merge implementation PR.**
2. **Apply migration** to live DB: `alembic upgrade head` (the operator
   runs this; the impl-PR description includes the command).
3. **Populate evidence (live, bounded)**: run the populator stage
   `--param dry_run=false --param limit=50` against a 50-ticker
   smoke. Verify counters + manifest.
4. **Re-run the validator** (operator's existing data-ops daemon
   already re-runs `fundamentals_quarterly_completeness` regularly;
   no explicit step needed unless operator wants an immediate
   re-evaluation).
5. **Verify FAIL count drops** — read the structlog output for the
   new `excluded_confirmed_data_gap_evidenced` sub-counter; verify
   ≤ ~50 rows entered the bucket.
6. **Full populator run** (live, no limit): once smoke is clean,
   `--param dry_run=false` with no limit. Expect ~143 evidence rows
   per the spec's empirical floor (the AEVA hit means at most 144 - 1
   = 143 would qualify; some may have outage-shaped failures that
   need re-attempt).
7. **Confirm `DATA_OPERATIONS_COMPLETE`** is achievable if residual
   FAIL count is 0; if > 0, operator decides next-arc disposition
   per spec §12.

## 16. Non-goals (preserved from spec)

- No `_infer_missing_period_ends` change.
- No `CheckResult` shape change.
- No validator threshold loosening.
- No live SEC fallback writes in plan PR (only operator-authorized
  live populator runs post-impl-merge).
- No `fundamentals_quarterly` cleanup / quarantine / delete.
- No AGPU reclassification.
- No ticker-reuse cleanup reopen.
- No scheduler / background daemon integration.

## 17. Open questions for the implementation PR

These do not block this plan PR — they are research items for the
implementer to resolve before opening the implementation PR:

1. **Exact alembic head** at impl-PR-open time — `down_revision`
   chain depends on what's merged.
2. **`ARDT_WATCHLIST` constant location** — `tpcore/quality/validation/
   checks/fundamentals_quarterly_completeness.py` module-level vs a
   shared config. Recommendation: module-level constant; small
   surface; revisitable.
3. **`pg_class` existence check at startup** — exact SQL idiom (e.g.,
   `SELECT to_regclass('platform.fundamentals_period_source_evidence')`).
   Recommendation: `to_regclass` (pure read; no permissions issue).
4. **Dashboard panel exact path** — verify `dashboard_components/` has
   the data-quality panel or whether a new component file is needed.

## 18. Test-of-tests (this plan PR)

A 28-case sentinel test at
`tests/test_excluded_confirmed_data_gap_plan_documented.py` pins the
load-bearing plan claims so a future "tidy" pass cannot silently drop:

- The resolved operator decisions (180-day freshness, dry-run no-write,
  inference clamp SPLIT, on-demand cadence).
- The migration + table shape + PK + CHECK constraints.
- The outcome enum (4 values).
- The source enum (3 values).
- The evidence-populator stage knobs + defaults.
- The validator join semantics (freshness + dual-source + no
  fetch_failure).
- The `CheckResult` frozen-shape preservation.
- The `_infer_missing_period_ends` non-change.
- The AEVA / ARDT / AGPU edge-case handling.
- The rollback / no-op safety.
- The hard rules (no threshold loosening, no cleanup, no AGPU
  reclassification, etc.).
