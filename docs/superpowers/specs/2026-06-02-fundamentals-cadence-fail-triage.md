# Fundamentals quarterly cadence — 144 per-ticker FAIL triage

**Status:** SPEC ONLY. No implementation. No live DB writes. No live API calls.
No code/config/runtime/trading/risk change. Heavy-lane step 3 (spec PR)
per `docs/DEV_PIPELINE_STANDARD.md` §0/§1 and `.claude/rules/heavy-lane.md`.

Drafted: 2026-06-02.
Author: data-operations track.
Owner: operator (decision gates marked below).

## 1. Verdict

**Existing code is sufficient for ~81 % of the 144-FAIL bucket.** The
canonical `scripts/ops.py --stage historical_fundamentals_quarterly`
stage (line 8233; existing on `main` since 2026-05-22) was designed
for exactly this problem class: per-ticker FMP fetch with deep
(80-quarter ≈ 20-year) history, idempotent upsert via
`FundamentalsCache._upsert_payload`, resumable via
`FUNDAMENTALS_BACKFILL_TICKER_DONE` events. Targets are read from
`tpcore.data.fundamentals_backfill.enumerate_gap_tickers`, which
delegates to `compute_fundamentals_repair_targets` — the **same
function the validator's detector consults**. Detector and healer
cannot disagree on scope by construction.

**Recommended next operator step is a live operator-run sequence
(dry-run → bounded live → full live) targeting the 117-ticker
historical-backfill bucket.** ~10 % of the 144 (15 tickers) are
validator-semantics defects that the existing stage cannot heal — a
**separate spec arc** is needed for those, **not** in this PR's scope.

**Boundary with the just-merged metadata-coverage arc (PRs #428/#429).**
The metadata-coverage structural sentinel
(`metadata_coverage_insufficient`) is **CLEARED** — `metadata_coverage_ratio`
= 5.46 % at the time of this spec. The 144 cadence FAILs are the
*designed* per-ticker FAIL signal the validator surfaces once the
structural blocker is gone. **The two arcs do not overlap.** No SEC
metadata work is needed here.

## 2. Problem statement

`tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
returns 144 per-ticker `FailureDetail` entries with
`reason="missing_period_<form>"`. The `metadata_coverage_low` sentinel
is `False` — the gate's structural blocker is no longer active; what
remains is the designed cadence-evidence signal. Distribution at the
time of the metadata-coverage arc completion (2026-06-02 05:23 UTC):

| Primary form | Count | % of 144 |
|--------------|------:|---------:|
| 10-Q         | 137   | 95.1 %   |
| 20-F         | 6     | 4.2 %    |
| 40-F         | 1     | 0.7 %    |

| Missing-period severity | Count | % of 144 |
|-------------------------|------:|---------:|
| 1 missing               | 58    | 40.3 %   |
| 2–3 missing             | 40    | 27.8 %   |
| 4–9 missing             | 23    | 16.0 %   |
| 10–19 missing           | 15    | 10.4 %   |
| 20+ missing             | 8     | 5.6 %    |

By tier: 133 tier-1, 11 tier-2.

`DATA_OPERATIONS_COMPLETE` remains blocked while any per-ticker FAIL
fires. Closing this 144-set is the next concrete step toward gate
clearance.

## 3. Evidence from repository

Files inspected (read-only):

* `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`
  — validator code, in-source docstring on metadata-coverage sentinel
  and per-cadence routing, `_evaluate` line 220+, gate ratio at
  line 404-410, `compute_fundamentals_repair_targets` at line 537.
* `tpcore/data/fundamentals_backfill.py` — `enumerate_gap_tickers`
  (line 120), `backfill_one_ticker` (line ~130 — deep FMP fetch +
  upsert + resume marker; 402 / "no usable fundamentals" classified),
  `backfill_universe` (line ~200 — resume by 30-day window, per-ticker
  failure isolation), `DEFAULT_HISTORY_LIMIT_QUARTERS = 80` (≈ 20 y).
* `scripts/ops.py::_stage_fundamentals_refresh` (line 949) — canonical
  per-week refresh; explicit "`skip_if_refreshed_within_hours=24`"
  resumability; **cannot heal historical gaps** (docstring line 1045+).
* `scripts/ops.py::_stage_historical_fundamentals_quarterly`
  (line 8233) — the matching stage for *this* problem class. In-source
  docstring acknowledges the same pattern at higher scale
  (~"285 of 1090 active T1/T2 stock tickers failing" was the
  2026-05-22 baseline).
* `tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`
  — covers the cadence math; detector/healer parity is the existing
  test invariant (will continue to hold after live runs).
* `TODO.md` lines 150-… — "Fundamentals quarterly cadence — 144
  per-ticker FAILs (NEW, 2026-06-02)" entry capturing the run-time
  inventory.
* `docs/superpowers/specs/2026-06-02-metadata-coverage-backfill-full-universe.md`
  — boundary doc (metadata-coverage arc).

## 4. Post-metadata-coverage state

For exact alignment with the moment this spec was drafted, the gate
returned (read-only re-evaluation, 2026-06-02 ~05:23 UTC):

```
evaluated_routed:              1488
excluded_dark:                 200
excluded_metadata_required:    86
excluded_confirmed_data_gap:   0
excluded_lifecycle_terminated: 6
excluded_other_form:           0
metadata_coverage_ratio:       0.0546  (5.46 %)
metadata_coverage_low:         False   ← structural sentinel OFF
per-ticker FAIL gaps:          144
```

The 1,488 `evaluated_routed` is the cadence-routed denominator. 144 /
1488 ≈ 9.7 % of routed-eligible tier ≤ 2 tickers are currently flagged
for a cadence gap.

## 5. Current validator behavior

Per the 2026-05-30 P1 rewrite (in-source docstring of the validator):

* **Cadence routing** is by `sec_document_type_primary`:
  10-Q → quarterly (`MAX_QUARTERLY_GAP_DAYS = 100`);
  {10-K, 20-F, 40-F} → annual (`MAX_ANNUAL_GAP_DAYS = 450`).
* **Per-cadence liveness windows**:
  `LIVE_WITHIN_DAYS_QUARTERLY = 120`;
  `LIVE_WITHIN_DAYS_ANNUAL = 540`.
* **Exclusion buckets** that do NOT count toward per-ticker FAIL:
  `excluded_dark`, `excluded_metadata_required` (now 86 / 1574 = 5.46 %),
  `excluded_confirmed_data_gap` (< 2 filings + past new-listing grace),
  `excluded_lifecycle_terminated` (Form 25 / Form 15 evidence),
  `excluded_other_form`.
* **Healer entry point**: `compute_fundamentals_repair_targets(pool)`
  returns `(tickers, lookback_days)`; lookback brackets the oldest
  missing period (+ `REPAIR_LOOKBACK_BUFFER_DAYS = 14`).

Empirically observed today: `compute_fundamentals_repair_targets` =
`set(_evaluate.gaps)` = 144 tickers; lookback_days = **7,200**
(~20 years; the oldest inferred-missing period is IMPP 2013-04-27).
Detector/healer parity holds.

## 6. 144-failure inventory summary

Read-only triage queried `platform.ticker_classifications` and
`platform.fundamentals_quarterly` for each of the 144 tickers
returned by `compute_fundamentals_repair_targets`. Per-ticker fields
collected: `sec_document_type_primary`, `first_public_filing_date`,
`last_filing_date`, `fiscal_year_end_month`, `issuer_lifecycle_state`,
`lifetime_end`, `country`, tier, `fq_count` (count of
fundamentals_quarterly rows), `fq_earliest`, `fq_latest`. Cross-joined
with `ev.gaps` (oldest_missing, newest_missing, n_missing, form).

No DB writes, no API calls. Pure read-only.

## 7. Failure taxonomy

Five **decision-useful** buckets — each implies a different operator
action:

| Bucket                                  | Count | %    | Operator action                                                |
|-----------------------------------------|------:|-----:|----------------------------------------------------------------|
| **A** — likely routine refresh          | 5     | 3.5 %  | May already be fixed; re-running existing weekly `fundamentals_refresh` sufficient |
| **B** — likely historical backfill      | **117** | **81.2 %** | **`historical_fundamentals_quarterly` stage — primary target of this spec**  |
| **C1** — recent-filer validator defect  | 8     | 5.6 %  | NOT backfillable; needs validator-semantics spec arc                |
| **C2** — annual-filer cadence defect    | 7     | 4.9 %  | NOT backfillable; needs validator-semantics spec arc                |
| **D** — FMP depth limit (heuristic)     | 7     | 4.9 %  | May or may not be addressable; depth-limit detection observable post-run |

Total: 144 / 144.

Classification heuristics (deterministic):

* **A**: `n_missing ≤ 3 ∧ days_since_newest_missing < 200 ∧ form = 10-Q ∧ NOT recent_filer ∧ days_since_oldest_missing ≤ 11 y`
* **B**: anything not in A/C1/C2/D
* **C1**: `form = 10-Q ∧ first_public_filing_date within last 365 d ∧ fq_count < 5`
* **C2**: `form ∈ {20-F, 40-F}`
* **D**: `days_since_oldest_missing > 11 y` (FMP `DEFAULT_HISTORY_LIMIT_QUARTERS = 80` ≈ 20 y, so 11 y is the depth-uncertain band)

Sample tickers per bucket (alphabetical first 8):

```
A : AACB, CGCT, FIGX, QSEA, QSEAU
B : ADV, AEVA, AGPU, AIDX, AKTS, ALIT, ARDT, AVX
C1: GIX, GLIBA, GLIBK, LMRI, MANE, OYSE, SHAZ, TRAX
C2: ASTL, CGNT, FRGT, IBG, IMPP, ITOC, LX
D : EVTV, FA, GLXY, LZ, SBET, VIVK, XGN
```

## 8. Routine refresh candidates (bucket A — 5 tickers)

AACB, CGCT, FIGX, QSEA, QSEAU. Recent newest_missing (within 200 d),
small n_missing (≤ 3), 10-Q form, NOT recent IPO, oldest_missing
within FMP depth. These may already have been cleared by the most
recent `fundamentals_refresh` run; if they still surface, the
`historical_fundamentals_quarterly` stage will heal them as a side
effect of running the full B-bucket sweep.

**Operator action: bundle into the B-bucket run.** No separate sequence.

## 9. Lifecycle / document-type edge cases (buckets C1 + C2 — 15 tickers)

These are **validator-semantics defects** (or at least open questions),
not refresh-fillable.

### C1 — recent-filer over-demand (8 tickers)

GIX, GLIBA, GLIBK, LMRI, MANE, OYSE, SHAZ, TRAX. All 10-Q form. Each
has `first_public_filing_date` within the last year and `fq_count < 5`.
The validator's gap-inference walks the period_end_date series and
extrapolates expected periods; for very recent IPOs with sparse
fq_count, it extrapolates back to dates the issuer didn't yet exist.

Illustrative case — **GIX** (alphabetically last C1):

* `first_public_filing_date`: 2026-03-31 (≈ 2 months ago).
* `fq_count`: 3.
* `ev.gaps['GIX']`: 18 inferred missing periods, oldest = 2021-09-29.
* GIX legally did not exist as a SEC filer before 2026-03-31. The
  validator's missing-period inference reaches back ~5 years before
  the issuer's first filing.

The healer cannot fix these. FMP / SEC do not have pre-IPO
fundamentals_quarterly rows for them to fetch — those periods are
simply not real for that issuer.

**Operator action: NEEDS_OPERATOR_DECISION** — see §17 #2. Likely
candidates for a follow-up validator-semantics spec arc that bounds
gap-inference by `first_public_filing_date`. Out of scope here.

### C2 — annual-filer cadence (7 tickers)

ASTL (40-F), CGNT, FRGT, IBG, IMPP, ITOC, LX (all 20-F). The 2026-05-30
P1 validator rewrite routes 20-F / 40-F to annual cadence
(`MAX_ANNUAL_GAP_DAYS = 450`) which **should not fire** on these
filers' filing rhythm. That they do is a candidate signal that **at
least one of**:

1. The `period_end_date` stored in `platform.fundamentals_quarterly`
   for annual filers is quarter-shaped (e.g. ASTL has 33 rows with
   missing dates like 2015-09-15, 2019-07-16 — quarter-ish, not annual);
2. The validator's annual-cadence path has an off-by-one or grace
   miscount;
3. The annual-cadence path is correctly demanding annual filings
   and these issuers genuinely missed an annual deadline.

This is **NEEDS_REPO_VERIFICATION** at the validator level — see §17 #3.
Same disposition as C1: candidate for a follow-up validator-semantics
spec arc. Not in scope here.

## 10. Validator-semantics candidates

Buckets C1 + C2 (15 tickers; 10.4 % of 144) together constitute the
validator-semantics candidate set. They are **all** read-only signals
from the existing validator; they don't require any DB or HTTP
intervention to investigate. A follow-up spec arc would:

1. Re-run the validator's annual-cadence math against the C2 cohort
   with debug logging to determine which of the three causes above is
   operative.
2. Decide whether to add a `first_public_filing_date`-bounded
   gap-inference in the validator (C1 mitigation).
3. Make any change via the heavy-lane pipeline (`tpcore/quality/validation/**`
   is on the heavy-lane trigger list).

**Not in this spec's scope.** A separate spec PR is the right path
once the B-bucket run completes and any C1/C2 tickers still surface
in the residual.

## 11. Existing-code sufficiency verdict

**Sufficient for buckets A + B + D (129 tickers; ~89.6 %).**

`scripts/ops.py --stage historical_fundamentals_quarterly` already:

* Reads its target list from `enumerate_gap_tickers` →
  `compute_fundamentals_repair_targets` (same source as the validator,
  so it will pull exactly the live 144).
* Uses `cache.backfill(symbol)` with FMP's deep limit
  (`DEFAULT_HISTORY_LIMIT_QUARTERS = 80` ≈ 20 years) — addresses both
  recent gaps (B) and old-but-FMP-reachable gaps (the bulk of D).
* Idempotently upserts via `FundamentalsCache._upsert_payload` — same
  physical-truth gate path as `fundamentals_refresh`.
* Resumable per-ticker via `FUNDAMENTALS_BACKFILL_TICKER_DONE` events
  in `platform.application_log`; 30-day window.
* Classifies upstream failures cleanly: "no usable fundamentals" →
  no-data skip; "returned 402" → premium-gated skip; everything else
  raises and logs.
* Operator-on-demand (NOT in `OPS_UPDATE_STAGES`) — explicit
  authorization required. `--param tickers=` accepts an explicit list
  override.

**Insufficient for buckets C1 + C2 (15 tickers; 10.4 %).** Those are
validator-semantics; backfilling will not change the per-ticker FAIL
because the validator's gap inference (C1) and cadence routing (C2)
produce the missing-period expectations regardless of underlying data.

No code change is needed for the B-bucket arc. A follow-up validator
spec arc handles C1 + C2.

## 12. Proposed operator path

Three steps, mirroring the metadata-coverage arc structure
(dry-run → bounded live → full live). Each step requires explicit
operator authorization per the "backfills are dry by default" rule.

### §12.1 — dry-run (operator step 1)

Goal: verify scope size + resume state + plan, no SEC / FMP calls
that result in writes.

```bash
python scripts/ops.py --stage historical_fundamentals_quarterly
```

The stage's `--param tickers=` knob is the only scope override; the
default invocation reads `enumerate_gap_tickers(pool)` and processes
all of them. The stage docstring lists `resume`, `limit`, `end_date`,
`tickers` as the operator knobs.

**Important caveat** — the stage does NOT have a `dry_run` knob (read
`scripts/ops.py:8233-8345`). The closest read-only preview is
`--param limit=0`, which **NEEDS_REPO_VERIFICATION** to confirm
treats `0` as "no work" vs "no limit". The safer dry-run is the
**ad-hoc SQL probe** captured in §6 of this spec (already executed
read-only as part of this spec's drafting). The 144-ticker inventory
+ taxonomy is the operator's dry-run signal.

**Acceptance gate**: matches §6's 144 / 5 / 117 / 8 / 7 / 7 split
or a small drift explained by `fundamentals_refresh` having run
between then and now (A-bucket tickers may have cleared).

### §12.2 — bounded live (operator step 2)

Goal: validate end-to-end FMP fetch + idempotent upsert + resume
marker emission on a small slice before committing to the full
B-bucket sweep.

```bash
python scripts/ops.py --stage historical_fundamentals_quarterly \
    --param limit=10 \
    --param resume=true
```

The `limit=10` cap processes the first 10 tickers from
`compute_fundamentals_repair_targets`. Wall-clock estimate:
~10 × 1 s FMP fetch + ~10 × 0.2 s upsert = ~12 s plus startup.

**Acceptance gates (per spec):**

1. Stage returns successfully (no `RuntimeError`); per-ticker failures
   list is empty or operator-reviewed.
2. 10 `FUNDAMENTALS_BACKFILL_TICKER_DONE` events appear in
   `platform.application_log` for this run's `run_id`.
3. `platform.fundamentals_quarterly` row count increases by ≥ 0
   (may be 0 if all 10 were B-tickers where FMP returned ≤ existing).
4. Post-run, re-run `compute_fundamentals_repair_targets(pool)` — the
   target count should drop by some amount > 0 unless all 10 were
   C1 / C2 (in which case the diagnostic stands: validator-semantics).
5. NO `IDENTITY_DIVERGENCE_INVESTIGATE` events (this surface is the
   P1b FMP-fallback signal only; should never appear from
   fundamentals backfill).

If any acceptance gate fails, **stop**. Triage before §12.3.

### §12.3 — full live (operator step 3)

Goal: process all 144 (the historical stage will skip already-done
tickers via resume markers; effectively ~134 new fetches).

```bash
python scripts/ops.py --stage historical_fundamentals_quarterly \
    --param resume=true
```

Wall-clock estimate: 144 × ~1 s FMP fetch + per-ticker upsert
overhead ≈ ~3–5 min. The stage has its own 1-hour stage-runner
timeout — well in scope.

**Acceptance gate (gate-relevant):**

1. After completion, re-run `fundamentals_quarterly_completeness`
   check. Expected: per-ticker FAIL count drops from 144 to
   ≈ **15** (the C1 + C2 residual) plus any A / B / D tickers
   that FMP refused. Less than 15 if some C1 / C2 self-cleared.
2. Failure list returned by the stage (if any) lands in
   `application_log` with `severity="WARN"` (skipped) or `"ERROR"`
   (real failure). Operator inspects ERROR list.
3. No DB-row-count regression (UPDATE / INSERT only — never DELETE).

The arc is complete when the residual matches the C1 + C2 forecast.
Anything above ~15 residual ⇒ surface for §17 #4 investigation.

## 13. Non-goals

Out of scope for this spec PR:

* Code changes anywhere (`scripts/**`, `tpcore/**`, `ops/**`,
  `platform/**`, engine packages, `.claude/**`).
* Validator-semantics changes (C1 / C2 fixes — separate heavy-lane
  spec arc).
* Migration / schema changes.
* Adding a `dry_run` knob to `historical_fundamentals_quarterly`
  (would be a small heavy-lane change; defer until §12.1's lack of
  dry-run becomes empirically painful).
* Engine / risk / trading / order-management changes.
* Memstore writes; Anthropic API calls; Docker; Railway deploy;
  admin bypass; secret-bearing files in the diff.

## 14. Safety boundaries

* The historical stage is **operator-on-demand** by construction; not
  in `OPS_UPDATE_STAGES`. No daemon / scheduler will trigger it.
* FMP fair-use respected by the existing stage's internal sleep.
* `FundamentalsCache._upsert_payload` is idempotent; re-running is
  safe.
* Resume markers prevent re-fetching completed tickers within 30 days.
* "no usable fundamentals" + "returned 402" classify as skips, NOT
  failures — these don't pollute the failure list.
* No engine / risk / order surface touched.
* Detector/healer parity is a structural invariant of the codebase
  (validator and healer share `_evaluate`) — re-confirmed empirically
  during this spec's read-only triage (set equality verified).

## 15. Test strategy

No new tests required by this spec. The existing test suite covers
the cadence math, the detector/healer parity, and the upsert
idempotency:

* `tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`
  — validator unit tests (cadence routing; metadata-coverage
  threshold).
* `tests/` family on `_stage_historical_fundamentals_quarterly` (if
  present; NEEDS_REPO_VERIFICATION — `git grep historical_fundamentals_quarterly tests/`).
* The detector/healer parity invariant continues to hold via shared
  `_evaluate`.

If the §12.3 run reveals a defect in the historical stage, the
follow-up is a plan PR + implementation PR with new hermetic tests.

## 16. Live smoke strategy

Already enumerated in §12. The §12.2 bounded-live (`limit=10`) is the
smoke; §12.3 is the actual healing run. Both are operator-on-demand.

This spec authorizes **no live runs**. §12.2 and §12.3 each require
explicit operator authorization per the standing "backfills are dry
by default" rule.

## 17. Open operator decisions

1. **Run §12.2 / §12.3 now, or treat A-bucket separately?** A-bucket
   is 5 tickers; the historical stage will absorb them at zero
   incremental cost (they're part of the 144 universe). Recommendation:
   single run. **Decision: NEEDS_OPERATOR_DECISION (low-stakes).**

2. **C1 (recent-filer) follow-up spec arc** — should it be drafted
   immediately after the §12.3 run, or batched with C2 once the
   residual stabilizes? **Decision: NEEDS_OPERATOR_DECISION** (timing
   only; both are deferrable).

3. **C2 (annual-filer) NEEDS_REPO_VERIFICATION** — investigate which
   of the three candidate causes (§9.C2) is operative. Read-only;
   can run in parallel with the §12 sequence. **Decision:
   NEEDS_OPERATOR_DECISION** (effort vs. urgency).

4. **Residual handling after §12.3** — if residual > C1+C2 forecast
   (15), what's the disposition? Suggested: any unexplained extra is
   either an FMP gating issue (a ticker that needs the premium tier)
   or a deeper validator semantics issue. **Decision: NEEDS_OPERATOR_DECISION
   per residual finding.**

5. **Dry-run-knob backport** to `historical_fundamentals_quarterly`?
   The metadata-coverage spec (PR #428) flagged the same NEEDS_REPO_VERIFICATION
   semantics for `backfill_sec_metadata`; pattern repeats here. Likely
   worth a small heavy-lane plan PR after this spec lands, OR defer
   indefinitely if the §12 sequence works cleanly without it.
   **Decision: NEEDS_OPERATOR_DECISION (defer-friendly).**

## 18. Next implementation path

* **If §12.3 runs cleanly + residual ≤ 15:** validator-semantics
  spec arc for C1 + C2; no implementation here. Update TODO.md
  marking this entry "✅ partial — backfill complete; C1/C2 residual
  spec'd separately".
* **If §12.3 residual is much higher than 15:** new triage spec arc
  to classify the unexpected residual (FMP gating, C2 cadence
  miscount, validator regression). Do NOT loosen the validator
  threshold under any circumstance — the gate is the contract.
* **If §12.2 or §12.3 surfaces a stage-side defect:** plan PR for
  `historical_fundamentals_quarterly` followed by implementation PR
  (heavy-lane: `scripts/ops.py` is on the trigger list).

The next item is **operator-side**: authorize §12.2 against the
sequence above.

---

## Post-execution result — 2026-06-02 (§12.2 empirically stopped)

Operator-authorized §12.2 ran the same evening as this spec PR (#430)
merged. The original spec body above is preserved unmodified for
auditability; this section is the empirical correction.

### Sequence executed

| step | command params                                                          | wall      |
|------|-------------------------------------------------------------------------|-----------|
| §10  | ad-hoc SQL read-only preview (144-ticker inventory + taxonomy)          | —         |
| §12.2 | `--param dry_run=false --param limit=10 --param resume=true`           | **23.5 s** |
| §12.3 | **NOT RUN** — blocked by operator `stop_if` rule (see below)            | —         |

### §12.2 result counters

* `universe_size`: 10 · `resumed_skipped`: 0 · `tickers_attempted`: 10
* `tickers_succeeded`: 10 · `tickers_failed`: 0
* `rows_written` (FMP fetch + cache.upsert calls): **226**
* `fundamentals_quarterly.total`: 183 348 → 183 352 (delta **+4** —
  all four to AACB)
* `physical_truth gate rejections`: 5 on ARDT (safety mechanism
  worked as designed — anomalous FMP rows blocked before they could
  land)
* `IDENTITY_DIVERGENCE_INVESTIGATE` events: 0
* `FUNDAMENTALS_BACKFILL_TICKER_DONE` markers: 10 (one per ticker)

### Per-ticker validator state

Pre vs. post for the first-10 cohort:

| ticker | form | pre miss | post miss | pre fq | post fq | verdict        |
|--------|------|---------:|----------:|-------:|--------:|----------------|
| AACB   | 10-Q | 3        | **2**     | 2      | **6**   | partial: −1 missing, +4 fq rows |
| ADV    | 10-Q | 9        | 9         | 32     | 32      | no change      |
| AEVA   | 10-Q | 1        | 1         | 26     | 26      | no change      |
| AGPU   | 10-Q | 1        | 1         | 40     | 40      | no change      |
| AIDX   | 10-Q | 5        | 5         | 8      | 8       | no change      |
| AKTS   | 10-Q | 1        | 1         | 6      | 6       | no change      |
| ALIT   | 10-Q | 6        | 6         | 28     | 28      | no change      |
| ARDT   | 10-Q | 23       | 23        | 22     | 22      | no change (5 rows rejected by safety gate) |
| ASTL   | 40-F | 5        | 5         | 33     | 33      | no change      |
| AVX    | 10-Q | 3        | 3         | 30     | 30      | no change      |

Total per-ticker FAIL count: **144 → 144** (delta = 0).
`metadata_coverage_low` remains `False` (the structural sentinel
cleared by PRs #428/#429 stayed off — no regression).

### Empirical finding

**The stage is mechanically correct. The source data is the problem.**
`historical_fundamentals_quarterly` fetched FMP at the 80-quarter
depth, called `cache.backfill` for every ticker, and idempotently
upserted exactly the periods FMP returned. For 9 of 10 sampled
tickers, FMP returned the same `period_end_date` set the database
already had. The "missing periods" the validator infers are absent
from FMP itself.

Mechanistically: `compute_fundamentals_repair_targets` infers
expected period_end_dates from observed cadence between known
filings. For these tickers, the validator's inference produces dates
that FMP has no record of (genuine source gaps, fiscal-calendar
edge cases, or filings that exist on SEC EDGAR but not via the FMP
endpoint).

### Verdict correction — spec §11 sufficiency

The §11 "existing-code sufficiency" verdict said: *"Sufficient for
buckets A + B + D (129 tickers; ~89.6 %)."* That is **empirically
incorrect**. Correct verdict: existing code is **mechanically
sufficient but empirically insufficient** because the FMP source
does not have the periods the validator infers. The B-bucket
("likely historical backfill", 117 / 81.2 %) is therefore reclassified
as an **FMP-unreachable historical residual**, not an FMP-fillable
gap.

### Bucket taxonomy correction

| Bucket (original spec)              | Original count | Corrected reading                                                                                              |
|-------------------------------------|---------------:|-----------------------------------------------------------------------------------------------------------------|
| A — likely routine refresh          | 5              | Mostly **recent-quarter not-yet-filed**. AACB (1 of 10 tested) showed partial progress; others may resolve as cadence catches. |
| B — likely historical backfill      | 117            | **FMP-unreachable historical residual.** Not backfillable from FMP.                                             |
| C1 — recent-filer validator defect  | 8              | Unchanged. Still validator-semantics arc.                                                                       |
| C2 — annual-filer cadence defect    | 7              | Unchanged. Still validator-semantics arc.                                                                       |
| D — FMP depth limit (heuristic)     | 7              | Folds into FMP-unreachable historical residual.                                                                 |

### §12.3 explicitly blocked

The operator's §12.2 task spec `stop_if` rule fires:
*"No improvement in routine A/B/D candidates."* The 9-of-10 unchanged
rate triggers stop. **§12.3 (full live, no cap) is NOT to be run**
without one of:

1. New evidence that a *different source* (SEC companyfacts, FMP
   stable, IEX, etc.) has periods FMP does not. The
   `_stage_sec_fundamentals_fallback` stage at `scripts/ops.py:1042`
   is the obvious next probe.
2. A validator-semantics change that reclassifies the
   FMP-unreachable residual into `excluded_confirmed_data_gap` based
   on evidence (issuer existed, quarter is genuinely unrecoverable).
3. Operator override on the `stop_if` rule with documented rationale.

### ARDT physical_truth anomaly — NEW follow-up

The §12.2 stage log line:

```
fundamentals.cache.physical_truth_rejected
  symbol='ARDT' rejected=5 accepted=20
```

…is the safety mechanism working as designed: 5 of 25 FMP-returned
rows for ARDT were blocked before insertion. These payloads are in
`application_log` for run_id
`0cdb362c-d9de-4361-afb5-a83b456975f3`. Follow-up: read the 5
rejected payloads, decide whether to escalate (FMP-side data bug
worth filing) or accept as expected long-tail.

### Next-arc items (operator-actionable, no implementation here)

1. **SEC companyfacts spike** — run `_stage_sec_fundamentals_fallback`
   scoped to 10 tickers from the unchanged cohort (ADV, AEVA, AGPU,
   AIDX, AKTS, ALIT, ASTL, AVX + 2 more). Confirms or refutes
   "different source has different periods."
2. **Validator-semantics / `excluded_confirmed_data_gap` spec arc**
   covering: (a) FMP-unreachable historical residuals, (b) C1
   recent-filer over-demand, (c) C2 annual-filer cadence, (d) ARDT
   physical_truth anomaly disposition.
3. **Do NOT loosen the validator threshold** under any circumstance —
   the gate is the contract.

### Spec status

**COMPLETE.** §10 + §12.2 executed; §12.3 explicitly blocked. The
spec body above is preserved unmodified; this section is the
correction. Next operator action is the validator-semantics /
data-gap spec arc, not another `historical_fundamentals_quarterly`
run.
