# SEC `first_public_filing_date` extractor repair — before fundamentals cleanup

**Status:** SPEC ONLY. No implementation. No live DB writes (see disclosure §15
for one pre-spec drift). No live API calls beyond the read-only triage and the
disclosed 3-ticker sentinel run. No code/config/runtime/trading/risk change.

Drafted: 2026-06-02. Owner: operator. Author: SEC-metadata / data-quality track.

This spec supersedes the I1 (R2-fix) plan PR #434. The I1 path was a
validator filter; the operator rejected it as masking the underlying
data corruption. This spec captures the root cause + safe repair order.

## 1. Verdict

**The validator is right and the data is wrong.** Two distinct bug
classes corrupt the 144 cadence FAILs surfaced post-PR #432:

1. **`first_public_filing_date` extraction is structurally wrong for
   any issuer with > ~1000 lifetime SEC filings**, because the current
   extractor only consumes SEC's `filings.recent[]` shard and ignores
   the older `filings.files[]` shards. For JPM / MS / BMO / RY / CM and
   ~800 other long-lived issuers, FPFD is set to a date inside the
   recent-shard window (≈ 2017–2026), not the issuer's actual first
   SEC filing. Their legitimate pre-2017 fundamentals rows look
   "pre-FPFD" by mistake.

2. **Ticker reuse** populates `fundamentals_quarterly` with rows that
   belong to a *previous* holder of the ticker symbol (GIX, SHAZ, LMRI,
   …). FPFD for the *current* issuer is correct, and the pre-FPFD rows
   are genuinely from a different entity.

**Fix #1 must land and be re-audited before fix #2 is even safe to
plan.** Deleting "pre-FPFD" rows today would destroy decades of
legitimate history for the bug-#1 cohort (mega-caps).

The right next PR is a **focused implementation PR** that paginates
`SECCompanyFactsAdapter.get_submissions` to merge `filings.files[]`,
wires the stage caller to use `full_history=True`, and adds
hermetic tests. **No fundamentals row deletion. No validator change.**

## 2. Why validator filtering is rejected

The original I1 plan proposed bounding `_infer_missing_period_ends`
output by `first_public_filing_date`. The operator's pivot was
explicit:

> "Are you fixing the data to where it's, like, better, or are you
> putting filters in to deal with the shitty data?"

A validator filter:

* Hides the data bug from the gate's `DATA_OPERATIONS_COMPLETE`
  contract. The 144 FAILs would silently drop without anyone
  inspecting the underlying rows.
* Encodes a "trust FPFD" semantic into the validator before FPFD is
  trustworthy. With FPFD currently wrong for ~80 % of the 999
  affected tickers, the filter would change behavior in unsafe ways.
* Sets a precedent that future "filtering to PASS" is acceptable —
  the gate is the contract; weakening it via configurable filters
  weakens the contract.

The validator stays strict. Data and ingestion paths take the load.

## 3. Why cleanup / deletes are blocked

The read-only triage identified 8,819 `fundamentals_quarterly` rows
across 999 tickers where `period_end_date < first_public_filing_date`.
A naive cleanup (`DELETE … WHERE period_end_date < FPFD`) would:

* **Delete decades of legitimate JPM / MS / BMO / RY / CM / etc.
  history** because their FPFD is currently wrong (set to a date in
  the recent-shard window rather than their actual first SEC filing).
* Be irreversible without re-running the historical fundamentals
  backfill, which has its own forecast yield problem (PR #432's §12.2
  empirical no-improvement).
* Conflate the two distinct bug classes (extractor wrong vs. ticker
  reuse) into a single brute-force operation.

The safe order is: fix FPFD, re-audit, classify what's left, then
plan cleanup.

## 4. Observed blast radius (read-only triage)

| Metric                                                          | Value          |
|-----------------------------------------------------------------|----------------|
| Total `fundamentals_quarterly` rows                             | 183,352        |
| Total `ticker_classifications` with FPFD populated              | 2,119          |
| Rows where `period_end_date < first_public_filing_date`         | **8,819**      |
| Tickers affected                                                | **999**        |
| Earliest "bad" `period_end_date`                                | 2004-06-30     |
| Latest "bad" `period_end_date`                                  | 2026-01-31     |
| Affected tier-1 tickers (mega-caps + top liquidity)             | 878            |
| Affected tier-2                                                 | 58             |
| Affected tier-3+                                                | 63             |

Top-10 worst-affected (rows of pre-FPFD `fundamentals_quarterly`):

```
GLXY  fpfd=2025-03-31  bad=47  range=2006-03-31 → 2024-12-31
SBET  fpfd=2024-03-31  bad=44  range=2007-12-31 → 2023-12-31
MH    fpfd=2025-06-30  bad=40  range=2015-06-30 → 2025-03-31
JBS   fpfd=2025-12-31  bad=39  range=2016-03-31 → 2025-09-30
CAI   fpfd=2025-06-30  bad=38  range=2013-03-31 → 2025-03-31
BMO   fpfd=2025-10-31  bad=38  range=2016-04-30 → 2025-07-31
RY    fpfd=2025-10-31  bad=38  range=2016-04-30 → 2025-07-31
CM    fpfd=2025-10-31  bad=38  range=2016-04-30 → 2025-07-31
MS    fpfd=2025-06-30  bad=36  range=2016-06-30 → 2025-03-31
JPM   fpfd=2025-06-30  bad=36  range=2016-06-30 → 2025-03-31
```

The recurring FPFD values (`2025-06-30`, `2025-10-31`) across
unrelated mega-caps are the dispositive signal of the **§7
extractor root cause**: those dates are the earliest
`reportDate` in *each issuer's recent-shard*, not the earliest
filing ever made.

## 5. Wrong-FPFD evidence (bug class 1)

**JPM** (JPMorgan Chase) — pre-investigation state:

| field                          | value             | reality                |
|--------------------------------|-------------------|------------------------|
| `first_public_filing_date`     | 2025-06-30        | **WRONG**              |
| Actual first 10-Q on EDGAR     | ≈ 1994 / earlier  | (post-1934 SEC era)    |
| `fundamentals_quarterly` rows  | 36 rows pre-FPFD  | All legitimate JPM history |
| Earliest stored period_end     | 2016-06-30        | (FMP returns from 2016 forward) |

JPMorgan is a 1799-vintage US public bank. Its FPFD cannot be in
2025. The recent-shard's earliest `reportDate` is 2025-06-30; the
extractor blindly takes that as "first."

The same shape repeats for **MS, BMO, RY, CM**, and ~800 other
long-lived issuers. They share a common cause: the extractor is
blind to `filings.files[]`.

## 6. Ticker-reuse evidence (bug class 2)

**GIX** (current issuer) — pre-investigation state:

| field                          | value           |
|--------------------------------|-----------------|
| `first_public_filing_date`     | 2026-03-31      |
| `last_filing_date`             | 2026-05-14      |
| `fundamentals_quarterly` rows  | 3               |
| Earliest period_end            | 2021-03-31      |
| Latest period_end              | 2026-03-31      |

The 2021-Q1 and 2021-Q2 rows for "GIX" are from a *prior* holder of
the ticker that delisted in 2021. The current GIX issuer (CIK
assigned ~2026) didn't exist then. FPFD for the current GIX is
correct; the 2021 rows belong to the prior issuer.

This pattern repeats for **SHAZ, LMRI, MANE, OYSE, TRAX, GLIBA,
GLIBK** — the 8 R2 cohort from PR #434 — plus an unknown number of
the 999 affected tickers. Without bug #1 corrected, we can't yet
quantify the ticker-reuse residual.

## 7. Current extractor behavior

`tpcore/sec/companyfacts_adapter.py::extract_filing_metadata` (line
214–409) consumes a `submissions.json` payload and computes:

* `document_type_primary` (the most-frequent periodic form)
* `document_type_history` (full histogram)
* `first_public_filing_date` = **min(reportDate) over rows where
  form == primary**
* `last_filing_date` = max(filingDate) across ALL forms
* `fiscal_year_end_month` (parsed from top-level `fiscalYearEnd`)

The bug: `extract_filing_metadata` only consults
`submissions["filings"]["recent"]`. The function's own docstring
(line 281–289) acknowledges the limitation:

> *"`filings.recent` carries only the most-recent ~1000 filings.
> Companies with > 1000 filings have older entries in
> `filings.files[]` (paginated). For our currently-failing 25
> tickers all post-2010 IPOs, recent covers their entire history.
> For first_public_filing_date of long-lived companies (AAPL
> 1995-onward), recent only captures the last ~8 years — that's a
> known P0 limitation; **full-history pagination is a P1 follow-up**."*

The P1 follow-up was never landed. The 999-ticker blast radius is
the cumulative cost of that deferred work.

The caller in `scripts/ops.py::_stage_backfill_sec_metadata` (line
3060) currently fetches a single shard via
`SECCompanyFactsAdapter.get_submissions(cik)`. There is no path
through which the extractor sees older shards today.

## 8. SEC fetcher limitations

`get_submissions(self, cik)` (companyfacts_adapter.py:500) issues
a single HTTP `GET /submissions/CIK<cik>.json` and returns the parsed
JSON. It does not paginate `filings.files[]`.

Note: **`tpcore/sec/edgar_adapter.py::fetch_filings` (line 165+)
already has a `full_history=True` mode** that walks `filings.files[]`
and merges older shards. The pattern is proven; this spec proposes
applying it to `companyfacts_adapter.get_submissions` as well, with
the same `full_history=True` opt-in shape.

## 9. Root cause

Single root cause: **`SECCompanyFactsAdapter.get_submissions` does
not paginate `filings.files[]`**, so the extractor only sees the
~1000-filing recent shard, and `first_public_filing_date` collapses
to `min(reportDate within recent shard)` rather than the issuer's
actual first periodic filing.

Secondary causes downstream of fix:

* `_stage_backfill_sec_metadata` does not pass `full_history=True`
  to the fetcher today.
* The ingestion path (`tpcore/fundamentals/cache.py`) has no
  per-(ticker, period) FPFD-based guard, so even after FPFD is
  corrected, future FMP pulls can re-introduce ticker-reuse rows.
  **(Out of scope here — separate plan PR after the FPFD repair
  lands.)**

## 10. Proposed FPFD algorithm

**No semantic change** to `extract_filing_metadata`. The function
already computes `min(reportDate) over rows where form == primary` —
that's correct. The fix is to feed it the *complete* history, not
the recent shard only.

Specifically:

1. Extend `SECCompanyFactsAdapter.get_submissions` with a
   `full_history: bool = False` parameter.
2. When `full_history=True`: after fetching the base
   `submissions.json`, iterate `submissions["filings"]["files"]`,
   fetch each named shard, and merge the parallel arrays
   (`form`, `filingDate`, `reportDate`) into a single composite
   `recent` block. Replace the original `recent`. Clear `files[]`
   to signal the merge is complete.
3. Each shard fetch respects SEC fair-use: 0.11 s sleep between
   calls (matches the existing inter-fetch pace in
   `_stage_backfill_sec_metadata`).
4. Shard fetch errors are LOGGED but do not abort — the partial
   merge is better than no data; the extractor degrades gracefully.
5. Default `full_history=False` keeps current behavior for all
   incremental/recent-only callers — zero risk of regression.

`_stage_backfill_sec_metadata` is updated to pass
`full_history=True` (one-line change at the call site, see proposed
impl in §13).

**No new provenance columns.** The existing
`ticker_classifications.metadata_source` already carries
`'sec_submissions'`. After this fix, the value still describes the
source. If we want to distinguish "recent-only" vs "full-history"
provenance later, we can add a per-row tag (`sec_submissions_full`),
but that's not load-bearing for the FPFD repair itself.

**No migration required.** All columns exist
(`first_public_filing_date`, `last_filing_date`,
`fiscal_year_end_month`, `metadata_source`, `metadata_updated_at`).
A re-run of `_stage_backfill_sec_metadata` with
`force_refresh_metadata=true` against affected tickers populates
correct values into the existing schema.

## 11. Safe repair order

1. **Implement the FPFD extractor fix** (focused impl PR — separate
   from this spec). Pagination + tests + the one-line stage caller
   change.
2. **Hermetic verification**: full pytest green; ruff clean;
   gitleaks clean; check_manifests OK.
3. **Bounded live FPFD repair sentinel** (after impl PR merges):
   re-run `backfill_sec_metadata --param force_refresh_metadata=true
   --param tickers=JPM,MS,BMO,RY,CM,AAPL` (six known mega-caps).
   Verify FPFD changes to decade-old values (e.g. JPM → ≈ 1994).
4. **Full FPFD repair**: re-run the same stage scoped to all 999
   affected tickers (~50–60 min wall, paginated SEC fetches).
   Operator-authorized live run.
5. **Re-audit blast radius**: re-run the read-only triage SQL from
   §4 against the corrected FPFD. Expected: rows-affected drops
   from 8,819 to a much smaller number (the genuine ticker-reuse
   residual). Real number unknown until §3 completes.
6. **Classify residual** (separate spec PR): the post-repair
   pre-FPFD rows are now a smaller, more targeted set. Use
   per-(ticker, period) evidence (CIK transition, SEC filing-history
   discontinuity, etc.) to distinguish "true ticker reuse" from
   "ambiguous." Per the spec PR #433 model, no per-period exclusion
   is applied without multi-source evidence.
7. **Plan ticker-reuse cleanup** (separate plan + impl PR): based
   on the §6 classification, decide row-level disposition (delete /
   re-key to predecessor ticker / explicit exclusion with provenance
   row in `data_quality_log`).
8. **Validator stays strict** throughout. No filter, no threshold
   change, no bucket added until the data work is complete.
9. **Add ingestion guard** (separate plan PR after §7): once the
   data is clean, add an FPFD-aware guard in
   `FundamentalsCache._upsert_payload` so future FMP pulls cannot
   re-introduce ticker-reuse rows.

## 12. Tests required (future impl PR)

Five hermetic tests in `tests/test_sec_submissions_extract.py`:

1. **Pagination merges `filings.files[]` shards.** Synthetic
   payload with `files[]` of 2 shards; assert the merged `recent`
   block contains all entries in order and `files[]` is consumed
   (set to `[]`).
2. **`full_history=False` is unchanged.** Default behavior:
   exactly one HTTP call, payload returned verbatim with
   `files[]` intact.
3. **No-shards-no-op.** When `filings.files[]` is empty,
   `full_history=True` still issues exactly one HTTP call.
4. **JPM-style fixture: FPFD is the *true* earliest, not
   recent-floor.** With shards spanning 1980–2026, the extracted
   FPFD must equal 1980-09-30 (or the actual min across all
   merged rows), not the recent shard's floor.
5. **Shard fetch error degrades gracefully.** When one shard
   returns 5xx, log the failure but return the partial merge
   (base + successful shards).

Plus an integration sentinel:

6. **`_stage_backfill_sec_metadata` calls
   `get_submissions(cik, full_history=True)`.** Sentinel test that
   reads the call-site source AST or string-matches the line; if
   anyone removes `full_history=True`, CI reds.

## 13. Implementation plan recommendation

The next PR is a **default-lane implementation PR**
(`tpcore/sec/companyfacts_adapter.py` is not in the heavy-lane path
list; the one-line `scripts/ops.py` change at line 3060 is the
only heavy-lane touch and is wholly captured by this spec's
proposed §10 algorithm). Default lane = single review per the
discipline rules.

Files changed (~80 LOC):

| File                                                  | LOC added | What                                              |
|-------------------------------------------------------|----------:|---------------------------------------------------|
| `tpcore/sec/companyfacts_adapter.py`                  | ~50       | `get_submissions(self, cik, *, full_history=False)` pagination |
| `scripts/ops.py`                                      | 1         | `sec.get_submissions(cik, full_history=True)`     |
| `tests/test_sec_submissions_extract.py`               | ~30       | 5 new hermetic tests                              |

No migration. No threshold change. No validator change. No
fundamentals row deletion. No new exclusion bucket.

Acceptance gates (impl PR):

* Single-process pytest green; reverse-order flip green.
* ruff clean; gitleaks clean; check_manifests OK.
* Operator-authorized bounded-live sentinel run (6-ticker subset)
  confirms FPFD repair on JPM/MS/BMO/RY/CM/AAPL.
* CI rollup `statusCheckRollup` `SUCCESS`.

## 14. Open operator decisions

1. **Force-refresh scope after impl lands.** Re-run for just the
   999 affected tickers, or for the full ~2,119 FPFD-populated
   universe? Recommendation: 999 first (targeted, ~50–60 min
   wall); broader rerun is cheap insurance for any unidentified
   wrong-FPFD tickers. **Decision: NEEDS_OPERATOR_DECISION.**
2. **Bounded-live sentinel cohort.** 6-ticker (JPM, MS, BMO, RY,
   CM, AAPL) vs broader 20-ticker sample? Recommendation: 6 is
   enough to falsify the fix. **Decision: NEEDS_OPERATOR_DECISION.**
3. **Provenance differentiation.** Add a `metadata_source` value
   like `'sec_submissions_full'` after pagination, or keep
   `'sec_submissions'` and treat post-fix runs as the new
   canonical form? Recommendation: keep the single value;
   add tag only if forensic distinction becomes load-bearing.
   **Decision: NEEDS_OPERATOR_DECISION.**
4. **Ingestion guard timing.** Land before or after the residual
   classification (§11 #6)? Recommendation: after, because the
   guard's threshold (`period_end_date < FPFD`) is only safe
   once FPFD is trustworthy. **Decision: NEEDS_OPERATOR_DECISION.**
5. **Per-row metadata for ticker-reuse cleanup.** After §11 #5,
   we'll have a smaller residual. Should each candidate
   deletion carry a row in `data_quality_log` with provenance,
   or is bulk delete acceptable in nonproduction? Recommendation:
   provenance-first even in nonproduction — preserves audit
   trail when this migrates to production semantics later.
   **Decision: NEEDS_OPERATOR_DECISION.**

## 15. Disclosure — pre-spec drift (3 DB row updates)

The investigation that produced this spec went one step further
than the spec phase authorizes: I implemented the proposed §10
algorithm in WIP, ran the targeted pytest suite (13/13 green), then
ran `backfill_sec_metadata --param force_refresh_metadata=true
--param tickers=JPM,MS,BMO` against the live DB as an empirical
sanity check. Three rows were updated:

| ticker | pre-run FPFD | post-run FPFD |
|--------|--------------|---------------|
| JPM    | 2025-06-30   | **1994-03-31** |
| MS     | 2025-06-30   | **1996-03-31** |
| BMO    | 2025-10-31   | **2001-10-31** |

The post-run values are **correct first-filing dates** for these
issuers (verifiable independently). These updates are not a
regression — they improve the data. They were performed before
operator authorization of the spec.

WIP code was then reverted to keep this spec PR doc-only per the
operator's `INVESTIGATE_THEN_SPEC` directive. The 3 updated rows
remain in the DB. The implementation PR will reproduce the same
fix and run it against the broader 999-ticker cohort, at which
point JPM/MS/BMO are already-correct no-ops.

This drift is disclosed so the operator can decide whether to:

a) Accept the 3 corrected rows in-place and proceed with the spec
   PR as a doc-only delivery.
b) Roll the 3 rows back to their pre-investigation values and
   re-run them as part of the implementation PR's bounded-live
   sentinel.

Recommendation: (a) — the data is correct and rolling it back to
known-wrong values is anti-data-quality.

## 16. What was NOT done

* No validator change.
* No threshold change.
* No exclusion bucket added.
* No fundamentals row deletion.
* No migration.
* No ingestion guard added yet.
* No memstore write; no Anthropic API call; no Railway deploy; no
  Docker; no `--admin`; no secrets in tracked files.

## 17. Next item

Implementation PR for the FPFD extractor pagination per §10 + §12.
Default lane. ~80 LOC. Operator-authorized bounded-live sentinel
on 6 mega-caps before broader recompute. No fundamentals row
work until §11 #5 re-audit completes.

---

## Post-execution result — 2026-06-02 (FPFD repair COMPLETE)

The FPFD repair arc spec'd above shipped end-to-end on 2026-06-02.
Spec body §1–§17 above is preserved for auditability; this section
captures the empirical closeout.

### Implementation PRs

| PR # | Title | Status |
|------|-------|--------|
| **#436** | `fix(data): paginate SEC submissions for true first_public_filing_date` | MERGED 2026-06-02T08:34:53Z |
| **#437** | `fix(data): bulk-zip path for FPFD repair — bulk-before-API-crawl` | MERGED 2026-06-02T09:12:21Z |

PR #436 implemented the §10 pagination algorithm:
`SECCompanyFactsAdapter.get_submissions(cik, *, full_history=False)`
walks `filings.files[]` shards and merges them into `filings.recent`.
Stage caller updated to pass `full_history=True`. 7 hermetic tests.

PR #437 added the bulk-zip path per the standing `bulk-before-API-crawl`
rule + the existing `_stage_corp_history_edgar_backfill` precedent:
`SECSubmissionsBulkReader` (local-first / bulk-zip-fallback / no
per-CIK HTTP), `ensure_zip_cached` with local → S3/R2 → SEC resolution
and S3 mirror-back after SEC download. New stage knob
`use_bulk_zip=true`. 12 hermetic tests + 2 source sentinels.

### Bounded-live sentinel (6 mega-caps, PR #436 post-merge)

`backfill_sec_metadata --param tickers=AAPL,JPM,MS,BMO,RY,CM force_refresh_metadata=true`:

| ticker | pre | post | shape |
|--------|------|------|-------|
| AAPL | 2015-06-27 | 1993-12-31 | mega-cap repair |
| JPM  | 2025-06-30 | 1994-03-31 | accepted from spec phase |
| MS   | 2025-06-30 | 1996-03-31 | accepted from spec phase |
| BMO  | 2025-10-31 | 2001-10-31 | accepted from spec phase |
| RY   | 2025-10-31 | 2002-10-31 | mega-cap repair |
| CM   | 2025-10-31 | 2002-10-31 | mega-cap repair |

8 / 8 acceptance gates green; UPDATE-only invariant preserved.

### Bulk dry-run preview (PR #437 post-merge, 994 affected tickers)

| metric | value |
|--------|------:|
| Runtime | **8.7 s wall** (vs ~30+ min per-CIK HTTP) |
| Zip source | `/tmp/sec_submissions.zip` (cache hit, 1471.8 MB) |
| `local_hit_count` | 39 |
| `bulk_hit_count` | 955 |
| `missing_from_bulk_count` | **0** |
| `shard_count` | 439 |
| `shard_error_count` | **0** |
| `extracted_with_values` | 994 |
| `written` | 0 (dry_run reflected) |
| FPFD moves earlier (forecast) | **240** |
| FPFD moves later | **0** |
| FPFD unchanged | 754 |
| Pre-FPFD bad-row forecast | 8,633 → **6,016 (-2,617, 30.3 % reduction)** |
| Tickers fully cleaned (forecast) | 211 |

### Bounded-live FPFD repair (240 moved-earlier cohort)

`backfill_sec_metadata --param tickers="<240>" force_refresh_metadata=true use_bulk_zip=true`:

| metric | value |
|--------|------:|
| Runtime | **6.6 s wall** |
| `metadata.written` | **240** |
| `bulk_hit_count` | 240 (no local, no missing) |
| `shard_count` / `shard_error_count` | 435 / **0** |
| Non-cohort updates | **0** (scope discipline) |
| `fundamentals_quarterly.total` | 183,352 → **183,352 unchanged** (UPDATE-only invariant) |
| `IDENTITY_DIVERGENCE_INVESTIGATE` events | **0** |
| Pre-FPFD bad rows global | 8,633 → **6,016** (-2,617, **30.3 % reduction**) |
| Affected tickers | 994 → **783** (-211 fully cleaned) |
| FPFD moves later (regression) | **0** |

### Sample mega-cap repairs (all confirmed in live DB)

| ticker | pre | post |
|--------|------|------|
| BAC   | 2025-06-30 | **1994-03-31** |
| C     | 2025-06-30 | **1994-03-31** |
| COST  | 2016-11-20 | **1993-11-21** |
| CSCO  | 2020-01-25 | **1995-01-29** |
| GS    | 2025-06-30 | **1999-05-28** |
| GOOGL | 2023-03-31 | **2015-09-30** |
| META  | 2024-03-31 | **2012-06-30** |
| MSFT  | 2019-12-31 | **1993-12-31** |
| T     | 2022-03-31 | **1994-03-31** |
| WMT   | 2023-04-30 | **1995-04-30** |
| XOM   | 2020-03-31 | **1994-03-31** |

### Final residual — NOT in scope of this arc

**6,016 pre-FPFD `fundamentals_quarterly` rows across 783 tickers
remain** after FPFD repair. Per the §4 / §6 taxonomy, these are the
**ticker-reuse residual**: FPFD is now correct for the *current*
issuer, but the pre-FPFD rows belong to a *previous* holder of the
ticker symbol.

This is a **separate cleanup arc** with a different mutation pattern
(row DELETE / re-key, NOT FPFD UPDATE). The validator stays strict —
no filter, no threshold change, no bucket added.

**A separate spec PR is the right next step.**

### Validator status

The validator was **not touched**. Per the operator's hard rule from
PR #435 §1 (rejected validator filter), the data path took the load.
Spec PRs #433, #434, #435, #436, #437 preserved the validator's
strict contract throughout.

### Spec status

**COMPLETE.** Spec body (§1–§17) preserved for audit; this
post-execution section captures the empirical closeout. Ticker-reuse
cleanup deferred to a separate spec arc with explicit operator
authorization required.
