# Symbol-history evidence backfill — `ticker_history` + `issuer_securities`

**Status:** SPEC ONLY. No implementation. No DB writes. No live API calls.
Direct successor to `2026-06-02-ticker-reuse-fundamentals-cleanup.md`
(PR #441) after the bucket=1 dry-run verification (this PR) proved the
classifier is evidence-starved, not cleanup-ready.

Drafted: 2026-06-02. Owner: operator. Author: symbol-history / issuer-identity track.

## 1. Verdict

The bucket=1 dry-run produced **0 high-confidence delete candidates**
across two consecutive runs, including one immediately after
`corp_history_edgar_backfill` populated +513 `issuer_history` rows.
Only **1 of 74** bucket=1 rows flipped disposition (GIG @ 2024-06-30,
ambiguous → weak_evidence_keep).

Post-backfill manifest tally (`data/fundamentals_quarterly_cleanup_manifest_20260602T1111Z.csv`):

| Disposition                       | Rows |
|-----------------------------------|-----:|
| `high_confidence_archive`         | 0    |
| `ambiguous_predecessor_unknown`   | 50   |
| `weak_evidence_keep`              | 24   |
| **Total**                         | **74** |

`corp_history_edgar_backfill` substrate delta (this run):
`issuers` +27, `issuer_history` +513, `corporate_events`
(`name_only_change`) +263. `ticker_history` and `issuer_securities`
unchanged by that stage (out of scope by design).

The blocker is structural: the cleanup classifier's high-confidence
path requires **rank-3 evidence — proof that the ticker, at the
period_end_date, mapped to a *different* `classification_id` (and
therefore a different issuer) than the current row's `cik` resolves
to.** That evidence lives in two tables, both effectively empty:

| Table                       | Row count (2026-06-02) | Required for rank-3 |
|-----------------------------|-----------------------:|---------------------|
| `platform.ticker_history`   | 13,840                 | Temporal `(ticker → classification_id)` over time |
| `platform.issuer_securities`| 25                     | Temporal `(issuer_id → classification_id)` over time |

`ticker_history` exists at 13,840 rows but is **populated only from the
current ticker_classifications snapshot** — it carries no historical
`(ticker → previous_classification_id)` mappings. `issuer_securities`
has 25 rows and is effectively unpopulated.

**SEC `formerNames` cannot fill this gap.** It captures same-CIK
*name* changes only. A SPAC merger retains the same CIK while the
ticker changes — `formerNames` is silent on the ticker change.
Likewise, a true ticker reuse (e.g., delisted predecessor + later
unrelated registrant inheriting the symbol) requires symbol-history
data that SEC submissions.zip does not contain.

Conclusion: **stop bucket-cleanup execution** (no quarantine, no
delete) until rank-3 substrate exists. Build the substrate from a
bulk-first source. No per-ticker API crawl.

## 2. Why this is *not* "just quarantine everything ambiguous"

Quarantining the 50 ambiguous bucket=1 rows under
`corp_history_substrate_sparse` would lock in an
**evidence-starved decision** that the substrate refresh would have
overturned. PR #441's migration intentionally includes the
`promoted_back_at` column on `fundamentals_quarterly_quarantine`
precisely so a row can be returned to live after evidence improves,
but the operating principle is: **do not quarantine until you have
asked the strongest available evidence source.** That source has not
been asked. Quarantine-as-shortcut violates the standing
`feedback_no_shortcuts_100_pct` discipline.

Bucket 1 is also the most-likely-SPAC bucket. Many of the 50
ambiguous rows are single SPAC-era filings under a SPAC ticker
(GIWWU, HCICU, AFJKU, etc.) that may be **legitimate filings to keep**
rather than ticker-reuse artefacts. Distinguishing those two cases
is exactly what rank-3 substrate enables.

## 3. Evidence model required

The classifier's existing rank-3 check (`scripts/ops.py`
`_classify_ticker_reuse_row`) looks for: for `(ticker, period_end_date)`,
does there exist a `ticker_history` row with `valid_from ≤ period_end_date
< COALESCE(valid_to, infinity)` whose `classification_id` resolves
(via `issuer_securities` join) to an `issuer_id` **different** from
the current row's `cik → issuer_id` resolution?

Required substrate rows therefore must satisfy:

* **`ticker_history(classification_id, ticker, valid_from, valid_to)`** —
  rows MUST cover the *historical* `(ticker → classification_id)`
  mapping, not just the current snapshot. `valid_to` non-NULL is the
  signal that the ticker has been reassigned.
* **`issuer_securities(issuer_id, classification_id, valid_from, valid_to)`** —
  rows MUST map each historical classification_id to the issuer that
  held it during the window. Without this, `ticker_history.classification_id`
  cannot be resolved to an `issuer_id` for the rank-3 different-issuer
  comparison.

Idempotency: both tables MUST be loaded via `ON CONFLICT DO NOTHING`
on a stable natural key (e.g., `(classification_id, ticker, valid_from)`
for `ticker_history`; `(issuer_id, classification_id, valid_from)` for
`issuer_securities`). Reloads MUST be safe.

## 4. Bulk-first source candidates (ranked)

Hard rule: **no per-ticker API crawl**. Each candidate is judged on
(a) whether it provides historical ticker-reassignment evidence, and
(b) whether it is reachable as a bulk artifact (download once, parse
many) or batched (single call returns many records).

### A. R2-archived FMP daily snapshots (preferred)

The R2 archive substrate (`project_r2_archive_substrate_2026_05_26`)
is documented to retain historical FMP daily-pull artifacts. If
FMP `/v3/stock/list` or `/v3/symbol/available-traded` snapshots are
in R2 for ≥ N consecutive days, **replay** yields implicit ticker
history: the first day a `(ticker → CIK)` mapping changes is a
`valid_to`/`valid_from` boundary.

* **Pro:** zero new API calls; uses existing archive; the operator
  already pays for FMP Starter.
* **Pro:** captures SPAC ticker-change events (CIK retained, ticker
  changes — invisible in SEC formerNames).
* **Con:** historical depth limited to whatever R2 actually retains;
  pre-archive periods unreachable.
* **Spec must answer:** what R2 prefix holds these snapshots, and
  what is the earliest available date? (Inspection task in §6.)

### B. FMP bulk symbol-change endpoint

FMP exposes a historical symbol-change feed. The spec must verify
whether it is a single bulk download (acceptable) or a per-ticker
crawl (NOT acceptable under `feedback_bulk_before_api_crawl_REINFORCED`).

* **Pro:** purpose-built for this question — direct
  `(old_ticker, new_ticker, effective_date)` tuples.
* **Con:** historical depth and SPAC coverage need empirical
  verification.
* **Spec must answer:** is there a `/v3/historical/symbol-change`
  bulk artifact (CSV/JSON) downloadable in one request?

### C. SEC submissions.zip `tickers[]` cross-walked with `formerNames` boundaries

Each `CIK.json` entry in `submissions.zip` carries a `tickers` array
of *current* tickers. Combined with `formerNames` start/end dates,
this gives an approximate "ticker X was valid for CIK Y from
[formerNames.last.to] onward" — but ONLY for the most-recent ticker
under that CIK. It does NOT capture intra-CIK ticker changes (e.g.,
SPAC pre-merger ticker → post-merger ticker, same CIK).

* **Pro:** zero new dependencies; bulk artifact already in use.
* **Con:** misses the SPAC-ticker-change case, which is exactly the
  bucket=1 shape. Insufficient on its own.

### D. NASDAQ / NYSE issuer-list daily archives (if archived)

If R2 retains daily NASDAQ-listed / NYSE-listed CSVs, replay gives a
secondary source for ticker validity windows. Secondary because
listed-status changes lag ticker reassignment; primary use is to
cross-validate Path A.

### E. Ruled out (per hard rules)

* **OpenFIGI per-ticker crawl** — even with the 100/request batch
  endpoint, this is iterative and only returns *current* mapping,
  not historical reassignment.
* **CRSP / Compustat** — gold standard for ticker history, but
  not subscribed (out of scope).
* **Polygon `/v3/reference/tickers`** — not in the project's data
  feed roster (`tpcore/providers.py`); adding would require a DFCR.
* **Tradier corp-actions** — already in roster as secondary, but
  per-ticker; not bulk.

## 5. Classifier reframe for SPAC-ticker-change

Even with rank-3 evidence loaded, the classifier needs a sharper
distinction: **same-CIK ticker change** (SPAC merger; retain row) vs
**different-issuer ticker reuse** (delete row). The proposed rule:

* For a candidate row at `(ticker, period_end_date)`:
  * Resolve via `ticker_history` the `classification_id` valid at
    `period_end_date`. Call this `tc_old`.
  * Resolve via `issuer_securities` the `issuer_id` for `tc_old`.
    Call this `issuer_old`.
  * Resolve the current row's `(ticker, NOW)` to `tc_new` → `issuer_new`.
  * If `issuer_old.cik == issuer_new.cik`: **same-issuer ticker
    change** — KEEP the row (it's a legitimate prior filing under the
    same legal entity).
  * If `issuer_old.cik ≠ issuer_new.cik` AND both are non-NULL:
    **true ticker reuse** — high-confidence ARCHIVE-then-DELETE
    candidate.
  * Otherwise: ambiguous (existing path).

This is **a classifier-logic refinement**, not a threshold loosening.
It hardens the high-confidence path against SPAC false positives
that would otherwise destroy legitimate filings.

## 6. Implementation shape (for the *next* plan PR — not this spec)

This spec authorizes only the *plan* PR. Implementation PR is gated
by spec-reviewer PASS + operator spec-read gate per heavy-lane §1.

Expected implementation surface:

* New `scripts/ops.py` stage `symbol_history_evidence_backfill`
  (heavy-lane by adjacency).
* Bulk-first invocation pattern: load from R2 (Path A); if R2
  unavailable, FMP bulk symbol-change endpoint (Path B); fall back
  to SEC `tickers[]` cross-walk (Path C). Per-ticker API crawl is
  a hard NO.
* Idempotent `ON CONFLICT DO NOTHING` INSERTs into `ticker_history`
  and `issuer_securities`.
* No new tables, no migration (schemas already exist).
* Reuse `tpcore.sec.submissions_bulk_reader.SECSubmissionsBulkReader`
  3-tier resolution (local → S3/R2 → SEC) for Path C.
* Cleanup classifier reframe per §5; spec-tracked sentinel test
  asserting same-CIK-ticker-change resolves to `weak_evidence_keep`
  not `high_confidence_archive`.

## 7. Sentinel (optional, this spec)

If the plan PR proceeds, a sentinel test in
`tests/test_symbol_history_substrate.py` SHALL red CI when
`issuer_securities` row count falls below a floor (e.g., 1,000) — the
fail mode is "someone truncated the substrate without re-running the
backfill". Floor is set after the first live populate so the number
is empirical, not aspirational. Not added in this spec.

## 8. Hard rules carried over

* No destructive cleanup of `fundamentals_quarterly` until rank-3
  substrate exists and classifier reframe is shipped + reviewed.
* No quarantine-only shortcut.
* No validator filter.
* No threshold loosening.
* No per-ticker API crawl.
* Bulk-first via R2/S3 archive precedent.
* Local-cache verification before SEC re-download
  (`tpcore.sec.submissions_bulk_reader.ensure_zip_cached` discipline).

## 9. Acceptance for *this* spec PR

* Doc lands at `docs/superpowers/specs/2026-06-02-symbol-history-evidence-backfill.md`.
* TODO.md row added under the Corporate-history enrichment epic.
* Bucket=1 dry-run verification (this turn) and manifest files
  retained for the operator in `data/`.
* No code under `scripts/`, `tpcore/`, or `platform/migrations/`
  touched in this PR.

## 10. Open questions for the plan PR

1. What R2 prefix and earliest date holds FMP daily ticker-list snapshots?
2. Does FMP expose a bulk symbol-change CSV or is it per-ticker?
3. What is the expected `issuer_securities` row count after first
   populate (sets the §7 floor)?
4. Are NASDAQ/NYSE daily issuer lists archived in R2, and at what
   depth?
5. How is `classification_id` minted for historical (pre-snapshot)
   ticker reassignments? Does `tpcore.ingestion.classification_keys`
   need a deterministic historical variant?
