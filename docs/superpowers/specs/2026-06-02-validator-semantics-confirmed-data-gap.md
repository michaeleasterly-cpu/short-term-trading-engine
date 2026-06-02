# Validator semantics — confirmed-data-gap classification

**Status:** SPEC ONLY. No implementation. No live DB writes. No live API calls.
No code/config/runtime/trading/risk change. Heavy-lane step 3 (spec PR) per
`docs/DEV_PIPELINE_STANDARD.md` §0/§1 and `.claude/rules/heavy-lane.md`.

Drafted: 2026-06-02. Author: data-operations / validator track. Owner:
operator (decision gates marked below).

## 1. Verdict

The 144 per-ticker FAILs surviving the §12.2 empirical stop split into
**four classes**, each needing a different treatment. None of the
treatments may loosen the `DATA_OPERATIONS_COMPLETE` threshold. The
metadata-coverage structural sentinel stays unchanged. The
`prices_daily_completeness` zero-tolerance invariant is untouched.

The right next step is **a small, evidence-gated extension of the
existing exclusion-bucket model in
`tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`**,
not a relaxation of any threshold. Specifically:

* **New bucket: `excluded_source_confirmed_unavailable`** — fires only
  when **multi-source negative attestation** is on record (FMP returned
  no_data for the period **AND** SEC companyfacts returned no_data for
  the period **AND** the period is past the issuer's `first_public_filing_date`).
* **Bound `_infer_missing_period_ends` by `first_public_filing_date`** —
  recent filers stop being charged for pre-IPO inferred periods.
* **Fix annual-cadence period storage** so 20-F / 40-F filers populate
  `fundamentals_quarterly` with annual `period_end_date` rows, not
  quarter-shaped rows. The validator's annual routing already exists;
  the data layer's shape is the bug.
* **`physical_truth` rejections recorded in `data_quality_log`** with
  per-ticker disposition. ARDT-style anomalies become operator-visible
  without being silently re-fetched on every backfill.

All four are **separate plan PRs and separate implementation PRs**. This
spec just classifies and proposes the shape. **No implementation is
authorized here.**

## 2. Problem statement

After PR #432's empirical correction, the 144 per-ticker FAILs are:

* Not refresh-fillable (FMP source lacks the periods — §12.2 empirical).
* Not gateable as `metadata_coverage_insufficient` (structural sentinel
  is OFF).
* Not lifecycle-terminated (those are `excluded_lifecycle_terminated`,
  6 tickers excluded that way).
* Not pre-`< 2 filings + past grace` (those are
  `excluded_confirmed_data_gap`; 0 tickers excluded that way today).

So they trigger the per-ticker FAIL surface — correctly per the gate's
contract, but not actionable via any existing healer. The
`DATA_OPERATIONS_COMPLETE` gate stays blocked. The right disposition
for each is the question this spec answers.

## 3. Evidence from §12.2 empirical stop

(Cross-reference: PR #432 +
`docs/superpowers/specs/2026-06-02-fundamentals-cadence-fail-triage.md`
"Post-execution result" section.)

| signal                              | value          | implication                                      |
|-------------------------------------|----------------|--------------------------------------------------|
| `tickers_succeeded` / attempted     | 10 / 10        | stage mechanically correct                       |
| `fundamentals_quarterly.total` delta | +4 (all AACB) | FMP source lacked periods for 9/10               |
| Per-ticker FAIL count post-§12.2    | 144 → 144      | no validator improvement                         |
| `physical_truth gate rejections`    | 5 on ARDT      | safety mechanism worked; anomaly is real         |

**`historical_fundamentals_quarterly` cannot heal this set. Source
augmentation OR validator-semantics change is the only path forward.**

## 4. Current validator behavior

`fundamentals_quarterly_completeness._evaluate` produces these
per-ticker buckets (file:
`tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`):

| Bucket                              | Trigger                                                                 |
|-------------------------------------|-------------------------------------------------------------------------|
| `evaluated_routed`                  | Routed-eligible (10-Q / 10-K / 20-F / 40-F) with ≥ 2 filings; cadence evaluated |
| `excluded_dark`                     | Liveness window exceeded (per-cadence)                                  |
| `excluded_metadata_required`        | `sec_document_type_primary` is NULL                                     |
| `excluded_confirmed_data_gap`       | `< 2 filings` AND `first_filing` past `_NEW_LISTING_GRACE_*_DAYS`       |
| `excluded_lifecycle_terminated`     | `issuer_lifecycle_state ∈ {deregistered, delist_effective}` (Form 25 / Form 15 evidence) |
| `excluded_other_form`               | Primary form not in routed set (e.g. N-1A)                              |
| **Per-ticker FAIL (`gaps`)**        | Cadence gap inferred from period_end_date series via `_infer_missing_period_ends` |

The metadata-coverage structural sentinel fires when
`excluded_metadata_required / (evaluated_routed + excluded_metadata_required) > 0.25`.
That's the ungameable boundary; not in scope here.

`compute_fundamentals_repair_targets` returns the same `gaps` set the
detector reports — detector/healer parity is the existing structural
invariant.

## 5. Remaining failure taxonomy

After PR #432's correction, the 144 split into four operator-actionable
classes (empirical counts from the §12.2 read-only triage):

| Class | Count    | Cause                                                                         |
|-------|---------:|-------------------------------------------------------------------------------|
| **R1 — FMP-unreachable historical residual** | ~117 (the corrected B-bucket)  | Inferred periods don't exist in FMP; cross-source attestation needed         |
| **R2 — Recent-filer pre-IPO over-inference** | 8 (former C1)               | `_infer_missing_period_ends` reaches back before `first_public_filing_date`  |
| **R3 — Annual-filer cadence-storage mismatch** | 7 (former C2)               | 20-F / 40-F filings stored with quarterly `period_end_date` shape            |
| **R4 — Physical_truth anomaly** | per-occurrence (ARDT = 1 currently) | FMP returned malformed rows; safety gate rejected; no validator surface today |

Sample tickers per class (alphabetical first 5):

* R1: ADV, AEVA, AGPU, AIDX, AKTS
* R2: GIX, GLIBA, GLIBK, LMRI, MANE
* R3: ASTL (40-F), CGNT, FRGT, IBG, IMPP
* R4: ARDT

Note: classes are not mutually exclusive in principle (an R3 ticker
could also have R1 periods). Empirically the §12.2 sample showed each
ticker fits cleanly in one class.

## 6. FMP-unreachable historical residuals (R1)

**Hypothesis**: the missing period_end_dates the validator infers for
these tickers are absent from FMP because they're either (a) genuinely
unreported by the issuer for that period (gap that no source has), or
(b) reported but only available via SEC EDGAR / a different provider.

**Right disposition**: NOT exclude on single-source evidence alone.
That would weaken the gate. The principled exclusion requires
**multi-source negative attestation**:

1. FMP returned `no_data` (or a present but empty fundamentals payload)
   for the period.
2. SEC companyfacts (`_stage_sec_fundamentals_fallback`,
   `scripts/ops.py:1042`) returned `no_data` for the period.
3. The period is past the issuer's `first_public_filing_date`.
4. The period is NOT in the active liveness window (don't exclude a
   recent quarter just because both sources haven't shipped yet).

When all four hold, the period is `source_confirmed_unavailable`. The
ticker's per-ticker FAIL would route to a new
**`excluded_source_confirmed_unavailable`** bucket.

**Authority/provenance requirements (load-bearing — these are the
guard against silently hiding real gaps):**

* Provenance recorded per (ticker, period_end_date) pair in
  `platform.data_quality_log` (existing table; see
  `platform/migrations/20260509_0000_initial_platform_schema.py:68`)
  with fields naming each source consulted and the
  `recorded_at` UTC timestamp.
* Re-attestation cadence: NO LESS THAN every 90 days. If a source's
  `no_data` is older than 90 days, the period is RE-EVALUATED on the
  next validator run (don't exclude on stale negative evidence).
* Operator override: an explicit `operator_disposition` field allows
  manual exclusion with a free-text reason. Operator overrides are
  permanent until explicitly removed.

**What must remain blocking (R1)**: anything failing all four conditions
above — including the case where one source has the period but the
other doesn't (the gap is real, the source disagreement is the signal).

## 7. Recent-filer partial histories (R2)

**Hypothesis**: `_infer_missing_period_ends` walks the
`period_end_date` series and extrapolates expected periods between
filings. For tickers with `first_public_filing_date` in (today − 365 d)
and `fq_count < 5`, the inference can reach back ~5 years before the
issuer's first SEC filing. Those inferred periods don't exist for the
issuer.

**Right disposition**: bound the inference. In `_evaluate`:

```python
# Pseudocode — load-bearing change in _infer_missing_period_ends call site.
if first_public_filing_date is not None:
    # Don't infer periods that pre-date the issuer's first filing.
    relevant_inferred = [
        d for d in inferred
        if d >= first_public_filing_date
    ]
```

This is a **bug fix in the validator's gap-inference**, not a
threshold loosening. The validator was over-counting; the corrected
count reflects reality.

**What must remain blocking (R2)**: cadence gaps in periods AFTER
`first_public_filing_date`. If a recent filer has 3 filings spanning
the last year and one expected period is missing, that's still a FAIL.

**Provenance**: `first_public_filing_date` is already populated by the
`backfill_sec_metadata` stage (see PR #429). The R2 fix only consults
that column; no new evidence needed.

## 8. Annual-filer cadence mismatch (R3)

**Hypothesis**: the P1 validator rewrite correctly routes 20-F / 40-F to
annual cadence (`MAX_ANNUAL_GAP_DAYS = 450`). Per the §12.2 sample, ASTL
(40-F) has 33 `fundamentals_quarterly` rows with `period_end_date`
values like 2015-09-15, 2019-07-16 — quarterly-shaped dates, not annual.
The validator's annual cadence math then sees gaps in the
quarterly-spaced records and infers "missing periods."

This is a **storage-shape bug**, not a validator-routing bug. The
validator's cadence routing is correct; the
`platform.fundamentals_quarterly` shape for annual filers stores
quarterly-cut dates because FMP ships them that way.

**Right disposition (two options, both need separate plan PRs)**:

* **Option A** — Add a `cadence_class` column to
  `platform.fundamentals_quarterly` (filled by the ingestion stage from
  `sec_document_type_primary`). The validator's
  `_infer_missing_period_ends` consults `cadence_class` to decide
  whether to infer quarterly or annual cadence per row.
* **Option B** — Keep `period_end_date` shape but add an
  `is_annual_anchor` boolean to filter the per-ticker series to one
  row per fiscal year before running the annual inference. Less
  disruptive but requires the ingestion to mark which quarterly row is
  the "fiscal-year-end" anchor.

**What must remain blocking (R3)**: annual filers genuinely past
`MAX_ANNUAL_GAP_DAYS` since their last 10-K / 20-F / 40-F. The gate is
the contract; the disposition only fixes the cadence math, not the
threshold.

**NEEDS_REPO_VERIFICATION**: confirm whether the ingestion path
(`tpcore.ingestion.handlers.handle_sec_fundamentals_fallback` and
`FundamentalsCache.backfill_all`) already has any per-row form
metadata to surface `cadence_class`. If yes, Option A is cheaper. If
no, both options require ingestion-side changes.

## 9. Physical_truth anomaly disposition (R4)

**Background**: `FundamentalsCache._upsert_payload` runs a physical_truth
gate before writing rows. Anomalous rows (the ARDT 5-row example from
§12.2) are logged via `structlog` at `tpcore/fundamentals/cache.py:423`
but do not surface in the validator's exclusion model.

**Hypothesis**: the rejected rows are FMP data quality bugs that the
safety gate caught. The cache correctly refused to write them. But
the validator never learns about them, so:

1. The next backfill re-fetches them, re-rejects them, re-logs — no
   forward progress.
2. The operator has no way to disposition "FMP shipped 5 bad rows for
   ARDT; treat as long-tail unless and until FMP fixes."

**Right disposition**: persist physical_truth rejections to
`platform.data_quality_log` with:

* `ticker`
* `period_end_date` (or `null` if pre-parse)
* `reject_reason` (the gate's classification — e.g. `negative_close`,
  `inconsistent_ohlc`, `out_of_range_ratio`)
* `source` (the provider; `fmp` for the §12.2 case)
* `rejected_at` UTC

The validator does NOT auto-exclude on physical_truth rejections —
those are operator-disposition signals. But the `application_log` /
`data_quality_log` surface lets the operator decide per-ticker
whether to:

* File a vendor ticket (escalate),
* Wait for vendor fix (no action; periodic re-attempt),
* Operator-disposition as `excluded_source_confirmed_unavailable`
  with the physical_truth event as evidence (manual override path
  from §6 #2).

**What must remain blocking (R4)**: anything where physical_truth
rejection happens AND the operator hasn't dispositioned the ticker.
The default is "fail loudly until operator decides."

## 10. Confirmed data-gap semantics

The new bucket `excluded_source_confirmed_unavailable` is the principal
addition. Semantics summary:

* **Scope**: per (ticker, period_end_date) pair, NOT per ticker.
* **Trigger**: multi-source negative attestation per §6, or operator
  override.
* **Re-evaluation**: every 90 days (stale-evidence floor); fewer days
  if the operator wants stricter.
* **Counter**: a new `excluded_source_confirmed_unavailable` int field
  on `_Evaluation`; logged via structlog at completion (same shape as
  the existing exclusion counters).
* **Effect on per-ticker FAIL**: a ticker whose ONLY missing periods
  are all in this bucket transitions from FAIL to PASS-with-exclusion.
  A ticker with mixed buckets stays FAIL until ALL its missing periods
  are dispositioned.

**This is NOT a threshold loosening.** A period is only excluded when
the evidence says it's genuinely unavailable. The gate's contract is
preserved.

## 11. What must remain blocking

| Failure shape                                                                | Must remain blocking? | Why                                                  |
|------------------------------------------------------------------------------|:---------------------:|------------------------------------------------------|
| Per-ticker gap with FMP `no_data` but SEC `available`                        | YES                   | Real source-side gap; cross-source disagreement     |
| Per-ticker gap with no evidence collected yet                                | YES                   | Single-source absence is not sufficient evidence    |
| Per-ticker gap with multi-source evidence older than 90 days                 | YES                   | Stale evidence triggers re-evaluation               |
| Per-ticker gap in cadence after `first_public_filing_date`                   | YES                   | Genuine cadence FAIL                                |
| Annual filer past `MAX_ANNUAL_GAP_DAYS` since last 10-K / 20-F / 40-F        | YES                   | Genuine annual cadence FAIL                         |
| Per-ticker gap blocked by physical_truth rejection WITHOUT operator disposition | YES               | Default-block until operator triages                 |
| `metadata_coverage_low` sentinel (existing)                                  | YES                   | Untouched by this spec                              |
| `prices_daily_completeness` zero-tolerance invariant                         | YES                   | Untouched by this spec; the gate's gate              |

## 12. What may be excluded

| Failure shape                                                                | Disposition                                                                                  |
|------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| Inferred period strictly before `first_public_filing_date`                   | NOT inferred (R2 fix; recategorized as "never a gap")                                         |
| Inferred period with multi-source negative attestation AND past first filing | `excluded_source_confirmed_unavailable` (R1)                                                  |
| Annual filer when annual-shape cadence is correctly applied                  | Routed-eligible cadence evaluation, NOT a quarterly miss (R3 fix; data-layer change)         |
| Per-ticker FAIL with explicit operator disposition row                       | `excluded_source_confirmed_unavailable` (manual override; operator's name + reason captured) |

## 13. Authority / provenance requirements

* **Per-period evidence rows** live in `platform.data_quality_log`
  (existing table). The migration `20260510_1200` already enforces a
  `unique(source, timestamp)` constraint — needs to be confirmed that
  adding per-(ticker, period) rows fits the table's schema. NEEDS_REPO_VERIFICATION.
* **Operator dispositions** are first-class: a `disposition` field with
  enum `pending / source_confirmed_unavailable / vendor_bug / operator_excluded`
  and free-text `reason`. Migration likely required to add columns to
  the existing `data_quality_log` table OR a new
  `fundamentals_period_disposition` sidecar table.
* **No retroactive exclusion**: a `data_quality_log` row in
  `disposition = pending` does NOT exclude. Only an explicit
  non-pending status excludes.
* **Audit**: every exclusion the validator applies is replayable from
  the evidence rows on file at the time of evaluation.

## 14. Non-goals

Out of scope for this spec PR:

* Code changes anywhere (`tpcore/**`, `scripts/**`, `ops/**`,
  `platform/**`, `.claude/**`, `.github/workflows/**`, engine packages).
* Migration / schema changes.
* Any change to the threshold (`METADATA_COVERAGE_FAIL_THRESHOLD`,
  `MAX_QUARTERLY_GAP_DAYS`, `MAX_ANNUAL_GAP_DAYS`, the per-cadence
  liveness windows).
* Loosening the `DATA_OPERATIONS_COMPLETE` contract.
* Re-running `historical_fundamentals_quarterly` (still blocked per
  PR #432).
* The `_stage_sec_fundamentals_fallback` spike (separate plan PR; that
  stage already exists, but consuming its results into validator
  evidence is part of the plan PR for R1).
* Memstore writes; Anthropic API calls; Docker; Railway deploy; admin
  bypass; secret-bearing files in the diff.

## 15. Implementation options (each = separate plan + impl PRs)

Listed in order of operator priority (R2 cheapest; R1 highest leverage).

| ID | Class                              | Estimated effort                          | Migration?                      | Heavy-lane gate                                                  |
|----|------------------------------------|-------------------------------------------|---------------------------------|------------------------------------------------------------------|
| I1 | R2 fix — bound by `first_public_filing_date` | One-line change in `_infer_missing_period_ends` call site + 2-3 unit tests | NO            | YES (touches `tpcore/quality/validation/**`)                     |
| I2 | R4 fix — persist physical_truth rejections   | New `cache.py` write path → `data_quality_log` + cascade catalog row | POSSIBLY (new column) | YES (touches `tpcore/quality/**` if validator reads the rows)    |
| I3 | R1 fix — multi-source attestation evidence path | New evidence-collection step in the validation cascade + `_evaluate` consults evidence rows | YES (likely new sidecar table) | YES (touches `tpcore/quality/validation/**` + `platform/migrations/**`) |
| I4 | R3 fix — annual-cadence storage shape         | Ingestion-side column or anchor flag + validator-side per-row cadence routing | YES (new column) | YES (touches multiple heavy-lane paths)                          |

I1 is the smallest unblock and should land first. I2 is the smallest
data-quality-log integration. I3 is the highest-leverage on the 117
R1 cohort and is the architectural anchor for everything that follows.
I4 is the most invasive (touches ingestion + validator + migration).

**This spec does NOT authorize any of I1-I4.** Each needs its own
plan PR (heavy-lane step 5).

## 16. Test strategy

No new tests required by this spec.

For future I1-I4 plan/implementation PRs, the existing test surface
already covers the load-bearing invariants:

* `tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`
  — cadence math + threshold semantics (do not regress).
* Detector/healer parity invariant: `compute_fundamentals_repair_targets`
  always equals `set(_evaluate.gaps)`. Continues to hold.
* The `prices_daily_completeness` zero-tolerance test continues to be
  the structural floor.

Each future plan PR will add evidence-row hermetic tests + at least
one full-suite regression test that asserts a known-FAIL ticker
correctly transitions to PASS-with-exclusion under the new bucket,
and a known-FAIL ticker without sufficient evidence STAYS FAIL.

## 17. Operator / live verification strategy

This spec authorizes **no live operator runs**. The validator can be
re-evaluated read-only at any time (the `_evaluate` function is pure
SQL + computation; ran four times during the metadata-coverage arc).

Future plan PRs will define their own dry-run / bounded-live /
full-live sequences per the standing "backfills are dry by default"
rule.

## 18. Open operator decisions

1. **R2 fix order.** Land I1 first as the cheapest unblock, before
   anything else? It would drop 8 tickers from the FAIL set immediately
   and demonstrate the spec's correctness. Recommendation: yes.
   **Decision: NEEDS_OPERATOR_DECISION.**

2. **R1 evidence storage shape.** Add columns to `data_quality_log` or
   create a new `fundamentals_period_disposition` sidecar table?
   Architectural decision; both options have tradeoffs (denormalized
   vs. sidecar). **Decision: NEEDS_OPERATOR_DECISION** for the I3 plan PR.

3. **R3 — column add vs. anchor flag?** Both Option A
   (`cadence_class` column) and Option B (`is_annual_anchor` flag) work.
   A is cleaner; B is a smaller migration. **Decision: NEEDS_OPERATOR_DECISION**
   for the I4 plan PR.

4. **R4 — auto-disposition policy?** Should the validator auto-create a
   `pending` row in `data_quality_log` on every physical_truth
   rejection, or only when the same ticker hits ≥ 2 rejections within
   a window? Auto-create-on-first is simpler; the threshold approach
   reduces operator noise. **Decision: NEEDS_OPERATOR_DECISION** for
   the I2 plan PR.

5. **Re-attestation cadence (R1).** Is 90 days the right stale-evidence
   floor? Could be 30 / 60 / 90 / 180. 90 was a starting point.
   **Decision: NEEDS_OPERATOR_DECISION** for the I3 plan PR.

6. **Manual operator-override entry surface.** Where does the operator
   actually write the `operator_disposition` row? Dashboard mutator,
   `scripts/ops.py` stage, direct SQL? Recommendation: a tiny
   `scripts/ops.py` stage `disposition_fundamentals_period` so the
   audit trail is consistent with all other DB mutations.
   **Decision: NEEDS_OPERATOR_DECISION** for the I3 plan PR.

---

## Next item

If this spec merges: **draft the I1 (R2 fix) plan PR** as the smallest
unblock. The plan PR enumerates the exact `_infer_missing_period_ends`
call-site change, the affected tests, and the heavy-lane review path.
I1 is the only one of I1-I4 that doesn't need a migration; landing it
first proves the spec's correctness on a small surface before the
larger arcs commit.
