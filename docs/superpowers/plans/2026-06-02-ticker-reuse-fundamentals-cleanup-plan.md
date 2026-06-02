# Ticker-reuse fundamentals cleanup — PLAN

**Status:** PLAN ONLY. No implementation. No DB writes. No deletes. No
schema migration. No validator change. No threshold loosening. No
per-ticker SEC HTTP crawl (bulk-first per the standing
`feedback_bulk_before_api_crawl_REINFORCED`).

Follows spec PR #439 + the §13 #1 NEEDS_REPO_VERIFICATION resolution
captured below. Authorizes the implementation arc only; the impl PR
opens after this plan merges.

Drafted: 2026-06-02. Owner: operator. Author: SEC-metadata /
issuer-lifecycle / data-quality track.

## 1. Verdict

**The cleanup arc is unblocked.** The §13 #1 NEEDS_REPO_VERIFICATION on
the suspicious mega-caps resolved with **22 / 22 confirmed** (zero
FPFD-extraction defects remaining). The 6,016-row residual is
genuinely ticker-reuse / CIK-transition artefacts and is safe to
process under the spec's evidence-gated cleanup pattern.

The implementation arc is:

1. **One migration PR** — adds `platform.fundamentals_quarterly_archive`
   + `platform.fundamentals_quarterly_quarantine` sidecar tables.
2. **One stage PR** — adds `scripts/ops.py::_stage_cleanup_ticker_reuse_fundamentals`
   with the dry-run + bounded-live + full-live knobs defined in §6.
3. **One closeout doc-only PR** — captures the post-execution empirical
   result, residual at end of arc, and the
   `weak_evidence_keep` operator-triage list. (Same shape as PR #438.)

Plan-only here. No row touches until the impl PR's bounded-live gate
fires with the operator's explicit `dry_run=false` opt-in.

## 2. Mega-cap re-audit disposition — §13 #1 RESOLVED

The spec PR #439 §13 #1 flagged a NEEDS_REPO_VERIFICATION: re-run the
bulk reader against the suspicious-mega-cap residual cohort (BLK,
FERG, RTO, NEUP, CAMP, MDXH and 9 others) to distinguish
FPFD-extraction defects from genuine ticker reuse.

**Re-audit ran 2026-06-02 ~10:18 UTC, 22 tickers sampled** (the §3
top-15 plus the 6 prior accepted-in-place mega-caps for control):

| metric | value |
|--------|------:|
| Sampled tickers | 22 |
| `bulk_extracted_fpfd == stored_fpfd` | **22 / 22** |
| Still-wrong FPFD (extractor disagrees with DB) | **0** |
| Confirmed ticker-reuse (or CIK-transition) | **22** |

Tickers verified:
AAPL · BAC · BLK · C · CAI · CAMP · ELE · EU · FERG · GLXY · JBS ·
JPM · LAC · MDXH · META · MH · MSFT · NEUP · RTO · SBET · UNIT · XOM.

Interpretation: SEC's `submissions.json` for each current CIK
confirms the stored FPFD. The pre-FPFD rows in `fundamentals_quarterly`
correspond to periods when the ticker symbol was attached to a
*different* CIK (predecessor entity) — classic ticker reuse or
CIK-transition (e.g. a re-org where the issuer got a new CIK).

**Disposition:** the spec's high-confidence ticker-reuse classification
in §5 ranks 1–3 is safe to apply across the 783 residual cohort. **No
further FPFD work required.**

## 3. Decision table — remaining spec §13 decisions resolved

| # | Spec PR #439 question | Plan PR decision | Rationale |
|---|------------------------|-------------------|-----------|
| 1 | NEEDS_REPO_VERIFICATION on suspicious mega-caps | **RESOLVED — 22/22 confirmed** | §2 above |
| 2 | Re-run `corp_history_edgar_backfill` before manifest generation? | **NO separate re-run; the impl PR's dry-run stage will RE-READ the existing local `data/sec_submissions/` cache + bulk `submissions.zip` for ALL 783 affected CIKs in one pass** | Bulk-before-API-crawl. `corp_history_edgar_backfill` already runs against the same bulk zip; the cleanup stage will share the bulk reader plumbing |
| 3 | Archive-table column shape (1-to-1 mirror vs minimal) | **1-to-1 mirror + 4 audit columns** (`archived_at`, `disposition_reason`, `decided_by_run_id`, `evidence_summary`) | Cheapest restore path; storage is essentially free for ~6k rows; auditability is non-negotiable |
| 4 | Quarantine table vs `is_quarantined` column | **Quarantine SIDECAR TABLE** (not column) | Same precedent as archive table; avoids `WHERE NOT is_quarantined` clauses in every consumer |
| 5 | Re-key (Option C) for the small confirmed-predecessor subset | **DEFERRED to a future arc** | Predecessor identity resolution is a sub-arc; default to archive for now. If a future operator session has confirmed predecessor evidence (via `ticker_history` / `corporate_events.ticker_swap`), a separate stage knob (`--param rekey_to_predecessor=true`) can be added in a follow-up impl PR |
| 6 | `weak_evidence_keep` triage cadence | **No cadence set; rows stay in `fundamentals_quarterly`; a single `data_quality_log` row per ticker carries the operator-triage flag** | Operator review is the right gate; setting a cadence policy requires more empirical evidence than this plan has |

## 4. Future migration shape

**One migration**, adding two sidecar tables. Implementation PR scope.

### 4.1 `platform.fundamentals_quarterly_archive`

Mirror of `platform.fundamentals_quarterly` (20 cols) plus 4 audit
columns:

```sql
CREATE TABLE platform.fundamentals_quarterly_archive (
    -- mirror of fundamentals_quarterly (every column, same types)
    id                    BIGINT,
    ticker                TEXT NOT NULL,
    filing_date           DATE,
    period_end_date       DATE NOT NULL,
    period_label          TEXT,
    net_income            NUMERIC,
    fcf                   NUMERIC,
    operating_cash_flow   NUMERIC,
    capex                 NUMERIC,
    revenue               NUMERIC,
    total_assets          NUMERIC,
    total_liabilities     NUMERIC,
    current_assets        NUMERIC,
    current_liabilities   NUMERIC,
    receivables           NUMERIC,
    cash_and_equivalents  NUMERIC,
    shares_outstanding    NUMERIC,
    recorded_at           TIMESTAMPTZ,
    pb                    NUMERIC,
    de                    NUMERIC,
    classification_id     BIGINT,
    -- audit columns (new)
    archived_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disposition_reason    TEXT NOT NULL,
    decided_by_run_id     UUID NOT NULL,
    evidence_summary      TEXT NOT NULL,
    -- primary key on (ticker, period_end_date, archived_at) so
    -- multiple archive cycles for the same row are possible (rare
    -- but valid if rollback + re-archive happens).
    PRIMARY KEY (ticker, period_end_date, archived_at)
);

CREATE INDEX ix_fq_archive_run ON platform.fundamentals_quarterly_archive
    (decided_by_run_id, archived_at);
CREATE INDEX ix_fq_archive_ticker ON platform.fundamentals_quarterly_archive
    (ticker, archived_at);
```

`disposition_reason` is a free-text string carrying the rank from
spec §5 (`evidence_rank_3_different_issuer_owns_ticker`) plus a
human-readable summary. `evidence_summary` carries the
issuer_securities/issuer_history diagnostic that produced the
classification.

### 4.2 `platform.fundamentals_quarterly_quarantine`

Same shape as the archive table but with one fewer audit column
(`disposition_reason` constrained to a smaller enum-shaped set):

```sql
CREATE TABLE platform.fundamentals_quarterly_quarantine (
    -- mirror of fundamentals_quarterly (every column)
    -- … (20 cols, same as above)
    -- audit columns
    quarantined_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disposition           TEXT NOT NULL CHECK (disposition IN (
        'ambiguous_predecessor_unknown',
        'corp_history_substrate_sparse',
        'cik_null',
        'operator_review_pending'
    )),
    decided_by_run_id     UUID NOT NULL,
    evidence_summary      TEXT NOT NULL,
    promoted_back_at      TIMESTAMPTZ,  -- non-null if operator restored
    PRIMARY KEY (ticker, period_end_date, quarantined_at)
);
```

Quarantine rows can be **promoted back** to the main table by a
future operator-on-demand stage if the predecessor identity becomes
known. `promoted_back_at` records the round-trip.

### 4.3 No change to `platform.fundamentals_quarterly` schema

The main table is untouched. Validator continues to read it AS-IS.
Engines / dashboards / backtest consume the same rows + columns.

## 5. Future ops-stage shape

`scripts/ops.py::_stage_cleanup_ticker_reuse_fundamentals` —
operator-on-demand only (NOT in `OPS_UPDATE_STAGES`). Heavy-lane by
discipline (scripts/ops.py path).

### 5.1 Knobs

| knob | default | effect |
|------|---------|--------|
| `dry_run`               | **True**  | Operator hard rule. `false` writes archive + delete; `true` only writes the manifest CSV. |
| `manifest_path`         | `data/fundamentals_quarterly_cleanup_manifest_<UTC>.csv` | Where the dry-run writes; the live-run reads. |
| `evidence_level`        | `strong`  | `strong` = ranks 1–3 dispositive only; `weak` = include rank 4–6 candidates; `all` = include `ambiguous` (still NOT deleted in `all` mode — they go to quarantine). |
| `tickers`               | none      | Comma-list of explicit tickers; subsets the 783. |
| `limit`                 | none      | Cap on rows processed; useful for severity-bucket sequencing. |
| `archive_only`          | `False`   | When `true`, run the archive INSERT path but skip the DELETE. Operator can inspect archived rows before the destructive step. |
| `delete_after_archive`  | **False** | Hard guard. Must be `true` AND `dry_run=false` AND `archive_only=false` AND `evidence_level=strong` for a row to be deleted from main. |
| `quarantine_weak`       | **True**  | When `true`, weak-evidence rows go to quarantine sidecar. When `false`, they're skipped entirely. Default is `True` (preserve traces). |
| `use_bulk_zip`          | **True**  | Inherited from `_stage_backfill_sec_metadata` — every evidence read uses the bulk reader; ZERO per-CIK HTTP. |
| `bulk_zip_cache_path`   | `/tmp/sec_submissions.zip` | Same as backfill stage. |
| `bulk_zip_force_download`| `False`  | Same. |

### 5.2 Hard invariants encoded in the stage

These are **structural in the code**, not toggle-able:

1. **No DELETE without a matching archive INSERT in the same
   transaction.** Implemented via the `archive_then_delete()` helper
   that does both writes inside one `async with conn.transaction():`.
2. **No DELETE of weak-evidence rows.** The `delete_after_archive`
   path enforces `disposition IN ('high_confidence_ticker_reuse')` at
   the row level.
3. **No DELETE of FPFD-unverified rows.** Before each row's
   classification, the stage re-reads the issuer's full-history FPFD
   via the bulk reader; if the extracted FPFD does NOT match the
   stored FPFD, the row is rejected (route to `data_quality_log` with
   `disposition='fpfd_drift_detected_skipped'`).
4. **Manifest reproducibility.** The dry-run manifest must be
   reproducible from the bulk-zip + DB state alone (no per-ticker SEC
   HTTP). The bounded-live re-validates each row against the same
   substrate before mutating; any verdict change → row dropped from
   the run + logged.
5. **Bounded by manifest row IDs.** The bounded-live's DELETE WHERE
   clause uses the row's `(ticker, period_end_date)` composite key
   from the manifest. No `WHERE period_end_date < FPFD` blanket
   DELETE — every DELETE is per-row.
6. **No mass DELETE.** A single transaction touches at most N rows
   where N ≤ 100 (configurable via `--param batch_size=N`); larger
   sets are chunked into per-batch transactions.

### 5.3 Output payload shape

```json
{
  "scope_size": 783,
  "manifest_path": "data/.../<UTC>.csv",
  "dry_run": true,
  "manifest_writes": 6016,
  "high_confidence_archive_count": 5234,
  "ambiguous_quarantine_count": 711,
  "weak_evidence_keep_count": 71,
  "fpfd_drift_detected_skipped_count": 0,
  "missing_from_bulk_count": 0,
  "shard_error_count": 0,
  "bulk_zip": {
    "zip_path": "/tmp/sec_submissions.zip",
    "local_hit_count": 0,
    "bulk_hit_count": 783,
    "missing_count": 0,
    "shard_count": 412,
    "shard_error_count": 0
  }
}
```

When `dry_run=false`, `manifest_writes` → 0 (no CSV produced;
manifest is read from `manifest_path` instead) and the three
disposition counters become **actual row mutations** instead of
forecasts.

## 6. Cleanup flow

### 6.1 Dry-run (operator step 1)

```bash
python scripts/ops.py --stage cleanup_ticker_reuse_fundamentals \
    --param dry_run=true \
    --param evidence_level=strong \
    --param use_bulk_zip=true
```

For each of the 783 affected tickers:

1. Fetch the current `(ticker, cik, first_public_filing_date)` from
   `ticker_classifications`.
2. Re-read the bulk reader's submissions payload for the CIK.
3. Re-extract FPFD via `extract_filing_metadata`. If
   `extracted_fpfd != stored_fpfd`, mark `fpfd_drift_detected_skipped`
   and continue.
4. For each pre-FPFD `period_end_date` row in the ticker:
   - Run the §5 evidence classifier (ranks 1–3).
   - Write a manifest row with the disposition.
5. Write the manifest CSV to `manifest_path`.

Returns the §5.3 payload with `dry_run=true`. **Zero DB writes.**

### 6.2 Bounded live (operator step 2)

```bash
python scripts/ops.py --stage cleanup_ticker_reuse_fundamentals \
    --param dry_run=false \
    --param manifest_path=data/.../<UTC>.csv \
    --param tickers=<74 single-row severity bucket> \
    --param archive_only=false \
    --param delete_after_archive=true \
    --param evidence_level=strong
```

For each row in the manifest matching the cohort:

1. Re-validate the row's evidence against the live substrate (same
   bulk reader path).
2. If the verdict matches the manifest's
   `high_confidence_ticker_reuse`, run the
   `archive_then_delete()` transaction:
   - `INSERT INTO fundamentals_quarterly_archive (…) SELECT … FROM
     fundamentals_quarterly WHERE …`
   - `DELETE FROM fundamentals_quarterly WHERE id = $1`
3. If the verdict changed (`fpfd_drift_detected_skipped` or
   evidence-rank disagreement), log to `data_quality_log` and skip.

### 6.3 Severity-bucket sequence

| step | cohort | rows |
|------|--------|-----:|
| §6.2-a | 1-row severity bucket | 74 |
| §6.2-b | 2–3 severity bucket | 267 |
| §6.2-c | 4–9 severity bucket | 2,536 |
| §6.2-d | 10–19 severity bucket | 1,255 |
| §6.2-e | 20+ severity bucket | 1,884 |

Each step requires explicit operator authorization. The full set is
not run in one go.

### 6.4 Quarantine for ambiguous rows

Done by the same stage with `evidence_level=all quarantine_weak=true`.
Ambiguous rows route to `fundamentals_quarterly_quarantine` via an
`archive_then_quarantine()` transaction (same shape, different
sidecar).

## 7. Acceptance gates

For the FUTURE implementation PR (NOT this plan):

1. Migration applies cleanly + downgrades cleanly (Alembic round-trip).
2. Targeted tests pass per §8.
3. Whole-suite pytest (single-process + reversed-order) green.
4. `ruff` / `vulture` / `gitleaks` / `check_manifests` clean.
5. CI rollup `statusCheckRollup` `SUCCESS`.
6. Operator-side dry-run sanity: manifest CSV produced;
   `fundamentals_quarterly.total` unchanged; manifest's
   high-confidence count ≈ 5,234 (within ±5 % of dry-run forecast).
7. Operator-side bounded-live (74-row cohort): exactly 74 archive
   INSERTs + 74 main-table DELETEs; 0 quarantine rows; 0
   `fpfd_drift_detected_skipped`; 0 non-cohort updates.
8. Post-bounded-live: `fundamentals_quarterly.total` decreases by
   exactly the cohort size; `fundamentals_quarterly_archive.total`
   increases by the same.
9. Round-trip: an operator-on-demand `restore_from_archive` query
   restores a sample archived row back to the main table cleanly.

## 8. Test plan (10 tests in the future implementation PR)

Per the spec PR #439 §12 list, hermetic tests in
`tests/test_ticker_reuse_cleanup.py`:

1. **`archive_table_schema_sentinel`** — alembic migration produces
   the §4.1 column set + indexes.
2. **`quarantine_table_schema_sentinel`** — §4.2 shape including the
   `disposition` CHECK constraint enum.
3. **`manifest_csv_schema_sentinel`** — fixed column set + ordering
   (`ticker,period_end_date,current_cik,current_fpfd,proposed_disposition,evidence_rank_used,evidence_summary`).
4. **`dry_run_writes_zero_db_rows`** — `dry_run=true` produces
   manifest only; `fundamentals_quarterly.total` unchanged;
   `fundamentals_quarterly_archive.total` unchanged.
5. **`archive_before_delete_enforced`** — DELETE without prior
   archive INSERT in the same transaction is rejected (the helper
   raises `RuntimeError`).
6. **`weak_evidence_quarantined_not_deleted`** — fixture with rank-1
   evidence routes to quarantine, NOT archive+delete.
7. **`strong_evidence_deletes_only_manifest_matched_rows`** —
   fixture with multiple per-ticker rows; only the manifest-listed
   row is deleted.
8. **`fpfd_unverified_row_rejected`** — fixture where re-read FPFD
   differs from stored FPFD: row routes to `data_quality_log` with
   `disposition='fpfd_drift_detected_skipped'`.
9. **`rollback_restores_archived_row`** — archive → main UNDO query
   succeeds; validator sees the restored row.
10. **`bulk_first_evidence_source_documented`** — source sentinel that
    string-matches `use_bulk_zip=True` + the `SECSubmissionsBulkReader`
    import in the stage (forbids regressing to per-CIK HTTP).

Plus 1 integration sentinel in `tests/test_backfill_sec_metadata_stage.py`:

11. **`fundamentals_quarterly_total_unchanged_in_dry_run`** — full
    dry-run preserves the main-table row count.

## 9. Rollback plan

If the bounded-live (74-cohort) reveals a defect:

1. **Halt the run.** `STOP_IF` clauses in §10 catch most cases
   automatically.
2. **Restore from archive.** Operator-on-demand query:
   ```sql
   INSERT INTO platform.fundamentals_quarterly (…)
   SELECT … FROM platform.fundamentals_quarterly_archive
   WHERE decided_by_run_id = $1;
   ```
3. **DELETE the archive rows** (they were a mistake) with
   `decided_by_run_id` matching.
4. **Open a defect doc PR** capturing the run_id, the failure mode,
   the recovery query, and a NEEDS_OPERATOR_DECISION on whether to
   re-run.

Rollback is fully tested via test #9 in §8.

## 10. Stop conditions (encoded in §6.2 bounded-live)

| condition | action |
|-----------|--------|
| Manifest classification rate < 80 % `high_confidence_ticker_reuse` | Abort; run `corp_history_edgar_backfill` first then re-generate manifest |
| Any row's `current_cik` changed between manifest gen + bounded-live | Drop that row from the run; log to `data_quality_log` |
| Any row's bulk-extracted FPFD ≠ stored FPFD | Skip row; route to `data_quality_log` with `fpfd_drift_detected_skipped` |
| `IDENTITY_DIVERGENCE_INVESTIGATE` event emitted during run | Abort the batch immediately; rollback via §9 |
| `fundamentals_quarterly.total` decrement count ≠ archive INSERT count post-batch | Abort + alert; transaction violation |
| Any non-cohort row mutated | Abort; treat as code defect; rollback via §9 |

## 11. Bulk ingestion rule (operator policy)

Per the operator's standing rule re-stated in this task spec:

> Check S3/R2 archive first; sync local cache if present; provider
> bulk download only if archive missing/stale; verify archive/local
> match before ingest/ETL.

This is **already implemented** in `tpcore.sec.submissions_bulk_reader.ensure_zip_cached`
(PR #437). The cleanup stage inherits it via `use_bulk_zip=True`
default. **No new ingestion plumbing required.**

## 12. Implementation prompt (verbatim brief for the future impl PR)

When the operator authorizes the implementation PR, the implementer
should be briefed with this prompt:

> **Task**: implement the ticker-reuse fundamentals cleanup per
> `docs/superpowers/plans/2026-06-02-ticker-reuse-fundamentals-cleanup-plan.md`.
>
> **Scope**:
> * `platform/migrations/versions/<NEW>_*.py` — one alembic
>   revision adding `fundamentals_quarterly_archive` +
>   `fundamentals_quarterly_quarantine` per plan §4.
> * `scripts/ops.py` — add `_stage_cleanup_ticker_reuse_fundamentals`
>   per plan §5; register in `_STAGE_SPECS`.
> * `tests/test_ticker_reuse_cleanup.py` — 10 hermetic tests per
>   plan §8.
> * `tests/test_backfill_sec_metadata_stage.py` — add integration
>   sentinel per plan §8 #11.
>
> **Hard invariants** (plan §5.2): archive-before-delete; no
> weak-evidence delete; no FPFD-unverified delete; manifest
> reproducibility; bounded by manifest row IDs; no mass DELETE.
>
> **Gates** (plan §7): single-process pytest + reversed order-flip
> green; ruff / vulture / gitleaks / check_manifests clean; CI
> `SUCCESS`. Operator-side dry-run + bounded-live sanity per
> plan §7 #6–§7 #9.
>
> **Heavy-lane discipline**: split-review (spec-compliance reviewer
> first, then code-quality reviewer) per `.claude/rules/heavy-lane.md`.
> Operator authorizes the merge.
>
> **What not to do**: do not touch the validator. Do not change any
> threshold. Do not add a soft-delete column to `fundamentals_quarterly`.
> Do not implement the predecessor re-key option (plan §3 decision #5
> defers it). Do not run per-ticker SEC HTTP — `use_bulk_zip=True` is
> default and the source sentinel test #10 forbids regressing.

## 13. Open operator decisions (still pending)

1. **Schedule for the implementation PR**: same operator-session
   sequencing as the FPFD arc (spec → plan → impl), or batch the
   migration + stage + tests into a single impl PR? Recommendation:
   single impl PR (default-lane migration + heavy-lane stage in one
   coherent diff; heavy-lane discipline still applies). **Decision:
   NEEDS_OPERATOR_DECISION.**

2. **Predecessor re-key (Option C from spec §6)** — when do we
   re-evaluate? Recommendation: leave deferred until a separate
   triage shows ≥10 % of the residual has a confirmed predecessor
   ticker via `ticker_history`. Below that threshold, archive is the
   right default. **Decision: NEEDS_OPERATOR_DECISION** at a future
   triage point.

3. **`weak_evidence_keep` operator-review surface** — should the
   `data_quality_log` rows for these be surfaced in the operator
   dashboard? Recommendation: yes, eventually, but not load-bearing
   for this arc. **Decision: NEEDS_OPERATOR_DECISION** for the
   downstream dashboard arc.

## 14. Non-goals (out of scope for the impl PR this plan authorizes)

* No validator change.
* No threshold change.
* No FPFD extraction work (closed in PRs #435–#438).
* No change to `fundamentals_quarterly` main-table schema.
* No engine / risk / order / broker surface change.
* No predecessor re-key (Option C from spec).
* No operator-dashboard work.
* Memstore writes; Anthropic API calls; Docker; Railway deploy;
  admin bypass; secret-bearing files in the diff.

## 15. Next item

If this plan merges: draft the **implementation PR** per the §12
prompt. Files: 1 migration + 1 stage diff in `scripts/ops.py` +
2 test files. Heavy-lane discipline applies (per
`.claude/rules/heavy-lane.md`). Operator's bounded-live authorization
is the load-bearing acceptance gate.
