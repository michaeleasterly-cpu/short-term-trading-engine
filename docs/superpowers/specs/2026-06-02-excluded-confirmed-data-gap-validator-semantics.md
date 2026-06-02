# `excluded_confirmed_data_gap` validator-semantics extension

**Status:** SPEC ONLY. No implementation. No DB writes. No live SEC fallback.
Direct successor to PR #448 (`dry_run` knob) + PR #449 (SEC fallback spike
closeout). Establishes the validator-semantics design that the
`if_source_unavailable` branch of the 2026-06-02 operator decision tree
points to.

Drafted: 2026-06-02. Owner: operator. Author: validator-semantics /
data-quality track.

## 1. Verdict

The validator's `excluded_confirmed_data_gap` bucket **already exists**
(`tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
lines 213, 322, 378) but only fires for the **narrow** case of tickers
with `< 2 filings AND first-filing past grace`. This spec **extends its
semantic** to also cover **period-level confirmed unavailable** — a row
whose inferred missing `period_end_date` has been **attempted from BOTH
FMP and SEC AND both attempts yielded empty**, freshness-gated, with a
queryable evidence substrate.

**No threshold loosening. No global exclusion.** Each exclusion requires
explicit dual-source evidence per `(ticker, period_end_date)`. The 144
per-ticker FAILs documented in PR #430 + the 117-row FMP-unreachable
historical residual + the 1.4% SEC-fillable yield (PR #449) together
support the design but **only individual rows with evidence move into
the bucket** — never tickers en masse, never via heuristic.

## 2. Current validator state (read this before designing)

`fundamentals_quarterly_completeness._evaluate` (line ~322) returns an
`_Evaluation` with five exclusion buckets + one routed bucket:

| Bucket | Semantic (current) |
|---|---|
| `evaluated_routed` | ≥ 2 filings; cadence-routed; full gap evaluation produces PASS or FAIL |
| `excluded_dark` | silence-based heuristic (no recent filings, exceeds liveness window, no lifecycle evidence) — fallback when no Form 25/15 |
| `excluded_metadata_required` | `sec_document_type_primary` is NULL (P0 metadata not yet populated) |
| `excluded_confirmed_data_gap` | **< 2 filings + first-filing past grace** — sparse-ticker exclusion only |
| `excluded_other_form` | non-quarterly/annual primary form (e.g., 6-K) |
| `excluded_lifecycle_terminated` | Form 25/15 evidence (P2b) |

Per-row gap evaluation (line 386+): for each `evaluated_routed` ticker,
`_infer_missing_period_ends(earlier, later)` infers missing
`period_end_date` rows based on cadence. Each inferred date that lacks a
filing becomes a "gap" → the ticker FAILs.

**`CheckResult` is frozen.** Diagnostic counters are logged via
structlog at completion, not serialized into `CheckResult`. The
extension does NOT modify the frozen model.

## 3. Empirical evidence motivating the extension

| Source | Finding |
|---|---|
| PR #430 (spec) | 144 per-ticker FAILs surface after metadata-coverage gate cleared; B-bucket forecast was "FMP historical backfill heals 89.6%" |
| PR #432 §12.2 (live smoke, 10-ticker FMP cascade) | 1 of 10 tickers improved (AACB); 9 unchanged; FMP source-data lacks the inferred periods |
| PR #448 (impl) | Added `dry_run` knob to `sec_fundamentals_fallback` |
| PR #449 (SEC spike, 10-ticker cohort) | 9 of 10 in scope; 72 inferred missing periods; **1 SEC archive_row_planned**; 1.4% yield; SOURCE-UNAVAILABLE |
| Combined | Both FMP and SEC structurally lack the inferred periods for ~99% of bucket-A/B/C tickers. The validator's `_infer_missing_period_ends` is structurally over-inferring — OR these periods truly don't exist in any current public source |

The empirical floor: **143 of 144 per-ticker FAILs** survive dual-source
attempt. These are the rows that this spec authorizes (post-evidence) to
move into `excluded_confirmed_data_gap`.

## 4. What qualifies as source-confirmed unavailable

A `(ticker, period_end_date)` tuple qualifies if and only if ALL of the
following hold simultaneously:

1. **FMP attempt evidence**: a row exists in the evidence substrate
   recording that the canonical FMP cascade (e.g.,
   `historical_fundamentals_quarterly` or `fundamentals_refresh`)
   attempted the period and yielded no rows. Producer-hard-stop on
   silent vendor contract drift remains the upstream guard.
2. **SEC attempt evidence**: a row exists recording that
   `sec_fundamentals_fallback` (post-PR-448) attempted the period and
   `sec.extract_period(facts, pe)` returned `None`. The SEC fetch
   itself must have succeeded (no 404, no `DataProviderOutage`).
3. **Freshness gate**: both attempts are dated within the last
   `CONFIRMED_DATA_GAP_FRESHNESS_DAYS` (default proposal: 180 days /
   ~2 fiscal quarters). Periods evidenced > 180 days ago must be
   re-attempted; sources may have backfilled.
4. **No "outage"-shaped evidence**: if either attempt logged a fetch
   failure (404, timeout, `DataProviderOutage`), the row does NOT
   qualify — those need re-attempt, not exclusion.

A row that DOES qualify routes to the extended
`excluded_confirmed_data_gap` bucket. A row that DOES NOT qualify
remains in `evaluated_routed → FAIL` until evidence accrues or the
period becomes source-available.

## 5. Evidence substrate design

### 5.1 New table proposal

`platform.fundamentals_period_source_evidence`:

| Column | Type | Notes |
|---|---|---|
| `ticker` | text NOT NULL | |
| `period_end_date` | date NOT NULL | |
| `source` | text NOT NULL | `'fmp_historical' \| 'fmp_refresh' \| 'sec_companyfacts'` |
| `last_attempted_at` | timestamptz NOT NULL | |
| `outcome` | text NOT NULL CHECK | `'yielded' \| 'empty' \| 'fetch_failure' \| 'extract_none'` |
| `notes` | text NULL | source-specific payload (e.g., FMP run_id, SEC CIK) |
| `recorded_at` | timestamptz NOT NULL DEFAULT NOW() | |

PRIMARY KEY: `(ticker, period_end_date, source)`. UPSERT on conflict —
latest attempt wins per `(ticker, period_end_date, source)`.

### 5.2 Population paths

| Path | Who writes |
|---|---|
| FMP cascade (`historical_fundamentals_quarterly`, `fundamentals_refresh`) | extends to record `outcome='empty'` for each requested-but-not-returned period; today these stages don't track this — implementation PR adds the write |
| SEC fallback (`sec_fundamentals_fallback`, post-PR-448) | extends to record `outcome='extract_none'` for each period the stage tried that came back `None` from `sec.extract_period`. The dry-run path can populate evidence as a side effect of preview (operator-authorized) OR live path populates as part of normal write |

**Operator decision required:** does dry-run populate evidence, or is
evidence only written by live runs? Spec recommendation: **live-only
writes** — evidence is a live-DB attestation that the source was
attempted with real intent, not a preview side effect.

### 5.3 Alternative considered: extend `data_quality_log`

`data_quality_log` already exists. Adding rows there with
`source='confirmed_data_gap_evidence.{provider}.{ticker}.{period}'`
would avoid a new table. **Rejected because**:

- Natural-key joins from the validator to `data_quality_log` are
  awkward (no `period_end_date` column there; would need to parse
  `source` text).
- The 25-column `data_quality_log` schema is overloaded already; this
  evidence shape is materially different (per-period, not per-event).
- The new table is a tightly-scoped substrate; cleaner to read in the
  validator's tight SQL window.

## 6. Validator wiring changes

### 6.1 New per-period evidence join

In `_evaluate`, after `_infer_missing_period_ends` produces
`ticker_gaps`, the validator queries:

```sql
SELECT period_end_date
FROM platform.fundamentals_period_source_evidence
WHERE ticker = $1
  AND period_end_date = ANY($2::date[])
  AND last_attempted_at >= NOW() - INTERVAL '180 days'
  AND outcome IN ('empty', 'extract_none')
GROUP BY period_end_date
HAVING COUNT(DISTINCT source) FILTER (
  WHERE source IN ('fmp_historical', 'fmp_refresh')
) >= 1
AND COUNT(DISTINCT source) FILTER (
  WHERE source = 'sec_companyfacts'
) >= 1;
```

For each `period_end_date` in the result, increment
`excluded_confirmed_data_gap += 1` and remove from `ticker_gaps`. The
remaining `ticker_gaps` stay → ticker FAILs on those.

### 6.2 Sub-counter (logged, not in CheckResult)

Add to `_Evaluation`:

```python
excluded_confirmed_data_gap_evidenced: int = 0
```

This sub-counter separately tracks the "extended" semantic from the
existing "< 2 filings" semantic. Both increment
`excluded_confirmed_data_gap`; the sub-counter clarifies origin in
structlog output:

```
{
  "excluded_confirmed_data_gap": 144,
  "excluded_confirmed_data_gap_sparse": 1,           # < 2 filings + past grace
  "excluded_confirmed_data_gap_evidenced": 143,      # dual-source evidence
  ...
}
```

### 6.3 No CheckResult shape change

`CheckResult` stays frozen. The PASS/FAIL outcome is unchanged at the
top level; only the routing of WHICH rows count toward FAIL changes.

## 7. Distinguishing source-unavailable from over-inference

Two failure modes share a symptom (the validator infers a date no
source has):

| Mode | Cause | Disposition |
|---|---|---|
| **Source-unavailable** | Period truly was never reported (issuer was private, M&A gap, regulatory carve-out) | `excluded_confirmed_data_gap` after evidence |
| **Over-inference** | `_infer_missing_period_ends` heuristic produces dates that don't match the issuer's actual cadence (fiscal-year-end shift, ticker reuse boundary, recent IPO with sparse history) | Validator-semantics defect; fix `_infer_missing_period_ends` |

This spec **does NOT change `_infer_missing_period_ends`**. The
dual-source-evidenced exclusion is a defensive layer for the
source-unavailable case. Over-inference defects must be fixed at the
inference layer (separate spec arc; not authorized here).

The spec REQUIRES that the implementation PR include a sentinel test:
*if `_infer_missing_period_ends` ever produces a date that the issuer's
fiscal calendar makes structurally impossible (e.g., fiscal-year-end
shift after that date), the test reds CI.* This prevents the new
exclusion from masking inference defects.

## 8. Edge cases

| Case | Handling |
|---|---|
| **SPAC-merger Q1 (e.g., AEVA 2021-03-31)** | SEC HAS the period; FMP doesn't. SEC fallback would yield 1 row, evidence row would be `outcome='yielded'`, period does NOT qualify for exclusion → row enters `fundamentals_quarterly` normally, ticker PASSES |
| **Annual filers (20-F, 40-F)** | Same logic; just uses annual cadence. Dual-source evidence required. The `_NEW_LISTING_GRACE_ANNUAL_DAYS` already exists |
| **Recent filers (< grace window)** | The existing `< 2 filings + within grace → PASS silently` path is preserved. The extension only fires for `evaluated_routed` cohort (≥ 2 filings) |
| **Ticker reuse boundary** | The new ticker_history substrate (PR #444+) can intersect: if `_infer_missing_period_ends` produces a date BEFORE the current issuer's `lifetime_start`, that's evidence of inference crossing a reuse boundary. **Recommendation**: implementation PR's inference layer is amended to clamp `period_end_date >= classification.lifetime_start`. Out of scope for THIS spec; recorded as a pre-condition for the implementation PR |
| **ARDT physical_truth anomaly** (PR #432 leftover) | The 5 FMP rows rejected by `physical_truth` gate during §12.2 do NOT enter the evidence substrate (the safety mechanism worked; FMP returned rows that were structurally rejected). ARDT remains in FAIL until the underlying FMP issue is triaged |
| **AGPU `asset_class='spac'`** | The handler's universe filter excludes SPACs from SEC fallback. Until reclassified, AGPU cannot accrue SEC evidence → cannot qualify for exclusion → stays in FAIL until SPAC merger completes or operator reclassifies (separate triage) |

## 9. Row vs ticker vs period-level

**Period-level exclusion.** A ticker may have 5 inferred missing
periods; 3 may be confirmed-unavailable (dual-source evidence) and 2
not yet attempted. The 3 contribute to `excluded_confirmed_data_gap_evidenced`;
the 2 contribute to the ticker's FAIL count.

Tickers move to PASS only when **all** inferred missing periods are
covered (by data OR by evidence). This preserves the per-ticker
granularity of the existing validator output.

## 10. CheckResult reporting

| Field | Change |
|---|---|
| `CheckResult.failed` | List of `FailureDetail` rows; **shrinks** as periods move to `excluded_confirmed_data_gap` |
| `CheckResult.observed`, `.expected` | String summaries; preserved unchanged |
| New sub-counter (logged, not in `CheckResult`) | `excluded_confirmed_data_gap_evidenced` |

Dashboard impact: any consumer reading `excluded_confirmed_data_gap`
will see counts grow as evidence accrues. The
`fundamentals_quarterly_completeness` panel in the operator dashboard
SHOULD render the new sub-counter prominently. **Implementation PR
includes the dashboard surface change** so excluded rows are
visible-and-attributable, not silently hidden.

## 11. How to avoid hiding real data gaps

Four defenses:

1. **Freshness gate** (§4 #3): evidence > 180 days old is stale; the
   row falls back to FAIL until re-attempted. Encourages periodic
   re-population so sources that backfill are detected.
2. **Operator-facing surfacing** (§10): the sub-counter
   `excluded_confirmed_data_gap_evidenced` is logged at completion
   AND rendered on the dashboard. Excluded rows are not invisible.
3. **Audit trail**: each excluded row has rows in
   `fundamentals_period_source_evidence` that name the attempt
   timestamps + source IDs. Reversible via DELETE + validator re-run.
4. **Sentinel tests** (§12.2 of THIS spec): the implementation PR
   includes tests asserting that (a) the `< 2 filings` sparse-ticker
   path is preserved unchanged, (b) the new evidenced path requires
   BOTH providers + freshness + non-fetch-failure, (c) a fixture row
   that meets only the FMP attempt does NOT route to exclusion.

## 12. DATA_OPERATIONS_COMPLETE impact

The validator's PASS/FAIL outcome gates `DATA_OPERATIONS_COMPLETE`.
Currently the 144 per-ticker FAILs from `fundamentals_quarterly_completeness`
block the gate. After this spec ships:

1. **Implementation PR + evidence backfill stage**: a one-shot
   operator-on-demand stage `confirmed_data_gap_evidence_populator`
   runs the FMP cascade + SEC fallback against every currently-FAILing
   `(ticker, period_end_date)`. Idempotent. Live writes — operator-
   authorized only.
2. **Post-populate validator re-run**: rows with dual-source-empty
   evidence move to `excluded_confirmed_data_gap_evidenced`. FAIL
   count drops.
3. **Expected post-populate state**: FAIL count drops from 144 to
   ≤ ~5 (the SPAC-merger Q1 cohort + the 1-period AEVA case + any
   newly-fetched SEC hits). The exact number is empirical — the spec
   does NOT pre-commit.

**`DATA_OPERATIONS_COMPLETE` becomes achievable** once the
`fundamentals_quarterly_completeness` check reaches PASS (which
requires the FAIL count to be 0). If after evidence-population the
residual is > 0 but reflects truly source-unavailable periods, the
operator can EITHER:

- Accept the residual and route the remaining tickers to manual triage
  (e.g., via `excluded_dark` with explicit note), OR
- Tighten the freshness gate to force re-attempts (defensive), OR
- Reframe via a separate validator-semantics arc (e.g., per-ticker
  "issuer existed but quarter unrecoverable" annotation).

**This spec is silent on the third move** — it surfaces the evidence
infrastructure and lets the next operator decision pick the disposition.

## 13. Pre-exclusion gate

No row may move into `excluded_confirmed_data_gap_evidenced` without:

1. The new `platform.fundamentals_period_source_evidence` table existing
   (migration in implementation PR).
2. Dual-source rows present for the `(ticker, period_end_date)` tuple,
   each ≤ 180 days old, both `outcome IN ('empty', 'extract_none')`.
3. No `outcome='fetch_failure'` row in the freshness window for either
   provider.

**The implementation PR ships the migration, the validator wiring, AND
the evidence-populator stage in ONE coherent PR.** Splitting risks the
validator reading from an empty substrate and producing inconsistent
verdicts between operator-runs.

## 14. Non-goals

- **No threshold loosening.** The validator's PASS gate stays
  exactly at "all routed-eligible periods have data or evidence".
- **No global exclusion.** Every excluded row carries explicit
  dual-source evidence; tickers cannot be excluded in bulk.
- **No `_infer_missing_period_ends` change.** Over-inference is a
  separate defect class; this spec is silent on inference fixes.
- **No live SEC fallback run** authorized by this spec PR.
- **No cleanup / quarantine / delete** of `fundamentals_quarterly`
  rows. The exclusion mechanism is read-only-from-the-validator's-POV.
- **No AGPU reclassification.** Recorded as a separate deferred
  follow-up.
- **No PR for the AEVA single-hit row.** The 1-period SPAC-merger Q1
  pattern is captured in the spec narrative but not separately specced
  here.

## 15. Open operator decisions

These do not block this spec PR — they are research items for the
implementation PR:

1. **Freshness window**: 180 days proposed. Operator may choose 90
   days (more aggressive re-attempt) or 365 days (less FMP/SEC
   traffic). Empirical floor TBD by first live populate.
2. **Dry-run population**: does the dry-run path populate evidence,
   or only live? Spec recommends live-only.
3. **Evidence backfill cadence**: one-shot operator-on-demand initially.
   Future: daily background top-up via `data_repair_service`? Out of
   scope here.
4. **Inference clamp**: should `_infer_missing_period_ends` clamp to
   `lifetime_start` of the current `ticker_classifications` row? Spec
   notes this as a sister-defect (§8); operator decides whether to
   bundle into the implementation PR or split.

## 16. Acceptance criteria for THIS spec PR

| Gate | Result |
|---|---|
| Doc lands at `docs/superpowers/specs/2026-06-02-excluded-confirmed-data-gap-validator-semantics.md` | ✓ this PR |
| TODO.md row added under §7 Evidence-based fundamentals/lifecycle arc | ✓ this PR |
| Sentinel test pins the load-bearing claims | ✓ optional file |
| No code under `tpcore/`, `scripts/`, `platform/migrations/`, `.github/`, `.claude/`, `data/` | ✓ |
| `scripts/check_manifests.py` | OK |
| `gitleaks detect` | no leaks |

**Operator gates downstream** (NOT in this PR):

1. Operator reads this spec → PASS / REVISE.
2. On PASS → plan PR per heavy-lane §1.
3. On plan PASS → implementation PR (migration + validator wiring +
   evidence-populator stage + dashboard surface change, all coherent).
4. Implementation PR's evidence-populator stage runs operator-
   authorized live; validator re-runs; residual FAIL count reported.
