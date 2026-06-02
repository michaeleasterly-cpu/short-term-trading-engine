# I1 — Recent-filer missing-period inference bound (R2 fix) PLAN

**Status:** PLAN ONLY. No implementation. No live DB writes. No live API
calls. No code/config/runtime/trading/risk change. Heavy-lane step 5
(plan PR) per `docs/DEV_PIPELINE_STANDARD.md` §0/§1 and
`.claude/rules/heavy-lane.md` (validator path
`tpcore/quality/validation/**`).

Drafted: 2026-06-02. Owner: operator. Author: data-quality validator track.

## 1. Verdict

The R2/I1 fix is **smallest-possible**: extend the validator's
`_FILING_DATES_SQL` to surface `tc.first_public_filing_date`, collect
it per-ticker alongside `sec_document_type_primary` and
`issuer_lifecycle_state`, and filter the inferred missing-period list
to drop any date strictly less than that ticker's
`first_public_filing_date`. **No new exclusion bucket.** **No threshold
change.** **No migration.** **No new validator concept.** The fix is a
correctness bound on `_infer_missing_period_ends`'s output.

Expected effect on the production data set (per the §12.2 + §11
triage data captured in `docs/superpowers/specs/2026-06-02-validator-semantics-confirmed-data-gap.md`):
**8 tickers transition from FAIL to PASS** (the R2 cohort: GIX, GLIBA,
GLIBK, LMRI, MANE, OYSE, SHAZ, TRAX). Total per-ticker FAIL count
**144 → ≈ 136**. No effect on R1, R3, or R4 cohorts.

This plan **does NOT authorize the implementation PR.** Implementation
follows in a separate heavy-lane step-6 PR after operator authorization
of this plan.

## 2. Spec input

This plan executes against the verdict and §7 (Recent-filer partial
histories) of the merged spec PR #433:

> `docs/superpowers/specs/2026-06-02-validator-semantics-confirmed-data-gap.md`

Specifically the §7 pseudocode block:

```python
# Pseudocode — load-bearing change in _infer_missing_period_ends call site.
if first_public_filing_date is not None:
    relevant_inferred = [
        d for d in inferred
        if d >= first_public_filing_date
    ]
```

The spec calls this a "bug fix in the validator's gap-inference, not a
threshold loosening." This plan implements exactly that.

## 3. Problem statement

The validator's `_evaluate` walks each ticker's sorted `period_end_date`
series and runs `_infer_missing_period_ends` between consecutive entries.
For the 8 R2 tickers, the earliest entry in `period_ends` predates the
issuer's `first_public_filing_date` recorded by the
`backfill_sec_metadata` arc (PR #429). The gap between that stale-or-misloaded
earliest entry and the next entry can span years, causing
`_infer_missing_period_ends` to infer many "missing" periods that
**pre-date the issuer's first SEC filing**. Those periods cannot ever be
backfilled — they do not exist for that issuer.

Concrete case from §6 of the spec — **GIX**:

| field                            | value         |
|----------------------------------|---------------|
| `first_public_filing_date`       | 2026-03-31    |
| `fq_count`                       | 3             |
| `period_ends[0]` (inferred)      | ~2021-09-29   |
| `ev.gaps['GIX']` count           | 18            |
| oldest inferred missing period   | 2021-09-29    |

The validator currently counts 18 missing periods reaching back ~5 years
before the issuer's first SEC filing. That is a false-positive cadence
FAIL.

## 4. R2 evidence

From the §12.2 read-only triage executed during the spec drafting
(2026-06-02 ~05:23 UTC):

| ticker | form | n_miss | first_public_filing_date | fq_count | oldest_inferred_miss |
|--------|------|-------:|--------------------------|---------:|----------------------|
| GIX    | 10-Q | 18     | 2026-03-31               | 3        | 2021-09-29           |
| GLIBA  | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| GLIBK  | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| LMRI   | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| MANE   | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| OYSE   | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| SHAZ   | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |
| TRAX   | 10-Q | …      | (recent IPO)             | <5       | pre-IPO              |

Common shape: `first_public_filing_date` within the last 365 days,
`fq_count < 5`, oldest inferred missing period predates
`first_public_filing_date`. **NEEDS_REPO_VERIFICATION** at
implementation time: re-run the per-ticker triage for the 7 unverified
rows to confirm n_miss and oldest_inferred_miss values.

The spec already verified that `first_public_filing_date` is populated
for these tickers by the post-PR-#429 metadata-coverage backfill.

## 5. Current inference behavior

File: `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`.

Key functions and lines:

* `_FILING_DATES_SQL` at line ~180 — selects `tc.sec_document_type_primary`,
  `tc.issuer_lifecycle_state`, `tc.issuer_lifecycle_event_date` from
  `ticker_classifications` joined to `liquidity_tiers tier <= 2` and
  `fundamentals_quarterly`. **Does NOT select
  `tc.first_public_filing_date`** — that's the gap.
* `_infer_missing_period_ends(earlier, later, *, max_gap_days, period_days)`
  at line 229 — returns a list of `date` objects evenly spaced between
  `earlier` and `later` when the gap exceeds `max_gap_days`. No
  awareness of issuer-existence semantics; it can return any date in
  any range.
* `_evaluate` call site at lines ~390-399 — loops consecutive pairs
  in `period_ends`, accumulates `ticker_gaps`, stuffs them into
  `gaps[ticker]` if non-empty.

`first_filed = period_ends[0]` at line ~355 is the existing
"first-filing proxy" used for the `excluded_dark` / `excluded_confirmed_data_gap`
buckets. It is NOT the same as `first_public_filing_date` and is NOT
adequate for the R2 fix — the stale-or-misloaded earliest entry is
exactly the problem.

## 6. Target inference behavior

After I1:

1. `_FILING_DATES_SQL` includes `tc.first_public_filing_date`.
2. `_evaluate` collects `first_public_filing_date_by_ticker: dict[str, date | None]`
   alongside the existing per-ticker maps (`primary_by_ticker`,
   `lifecycle_by_ticker`).
3. **After** the `for i in range(1, len(period_ends))` inference loop
   builds `ticker_gaps`, filter the list:
   ```python
   fpfd = first_public_filing_date_by_ticker.get(ticker)
   if fpfd is not None:
       ticker_gaps = [d for d in ticker_gaps if d >= fpfd]
   ```
4. If `ticker_gaps` becomes empty after filtering, the ticker contributes
   nothing to `gaps` (i.e. it falls through to PASS).
5. If `first_public_filing_date` is NULL (the legacy / not-yet-backfilled
   case), behavior is **identical to today** — no filter applied.

Critically, this filter is applied at the **caller**, not inside
`_infer_missing_period_ends`. The inference function stays pure and
testable; the bound is a per-ticker, evidence-gated filter at the
single call site that owns the ticker context.

## 7. File change plan

Exactly ONE file changes in the implementation PR (modulo tests):

* `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`

No new imports needed (`date` is already imported). No new constants.
No new schema columns. No migration. The implementation PR diff is
estimated at:

| section                  | added | modified | removed | total LOC |
|--------------------------|------:|---------:|--------:|----------:|
| `_FILING_DATES_SQL`      | 1     | 0        | 0       | 1         |
| `_evaluate` SELECT-binding | 1   | 0        | 0       | 1         |
| `_evaluate` per-ticker map | 2   | 0        | 0       | 2         |
| `_evaluate` filter block | 3     | 0        | 0       | 3         |
| **total source LOC**     | **7** | 0        | 0       | **7**     |

Plus 4–5 new unit tests (~80 LOC).

## 8. Exact code-change sketch

The implementation PR will produce the following diff shape (NOT
applied here — this is the planned shape):

```diff
 _FILING_DATES_SQL = """
     WITH liquid AS (
         SELECT lt.ticker, tc.sec_document_type_primary,
                tc.issuer_lifecycle_state,
-               tc.issuer_lifecycle_event_date
+               tc.issuer_lifecycle_event_date,
+               tc.first_public_filing_date
         FROM platform.liquidity_tiers lt
         JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
         WHERE lt.tier <= $1
     )
     SELECT fq.ticker, fq.period_end_date,
            liquid.sec_document_type_primary,
            liquid.issuer_lifecycle_state,
-           liquid.issuer_lifecycle_event_date
+           liquid.issuer_lifecycle_event_date,
+           liquid.first_public_filing_date
     FROM platform.fundamentals_quarterly fq
     JOIN liquid USING (ticker)
     WHERE fq.period_end_date IS NOT NULL
     ORDER BY fq.ticker, fq.period_end_date
 """

 # ... inside _evaluate, in the per-ticker grouping loop ...
+    first_public_filing_date_by_ticker: dict[str, date | None] = {}
     # existing per-ticker map population (primary_by_ticker, etc.) ...
+        first_public_filing_date_by_ticker[r["ticker"]] = r.get("first_public_filing_date")

 # ... inside the routed-eligible evaluation block, AFTER the inference loop ...
         if ticker_gaps:
+            # R2 fix: drop inferred missing periods that pre-date the
+            # issuer's first public filing. The inference function is
+            # cadence-driven and unaware of issuer existence; this is
+            # the single source of "was this issuer real then?" truth.
+            fpfd = first_public_filing_date_by_ticker.get(ticker)
+            if fpfd is not None:
+                ticker_gaps = [d for d in ticker_gaps if d >= fpfd]
+        if ticker_gaps:
             gaps[ticker] = (sorted(ticker_gaps), primary)
```

Note the **double `if ticker_gaps:`** — the outer one filters out the
case where every inferred gap pre-dates `first_public_filing_date` and
leaves nothing to flag. This is intentional and is the load-bearing
mechanism by which R2 tickers transition to PASS.

## 9. Test plan

5 new unit tests in
`tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`:

1. **`test_recent_filer_does_not_require_periods_before_first_public_filing_date`** —
   Fixture: GIX-shaped — `first_public_filing_date = today − 60 days`,
   3 `fundamentals_quarterly` rows starting 4 years before
   `first_public_filing_date` and ending recently. Before the fix:
   many inferred missing periods → FAIL. After the fix: those pre-FPFD
   periods filtered → PASS (or `excluded_confirmed_data_gap` if
   `< 2 filings` post-filter; the test asserts the per-ticker FAIL
   bucket no longer contains the ticker).

2. **`test_recent_filer_partial_quarter_boundary_handled_conservatively`** —
   Fixture: `first_public_filing_date = 2026-04-15` (mid-quarter);
   filings on 2026-06-30 and 2026-09-30. Inferred missing period
   2026-03-31 (Q1) falls strictly before 2026-04-15 → filtered out.
   The boundary is **exclusive** (`d >= fpfd`); periods strictly less
   than FPFD are dropped, periods equal-to or later are kept. Asserts
   no false FAIL on the Q1 boundary case.

3. **`test_null_first_public_filing_date_preserves_existing_behavior`** —
   Fixture: AAPL-shaped, `first_public_filing_date = NULL` (legacy /
   pre-backfill), 8 filings with one missing quarter. Pre-fix and
   post-fix behavior must be identical: 1 inferred missing period →
   FAIL. Asserts the NULL FPFD path falls back to today's behavior
   exactly.

4. **`test_existing_full_history_issuer_behavior_unchanged`** —
   Fixture: AAPL-shaped, `first_public_filing_date = 1980-12-12`
   (pre-dates every conceivable inferred period), 12 quarters with
   one missing period in 2024-Q2. The FPFD filter is a no-op (every
   inferred date is `>= 1980-12-12`). Asserts the legacy detection
   path is unchanged for long-history issuers.

5. **`test_R2_fixture_cohort_count_reduces_by_8`** — Fixture: a
   synthesized mini-universe containing the 8 R2 tickers (GIX-shaped)
   plus 5 non-R2 control tickers. Asserts pre-fix FAIL count = 13,
   post-fix FAIL count = 5 (the 5 controls), delta = 8.

   **NEEDS_REPO_VERIFICATION** at implementation time: confirm the
   8-count by re-running the live triage against the current
   production DB (read-only; no writes).

All 5 tests use the existing `_quarterly_filings` / `_Pool` fixture
infrastructure already present in the test file. No new fixture
machinery needed.

## 10. Risk assessment

| risk                                                                 | severity | mitigation                                                                                   |
|----------------------------------------------------------------------|:--------:|----------------------------------------------------------------------------------------------|
| The R2 cohort count empirically isn't 8                              | LOW      | Test #5's pre/post assertion is concrete; if production count is different, that's evidence to update the spec, not block the fix |
| A non-R2 ticker has stale `first_public_filing_date`                 | LOW      | The filter is gentle: it only drops gaps strictly older than FPFD. Stale-but-later FPFD doesn't drop anything important |
| `first_public_filing_date` not populated for an R2 ticker            | LOW      | Filter degrades to no-op (NULL-FPFD branch, test #3); no regression                          |
| Detector/healer parity breaks                                        | LOW      | `compute_fundamentals_repair_targets` shares `_evaluate`; filter applies symmetrically       |
| The R2 cohort's `period_ends[0]` is somehow > `first_public_filing_date` | LOW    | In that case the filter has no effect (no inferred gaps strictly older than FPFD); R2 doesn't trigger and that's the right outcome |
| Threshold gets accidentally loosened                                 | NONE     | The filter only removes pre-existence inferred dates. The 25 % metadata-coverage threshold, the 100/450-day cadence thresholds, and the per-cadence liveness windows are all untouched |
| Migration required                                                   | NONE     | `tc.first_public_filing_date` already exists in `ticker_classifications` (migration `20260530_0200`) |
| Test isolation broken (DB / network)                                 | NONE     | The validator's existing hermetic fixture pattern (`_Pool`, `_quarterly_filings`) extends cleanly to FPFD-bearing rows |

## 11. Acceptance criteria

For the implementation PR (NOT this plan):

1. The five tests in §9 all pass on a single-process pytest
   (`python -m pytest -p no:xdist`) AND on the reversed module-order
   reflip per `.claude/rules/tests-and-ci.md`.
2. The full-suite gate (`python -m pytest -p no:xdist`) is green —
   especially the existing
   `test_C7_pre_ipo_quarters_not_demanded`,
   `test_C8_healer_symmetry_with_check`, and
   `test_C9_clean_state_returns_empty_targets` tests must still pass
   without modification (they exercise paths unaffected by the FPFD
   filter; if any of them changes behavior, the fix is over-reaching).
3. `ruff` clean.
4. `gitleaks` clean.
5. `check_manifests.py` OK.
6. CI rollup `statusCheckRollup` conclusion `SUCCESS`.
7. **Operator-side post-merge verification (READ-ONLY, no live runs)**:
   re-run `_evaluate` against the live production database and confirm:
   * `len(ev.gaps)` drops from 144 to ≈ 136 (R2 cohort dropped).
   * `metadata_coverage_low` remains `False` (no structural-sentinel
     regression).
   * GIX, GLIBA, GLIBK, LMRI, MANE, OYSE, SHAZ, TRAX are no longer
     in `ev.gaps`.
   * No new tickers added to `ev.gaps` (no regression cohort).

For this plan PR:

1. Plan is doc-only (one new doc; optional `TODO.md` + sentinel test).
2. Plan follows merged spec #433 explicitly (§2 cross-reference).
3. Plan defines exact future implementation change (§8 diff sketch).
4. Plan defines tests proving recent-filer false positives are removed
   without changing legacy behavior (§9).
5. Plan explicitly says validator threshold is unchanged (§1, §6, §10).
6. CI passes; normal merge.

## 12. Verification commands

For this plan PR (no code change):

```
python scripts/check_manifests.py
python -m pytest -p no:xdist -q tests/test_i1_recent_filer_plan_documented.py  # if added
python -m ruff check tests/test_i1_recent_filer_plan_documented.py  # if added
gitleaks detect --config .gitleaks.toml --no-banner --redact --source .
git diff --name-only
```

For the future implementation PR (per acceptance criteria §11):

```
python -m pytest -p no:xdist
python -m ruff check tpcore tests
python scripts/check_manifests.py
gitleaks detect --config .gitleaks.toml --no-banner --redact --source .
git diff --name-only
```

## 13. Implementation prompt

When the operator authorizes the implementation PR, the implementer
should be briefed with this prompt (verbatim):

> **Task**: implement I1 (R2 fix) per
> `docs/superpowers/plans/2026-06-02-i1-recent-filer-missing-period-bound-plan.md`.
>
> **Scope**: `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
> + `tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`.
> No other files. No migration. No threshold change. Touch each line
> exactly as the diff sketch in plan §8 specifies.
>
> **Tests** (per plan §9): add the 5 new tests. Do NOT modify the
> existing C1-C12 tests except their internal fixtures if needed for
> the new column (the
> `_Pool` / `_quarterly_filings` helpers may need a `first_public_filing_date`
> kwarg with a default of `None` — that change is acceptable and
> backwards-compatible).
>
> **Gates** (per plan §11): single-process pytest + reversed order-flip
> green; full suite green; ruff clean; gitleaks clean;
> check_manifests OK; CI `statusCheckRollup` `SUCCESS`. Operator
> verifies post-merge against the live production DB.
>
> **Heavy-lane discipline**: split-review (spec-compliance reviewer
> first, then code-quality reviewer) per
> `.claude/rules/heavy-lane.md`. Operator authorizes the merge.
>
> **What not to do**: do not loosen any threshold. Do not add a new
> exclusion bucket (`excluded_confirmed_data_gap` exists for the
> `< 2 filings + past grace` case; the R2 fix is **not** about adding a
> new bucket — it's about correcting the inference function's output for
> tickers that have `≥ 2 filings` and a known `first_public_filing_date`).
> Do not change the SQL except to add the `first_public_filing_date`
> column. Do not change `_infer_missing_period_ends`'s signature or
> internals; the filter is applied at the caller.

---

## Open operator decisions

1. **R1/R3/R4 sequence after I1.** The spec PR #433 listed implementation
   ordering as I1 → I2 → I3 → I4 (cheapest to most-invasive). After I1
   lands, the next plan should be I2 (R4 — physical_truth → data_quality_log).
   Confirm or re-order? **Decision: NEEDS_OPERATOR_DECISION.**

2. **Test #5 fixture cohort.** Should the test use real-ticker symbols
   (GIX, GLIBA, …) or synthetic ("R2_FIXTURE_01", …)? Real-ticker is
   more legible; synthetic keeps test isolated from any future
   re-population of those symbols' `first_public_filing_date`.
   Recommendation: synthetic. **Decision: NEEDS_OPERATOR_DECISION.**

3. **Live verification step.** After the implementation PR merges,
   should an operator-on-demand `--check` re-run be required, or is
   the unit-test set sufficient? Recommendation: re-run because the
   §12.2 production state is the load-bearing evidence of correctness.
   **Decision: NEEDS_OPERATOR_DECISION.**

## Next item

If this plan merges: **draft the I1 implementation PR** per the
prompt in §13. ~7 LOC source change + 5 new tests; heavy-lane
discipline applies. After implementation, re-run `_evaluate` read-only
against the live DB to verify 144 → ≈ 136 transition; record empirical
result in TODO + as a "Post-execution result" section appended to this
plan.
