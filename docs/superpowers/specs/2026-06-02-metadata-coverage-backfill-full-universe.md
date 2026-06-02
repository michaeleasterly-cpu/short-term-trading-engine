# Metadata-coverage backfill — full active universe

**Status:** SPEC ONLY. No implementation. No live DB writes. No live API calls.
Heavy-lane step 3 (spec PR) per `docs/DEV_PIPELINE_STANDARD.md` §0/§1 and
`.claude/rules/heavy-lane.md`. Plan + implementation are separate downstream
PRs, only if the verdict below requires them.

Drafted: 2026-06-02.
Author: data-operations track.
Owner: operator (decision gates marked below).

## 1. Verdict

**Existing code is sufficient.** No new stage, no migration, no validator
change is required for the operator to attempt the full-active-universe
metadata-coverage backfill. The existing operator-on-demand stage
`scripts/ops.py --stage backfill_sec_metadata` (live at commit 25a2906)
already drives both legs the gate needs: CIK resolution (do_cik) and SEC
submissions evidence extraction (do_metadata). The default-scope query
already walks every row in `platform.ticker_classifications` where the
required fields are NULL.

**Recommended operator next step is therefore a live operator-run sequence
(dry-run → bounded live → full live), not an implementation PR.** A small
optional follow-up — a new `tradeable_only=true` scope knob bounding the
stage to `liquidity_tiers.tier <= 2` (the gate's denominator) — would
improve efficiency but is **not** required to advance coverage. That knob,
if approved, is a separate heavy-lane plan + implementation arc; this spec
does not authorize it.

**Boundary with P1b.** P1b (PRs #423/#424/#425/#426/#427) addressed the
**unresolved-CIK long-tail** sub-bucket (tickers in `ticker_classifications`
with `cik IS NULL AND country IS NULL`, ~1,630 rows). The live-smoke of P1b
returned 0/100 FMP resolutions; the optional `fmp_max_unresolved=0` full
pass is explicitly blocked pending **P1c source triage**. The metadata-coverage
backfill spec'd here is a **different bucket**: it backfills `cik` AND
`sec_document_type_primary` for **all tradeable (liquidity_tiers tier ≤ 2)
tickers where those columns are NULL**, not the unresolved-tail residue.
The two arcs are operationally and source-authority-wise independent.

## 2. Problem statement

`tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
emits a synthetic structural sentinel
`FailureDetail(ticker="<metadata_coverage>", reason="metadata_coverage_insufficient", …)`
whenever

```
excluded_metadata_required / (evaluated_routed + excluded_metadata_required)
    > METADATA_COVERAGE_FAIL_THRESHOLD   # = 0.25 (25%)
```

— i.e. whenever **more than 25 %** of the "routed-eligible OR
metadata-required" sub-universe (tier ≤ 2; non-dark; non-lifecycle-terminated;
≥ 2 filings) lacks a populated `sec_document_type_primary`. Per the file's
in-source docstring (lines 60–63):

> At commit 2eca8c7 metadata coverage was 362 / 13,840 = **2.6 %** — far
> below the 25 % threshold. **`DATA_OPERATIONS_COMPLETE` remains blocked
> post-P1 until backfill coverage reaches > 75 % of the routed-eligible
> universe.** This is the correct outcome, not a regression.

`DATA_OPERATIONS_COMPLETE` is the predicate the trading lane gates on. The
metadata-coverage sentinel therefore directly blocks the global
"100% data or don't trade" invariant. This spec lays out the operator-run
sequence that should clear it.

## 3. Evidence from repository

Files inspected (read-only):

* `scripts/ops.py` — `_stage_backfill_sec_metadata` at line 2604 (the
  registered stage; `_STAGE_SPECS` registration at line 9241). Knobs
  enumerated in §6.
* `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
  — gate semantics (METADATA_COVERAGE_FAIL_THRESHOLD = 0.25; sentinel
  emission at line 460–476; `_FILING_DATES_SQL` line 180 confirms the
  `JOIN liquidity_tiers WHERE tier <= 2` denominator).
* `tests/test_backfill_sec_metadata_stage.py` — coverage_before /
  coverage_after payload contract (test_009).
* `tests/test_p1b_empirical_finding_documented.py` (PR #427) — the P1b
  empirical-finding sentinels (boundary between this spec and P1b).
* `TODO.md` — the open "Metadata coverage gate (STILL OPEN — P1b did NOT
  move it)" item (line 150–155).
* `docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md` and
  `docs/superpowers/plans/2026-06-01-p1b-cik-long-tail-backfill-plan.md`
  — P1b authority order, FMP fallback design, live-smoke result.
* `platform/migrations/` — migration `20260530_0200` adds the metadata
  columns the stage populates (`cik`, `sec_document_type_primary`,
  `first_public_filing_date`, `last_filing_date`, `fiscal_year_end_month`,
  `metadata_source`, `cik_source`, `metadata_updated_at`).

## 4. P1b boundary (explicit, non-overlapping)

| Dimension                  | P1b (PRs #423–#427)                                                                 | This spec (metadata coverage backfill)                                                     |
|---------------------------|-------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Target bucket             | `cik IS NULL AND country IS NULL` long-tail (~1,419 rows post-SEC; ~1,630 pre-SEC) | Full active universe — `liquidity_tiers.tier <= 2` tickers where `cik IS NULL OR sec_document_type_primary IS NULL` |
| Authority for CIK         | FMP `/stable/profile` (lower than SEC; never overwrites a non-NULL CIK)             | SEC `data.sec.gov/files/company_tickers` (primary; canonical for US issuers)               |
| Authority for metadata    | n/a (P1b is CIK-only)                                                               | SEC `data.sec.gov/submissions/CIK<cik>.json` (do_metadata leg)                             |
| Live result               | 0 / 100 sampled resolutions (PR #427); full pass BLOCKED pending P1c triage         | Not yet run                                                                                |
| Closes the coverage gate? | No (P1b did not move `excluded_metadata_required`)                                  | Yes (target: drive `coverage_ratio` from current ~2.6 % to < 25 %)                         |
| `do_fmp_fallback`         | The relevant knob — opt-in only                                                     | OFF / not used (this arc is SEC-primary; FMP fallback is a P1b/P1c concern)                |
| Implementation status     | Done (PRs #423/#424/#425/#426)                                                      | **No new implementation expected** (verdict §1)                                            |

The two arcs must not be conflated. Specifically, `fmp_max_unresolved=0`
remains blocked (PR #427); this spec does **not** change that.

## 5. Current coverage gate (read-only summary)

* **Predicate (numeric):**
  `excluded_metadata_required ÷ (evaluated_routed + excluded_metadata_required) ≤ 0.25`
  → coverage of `sec_document_type_primary` must be **≥ 75 %** of
  (routed + metadata-required) tickers.
* **Denominator universe** (per `_FILING_DATES_SQL` lines 180–197):
  rows in `platform.ticker_classifications` joined to
  `platform.liquidity_tiers` with `tier <= 2`. This is the **tradeable**
  universe — NOT every row in `ticker_classifications`.
* **Excluded buckets that do NOT count toward the denominator:**
  `excluded_dark`, `excluded_confirmed_data_gap` (< 2 filings + past
  new-listing grace), `excluded_lifecycle_terminated` (Form 25 / Form 15
  evidence), `excluded_other_form` (e.g. `N-1A` closed-end funds).
* **Field that resolves the sentinel:** `sec_document_type_primary`.
  Setting `cik` alone does NOT clear it — the `do_metadata` leg must
  succeed to extract the primary form from SEC submissions.
* **Per docstring at commit 2eca8c7:** 362 / 13,840 ≈ 2.6 %. The current
  value as of 2026-06-02 is NEEDS_REPO_VERIFICATION — measure as part of
  the dry-run in §10.

## 6. Current `backfill_sec_metadata` behavior (read-only inventory)

Stage signature: `_stage_backfill_sec_metadata(pool, cfg)` at
`scripts/ops.py:2604`.

Knobs (per the in-source docstring, lines 2635–2693):

| Knob                       | Default | Effect on this spec's job                                                                                       |
|----------------------------|---------|------------------------------------------------------------------------------------------------------------------|
| `dry_run`                  | `True`  | Hard rule: backfills are dry by default. Operator must pass `dry_run=false` to write.                            |
| `do_cik`                   | `True`  | Required ON for this job (long-tail Δ since previous SEC map refresh).                                           |
| `do_metadata`              | `True`  | Required ON. This is the leg that populates `sec_document_type_primary`.                                         |
| `do_fmp_fallback`          | `False` | **MUST stay False** for this spec. P1b/P1c concern.                                                              |
| `failing_only`             | `False` | **Wrong knob for this job.** Routes to `ev.gaps` — cadence-failed routed tickers — NOT to METADATA_REQUIRED.    |
| `no_cik_country_null`      | `False` | P1b sub-bucket. Off for this job.                                                                                |
| `tickers`                  | none    | Explicit comma-list. Useful for bounded live (§11).                                                              |
| `max_tickers`              | none    | Hard cap. Useful for dry-run sizing + bounded live.                                                              |
| `force_refresh_metadata`   | `False` | Re-runs metadata extraction even where already populated. **Stays False** here — coverage backfill targets NULLs only. |

**Default scope** when no scope knob is passed
(`scripts/ops.py:2776–2785`):

```sql
SELECT ticker FROM platform.ticker_classifications
 WHERE cik IS NULL
    OR sec_document_type_primary IS NULL
 ORDER BY ticker
```

This walks the **whole 13,840-row table** — superset of the
gate's tier ≤ 2 denominator. It is sufficient (correctness-wise) for
this job: the gate will see the writes anyway, and tier-3+ writes are
free benefit (cheap insurance against future tier promotions). It is
**not** efficient (SEC fair-use sleep of 0.11 s × ~13 500 candidates ≈
25 min per leg, ~50 min for both legs). The §11 bounded-live → full-live
sequence absorbs this cost.

**SEC rate-limit floor:** 0.11 s between fetches (per stage docstring),
yielding ≤ 9 req/s — comfortably under SEC's 10 req/s fair-use cap.

## 7. Target universe

**For the backfill writes:** every row in `platform.ticker_classifications`
where `cik IS NULL OR sec_document_type_primary IS NULL`. This is the
stage's default scope; no scope knob is needed.

**For the gate measurement (coverage_after evaluation):** rows joined
to `platform.liquidity_tiers` with `tier <= 2`. The gate predicate is
measured against this filtered universe, NOT against the full
`ticker_classifications` row count.

The discrepancy is by design. The stage writes broadly; the gate measures
narrowly. The §10 dry-run validates both ratios.

## 8. Target metadata fields

The single field the structural sentinel reads is **`sec_document_type_primary`**.
Setting `cik` is a prerequisite (the do_metadata leg keys on `cik`), but
the gate predicate is `sec_document_type_primary` NULL/non-NULL only.

Adjacent fields populated as side effect by the same SEC submissions
fetch (not gate-relevant, but useful for the cadence-routing logic and
for the operator-facing audit trail):

* `first_public_filing_date`
* `last_filing_date`
* `fiscal_year_end_month`
* `metadata_source` (`'sec_submissions'`)
* `metadata_updated_at` (NOW())
* `cik_source` (`'sec_ticker_map'` for the do_cik leg)

The migration that introduced these columns is `20260530_0200`.

## 9. Authority order

For each field, the spec-authoritative source is:

| Field                          | Primary source                                          | Adapter                                                | Fallback (NOT used here) |
|--------------------------------|---------------------------------------------------------|--------------------------------------------------------|--------------------------|
| `cik`                          | `data.sec.gov/files/company_tickers`                    | inline in `_stage_backfill_sec_metadata` (do_cik leg)  | FMP `/stable/profile` — P1b only |
| `sec_document_type_primary`    | `data.sec.gov/submissions/CIK<cik>.json` (primary form) | inline (do_metadata leg)                               | none                     |
| `first_public_filing_date`     | submissions.json                                        | inline                                                 | none                     |
| `last_filing_date`             | submissions.json                                        | inline                                                 | none                     |
| `fiscal_year_end_month`        | submissions.json                                        | inline                                                 | none                     |

SEC is the only authority used in this arc. FMP is OFF (do_fmp_fallback=false).

## 10. Dry-run behavior (operator step 1)

Goal: print plan + coverage delta forecast without touching the DB.
Validates the full-universe scope size and forecast the
`coverage_ratio` shift before any live SEC traffic.

**Command:**

```bash
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=true \
    --param do_cik=true \
    --param do_metadata=true
```

(No `failing_only`. No `no_cik_country_null`. No `do_fmp_fallback`.
No `max_tickers`. Default scope applies → full
`ticker_classifications` cik-or-document-NULL walk.)

**Expected payload fields to inspect:**

* `scope_size` — total candidates the stage would attempt.
* `coverage_before` (snapshot at start) — already includes `has_cik`,
  `has_sec_document_type_primary`, `has_first_public_filing_date`,
  `has_last_filing_date`, `has_fiscal_year_end_month`,
  `has_metadata_source`, `has_cik_source`, `total`.
* `coverage_after` — in dry-run this equals `coverage_before` (no writes).
* `cik.{candidates, resolved, unresolved}` — forecast of the do_cik leg.
* `metadata.{candidates, fetched, submissions_404, extracted_with_values}`
  — forecast of the do_metadata leg.
* `dry_run: true` — sanity assertion.

**Acceptance gate to proceed to §11:**

1. `scope_size ≥ 1` (otherwise: nothing to do; gate is already at
   target — NEEDS_REPO_VERIFICATION).
2. `coverage_before["has_sec_document_type_primary"] / coverage_before["total"]`
   is well below 0.75 (confirms the gate is still the active blocker).
3. No exceptions, no `submissions_404` rate > 1 % on a sampled subset
   (next bullet).
4. **Important:** the dry-run currently produces a stage-wide forecast,
   not a sampled-fetch forecast. If the operator wants a forecast of
   actual SEC `submissions_404` / extraction yield before the full
   live run, run a **bounded dry-run** with `max_tickers=200` first
   AND with the do_metadata leg making the actual HTTP fetches in
   dry-run mode (NEEDS_REPO_VERIFICATION — confirm whether the stage's
   dry_run gates only the DB writes or also the HTTP fetches). If the
   stage skips HTTP in dry-run, the bounded-live run in §11 IS the
   first real forecast.

## 11. Bounded live behavior (operator step 2)

Goal: validate end-to-end writes + measure `coverage_after` shift on a
representative slice before committing to the full run.

**Command:**

```bash
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=false \
    --param do_cik=true \
    --param do_metadata=true \
    --param max_tickers=200
```

Wall-clock estimate: ~200 × 2 × 0.11 s ≈ 45 s for the SEC fetches plus
DB-write overhead. Realistic upper bound ~2 min.

**Acceptance gate to proceed to §12:**

1. `dry_run=false` is reflected in the output payload.
2. `coverage_after["has_sec_document_type_primary"]
    > coverage_before["has_sec_document_type_primary"]` — the gate field
   moved up.
3. `cik.skipped_already_set > 0` if any of the 200 sampled rows had a
   pre-existing non-NULL CIK — operator-provenance preservation invariant
   from PR #425.
4. No new rows inserted (UPDATE-only — `scope_size == cik.candidates +
   metadata.candidates` accounting matches).
5. **No** `IDENTITY_DIVERGENCE_INVESTIGATE` events in
   `platform.application_log` — that surface belongs to the P1b FMP leg
   only.
6. `metadata.submissions_404` rate < 1 % (operator judgment — many
   tier-3 rows may legitimately 404 if delisted/foreign; if rate > 1 %
   on tier ≤ 2 sample, STOP and triage before §12).

If any guard fires, **stop**. Do not proceed to §12 without operator
authorization.

## 12. Full live behavior (operator step 3)

Goal: drive coverage to ≥ 75 % of the gate's denominator.

**Command:**

```bash
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=false \
    --param do_cik=true \
    --param do_metadata=true
```

(No `max_tickers` cap — walks the full default scope.)

Wall-clock estimate: ~13 500 candidates × 2 legs × 0.11 s ≈ 50 min if
both legs hit SEC for every row. In practice the do_cik leg shares the
single ticker→CIK map fetch, so the dominant cost is the do_metadata
leg fetching one submissions JSON per CIK ≈ ~25–30 min.

**Acceptance gate (gate-clearing):**

1. After completion, re-run the data-acceptance suite (operator command
   per `scripts/run_data_operations.sh` — separate from this stage).
   The `fundamentals_quarterly_completeness` check must NOT emit
   `FailureDetail(ticker="<metadata_coverage>", reason="metadata_coverage_insufficient", …)`.
2. The stage's `coverage_after["has_sec_document_type_primary"] /
    coverage_after["total"]` should rise materially (target ≥ 0.75 on
   the tier ≤ 2 sub-universe; broader denominator is operator-judgment).
3. `DATA_OPERATIONS_COMPLETE` may still be blocked by other checks
   (see §15). Successful metadata coverage clearance is necessary but
   not sufficient for `DATA_OPERATIONS_COMPLETE` to flip green.

## 13. Safety guards

The stage's existing in-code guards (no change required by this spec):

1. **CIK overwrite guard:** the do_cik leg's WHERE clause filters to
   `cik IS NULL`; never overwrites operator-set CIKs. The do_fmp_fallback
   sub-leg (which is OFF here) has the same guard.
2. **Lifetime-ended guard:** P1b PR #425 added a `lifetime_end IS NULL`
   filter to the FMP sub-leg. The SEC do_cik leg's behavior on
   lifetime-ended rows is NEEDS_REPO_VERIFICATION (read `scripts/ops.py`
   line range 2837 onward in a follow-up plan PR if §11 sees writes to
   lifetime-ended rows).
3. **Force-refresh guard:** `force_refresh_metadata` defaults False;
   this spec requires it stays False. Backfilling NULL columns only;
   never overwriting populated metadata.
4. **No-country-writeback guard:** P1b moved the `country` writeback OFF
   for the FMP leg. SEC do_metadata does NOT write `country` (it's not
   in the SEC submissions schema). Verified by inspection.
5. **SEC fair-use sleep:** 0.11 s between fetches preserves the 10 req/s
   cap. Stage docstring asserts this; not changed.
6. **dry_run-by-default:** preserved. Operator must pass
   `dry_run=false` explicitly.
7. **Application-log surface:** SEC do_cik / do_metadata legs do NOT
   emit `IDENTITY_DIVERGENCE_INVESTIGATE` (that's the P1b FMP-fallback
   surface only). If any divergence rows appear during §11/§12,
   something has been wired incorrectly — STOP and triage.

## 14. Expected output counters

Reference the existing `coverage_before` / `coverage_after` payload contract
asserted by `tests/test_backfill_sec_metadata_stage.py::test_009_coverage_report_emitted_when_scope_empty`:

```json
{
  "scope_size": int,
  "cik": {
    "candidates": int, "resolved": int, "unresolved": int,
    "skipped_already_set": int, "written": int
  },
  "metadata": {
    "candidates": int, "fetched": int, "submissions_404": int,
    "extracted_with_values": int, "written": int, "failures": [...]
  },
  "coverage_before": {
    "total": int, "has_cik": int, "has_sec_document_type_primary": int,
    "has_first_public_filing_date": int, "has_last_filing_date": int,
    "has_fiscal_year_end_month": int, "has_metadata_source": int,
    "has_cik_source": int
  },
  "coverage_after": { /* same shape */ },
  "dry_run": bool
}
```

The §11/§12 acceptance gates read these counters directly.

## 15. Validation and acceptance criteria (whole arc)

The whole arc is "operator runs the §10 → §11 → §12 sequence and verifies
the gate clears". Concrete checkpoints:

| Step | Command                                       | Acceptance signal                                                                                          |
|------|-----------------------------------------------|------------------------------------------------------------------------------------------------------------|
| §10  | dry_run=true (full scope)                     | `scope_size ≥ 1`; no exceptions; coverage forecast plausible                                               |
| §11  | dry_run=false max_tickers=200                 | `coverage_after.has_sec_document_type_primary > coverage_before`; no DIVERGENCE events; 404 rate < 1 %     |
| §12  | dry_run=false (no cap)                        | Gate field reaches ≥ 75 % of tier ≤ 2 denominator                                                          |
| Post | re-run `fundamentals_quarterly_completeness`  | No `metadata_coverage_insufficient` synthetic FailureDetail                                                |
| Post | re-run full data-acceptance suite             | Whichever other checks remain blocking surface for next-arc triage                                          |

The arc is complete when the metadata-coverage structural sentinel stops
firing. `DATA_OPERATIONS_COMPLETE` clearance is downstream of this and
NOT in this spec's scope.

## 16. Failure modes

What can go wrong, and what to do:

| Failure mode                                                                  | Diagnostic signal                                                          | Response                                                                                                                |
|-------------------------------------------------------------------------------|----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------|
| SEC `submissions.json` 404 rate high                                          | `metadata.submissions_404 / metadata.candidates > 0.05`                    | STOP. Likely the CIK column has stale / wrong CIKs OR a swath of rows are non-US issuers SEC doesn't host. Triage scope. |
| SEC rate-limit hit (HTTP 429)                                                 | Stage error / structlog `429` events                                       | STOP. Raise the SEC fair-use sleep (e.g. 0.15 s) via a follow-up plan PR; do not bypass.                                |
| `coverage_after.has_sec_document_type_primary` does NOT rise after §12        | Counter unchanged                                                          | The metadata leg is silently no-op. Investigate the parser; do NOT re-run with `force_refresh_metadata=true`.            |
| Sentinel still fires after §12                                                | `fundamentals_quarterly_completeness` still emits `metadata_coverage_insufficient` | The remaining gap is in the routed-eligible bucket whose CIK could not be resolved (P1c source territory).         |
| Stage writes to lifetime-ended row                                            | Row in `ticker_classifications` with `lifetime_end IS NOT NULL` mutated    | STOP. Add a `lifetime_end IS NULL` guard to the SEC legs in a follow-up plan PR (mirrors the P1b FMP guard).            |
| Stage emits `IDENTITY_DIVERGENCE_INVESTIGATE` event during SEC legs           | Row in `platform.application_log`                                          | STOP. SEC legs shouldn't emit those. Mis-wiring. Triage before any further runs.                                        |

## 17. Test strategy

Existing tests are sufficient. No new tests required by this spec.

* `tests/test_backfill_sec_metadata_stage.py::test_stage_registered_in_stage_specs`
  — stage stays addressable from the CLI.
* `tests/test_backfill_sec_metadata_stage.py::test_009_coverage_report_emitted_when_scope_empty`
  — coverage payload contract preserved.
* `tests/test_p1b_cik_long_tail_fallback.py` — guards the P1b boundary
  (the FMP knob must NOT activate here).
* `tests/test_p1b_empirical_finding_documented.py` — guards the P1b
  empirical finding (PR #427); this spec preserves it (does NOT
  recommend re-running `fmp_max_unresolved=0`).
* `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
  carries its own unit tests (test_fundamentals_quarterly_completeness.py
  family) — these are the gate-direction sentinels; this spec does NOT
  change them.

If the §11 or §12 live runs reveal a defect in stage logic, the
follow-up is a plan PR + implementation PR with new hermetic tests
(e.g. `tests/test_metadata_coverage_full_pass.py`). Not in scope here.

## 18. Live smoke strategy

Already enumerated in §§10–12. The bounded-live (§11) IS the smoke; the
full-live (§12) is the actual gate-clearing run. Both are operator-on-demand.

This spec authorizes the §10 dry-run only (it's a no-write operation
behind the existing `dry_run=true` default). §11 and §12 require
explicit operator authorization PER RUN per the standing
"backfills are dry by default" hard rule.

## 19. Open operator decisions

1. **Run §11 / §12 now, or block on a `tradeable_only` knob first?**
   The default scope works correctly today (it writes a superset of the
   gate's denominator). The extra writes are cheap insurance, not waste.
   Recommendation: run as-is. Follow-up `tradeable_only=true` knob is
   a separate efficiency arc, only worth funding if the SEC fetch
   budget becomes the binding constraint.
   **Decision: NEEDS_OPERATOR_DECISION.**

2. **Does the stage's `dry_run=true` gate skip HTTP fetches or only DB
   writes?** Affects whether §10 forecasts actual SEC yield or just
   scope size. **NEEDS_REPO_VERIFICATION** before §10 if the operator
   wants pre-flight yield numbers.

3. **Should §12 add a `--param max_tickers=N` ceiling out of caution?**
   Recommendation: no — the SEC fair-use sleep + 13 500-row cap is the
   natural ceiling. But an operator-set cap (e.g. `max_tickers=5000`
   first, then resume) is a valid risk-averse posture.
   **Decision: NEEDS_OPERATOR_DECISION.**

4. **Coverage measurement denominator clarification.** The
   `coverage_before` / `coverage_after` payload uses the full
   `ticker_classifications.total` as denominator. The gate uses the
   tier ≤ 2 sub-universe. Operator should mentally convert when
   reading the payload, OR a follow-up plan PR could add a
   `coverage_after_tradeable` sub-dict. **Decision: NEEDS_OPERATOR_DECISION**
   (efficiency / clarity, not blocker).

5. **Lifetime-ended guard parity.** The P1b FMP leg explicitly guards
   `lifetime_end IS NULL`. The SEC legs' behavior is
   NEEDS_REPO_VERIFICATION (see §13.2). If operator wants symmetry,
   this is a separate small plan PR; not required for the §10–§12 run.
   **Decision: NEEDS_OPERATOR_DECISION** (deferrable).

## 20. Out of scope

* Any change to `scripts/ops.py` (heavy-lane code change → separate
  plan PR + implementation PR).
* Any change to `tpcore/quality/validation/**` (heavy-lane).
* Any change to `platform/migrations/**` (heavy-lane).
* Any change to engine packages, `tpcore/risk/**`, `tpcore/selfheal/**`,
  `tpcore/order_management/**`, or order/broker behavior.
* P1c source-triage work (separate TODO entry, separate arc).
* The P1b `fmp_max_unresolved=0` full pass (explicitly blocked per
  PR #427).
* Memstore writes; Anthropic API calls; Docker; Railway deploy; admin
  bypass; secret-bearing files in the diff.
