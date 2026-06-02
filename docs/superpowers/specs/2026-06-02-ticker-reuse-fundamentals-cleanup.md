# Ticker-reuse fundamentals cleanup

**Status:** SPEC ONLY. No implementation. No DB writes. No live API calls.
Follows the FPFD repair closeout (PRs #436 + #437) which left a
6,016-row residual that is **not** FPFD-correctable. Validator stays
strict; this spec does **not** authorize any deletes.

Drafted: 2026-06-02. Owner: operator. Author: SEC-metadata /
data-quality / issuer-history track.

## 1. Verdict

**The 6,016 residual rows are ticker-reuse artefacts**, not validator
defects and not FPFD-extraction defects. The right disposition is
**evidence-gated row-level cleanup** of `platform.fundamentals_quarterly`
using the **existing** `platform.issuer_history` + `platform.issuer_securities`
+ `platform.ticker_history` substrate already populated by
`backfill_sec_metadata` + `corp_history_edgar_backfill` + the
issuer-lifecycle work. **No new identity model is required.**

Recommended pattern: **delete-with-archive into `data_quality_log`**
for *high-confidence* ticker-reuse rows (the issuer-different evidence
is dispositive). **Quarantine** (move-to-sidecar) for *ambiguous*
rows where the predecessor identity is plausible but not provable.
**Block** rows where the evidence is mixed.

**No row touches in this PR.** Implementation is a separate plan PR +
implementation PR with its own dry-run / bounded-live / full-live
acceptance gates.

## 2. Problem statement

After the FPFD extractor repair shipped (PRs #435 / #436 / #437) and
the 240 moved-earlier cohort was repaired live, the
`fundamentals_quarterly.period_end_date < ticker_classifications.first_public_filing_date`
inventory dropped from 8,633 rows → **6,016 rows across 783 tickers**.

This residual is **structurally different** from the FPFD-extraction
bug class:

* For these 783 tickers, the issuer's actual first SEC filing **is**
  the recorded FPFD. Re-running the extractor produces the same value.
* The pre-FPFD rows in `fundamentals_quarterly` correspond to periods
  *before* the issuer existed as a SEC filer under this CIK + ticker
  combination.
* Therefore those rows belong to a **previous issuer** that held the
  same ticker symbol — classic ticker reuse.

The cleanup is a **row-level identity-correction pattern**, not an
FPFD-correction pattern. The mutation is `DELETE` (with archive) or
`UPDATE ticker = <predecessor>` (re-key to a tombstoned predecessor
ticker), not `UPDATE first_public_filing_date`.

## 3. Residual inventory

Read-only triage (2026-06-02, ~10:00 UTC, post-PR #437 + 240-cohort repair):

| Severity bucket (pre-FPFD rows per ticker) | Tickers | Total rows |
|--------------------------------------------|--------:|-----------:|
| 1                                          | 74      | 74         |
| 2–3                                        | 111     | 267        |
| 4–9                                        | **432** | **2,536**  |
| 10–19                                      | 97      | 1,255      |
| 20+                                        | 69      | 1,884      |
| **Total**                                  | **783** | **6,016**  |

Top-15 most-affected (by pre-FPFD row count):

```
GLXY  47  FPFD=2025-03-31  range=2006-03-31 → 2024-12-31
SBET  44  FPFD=2024-03-31  range=2007-12-31 → 2023-12-31
MH    40  FPFD=2025-06-30  range=2015-06-30 → 2025-03-31
JBS   39  FPFD=2025-12-31  range=2016-03-31 → 2025-09-30
CAI   38  FPFD=2025-06-30  range=2013-03-31 → 2025-03-31
LAC   36  FPFD=2025-03-31  range=2015-12-31 → 2024-12-31
UNIT  36  FPFD=2025-03-31  range=2016-03-31 → 2024-12-31
EU    36  FPFD=2025-03-31  range=2016-03-31 → 2024-12-31
FERG  35  FPFD=2024-10-31  range=2016-01-31 → 2024-07-31
CAMP  34  FPFD=2024-09-30  range=2016-05-31 → 2024-06-30
MDXH  33  FPFD=2021-12-31  range=2004-06-30 → 2021-06-30
NEUP  33  FPFD=2024-09-30  range=2016-06-30 → 2024-06-30
RTO   33  FPFD=2022-12-31  range=2006-06-30 → 2022-06-30
BLK   33  FPFD=2024-09-30  range=2016-06-30 → 2024-06-30
ELE   32  FPFD=2025-12-31  range=2017-09-30 → 2025-09-30
```

The recurring "FPFD ≈ 2024-09-30 ± 1 year" pattern across multiple
mega-caps (BLK, FERG, RTO, NEUP, CAMP) is a **second-order signal**
that **some of these residual tickers might still have an FPFD-extraction
issue** — the FPFD repair fixed the dominant "recent-shard floor" class
but didn't catch every long-lived issuer. **Spec §16 #1 calls this out
as a NEEDS_REPO_VERIFICATION**: before any deletes, re-check whether
the top-20 mega-caps in this list have a *correctable* FPFD vs.
genuine ticker-reuse.

## 4. Ticker-reuse evidence

Evidence sources, ranked by authority:

| Rank | Source                                                       | What it proves                                                   |
|-----:|--------------------------------------------------------------|------------------------------------------------------------------|
| 1    | **SEC `formerNames[]`** for the current CIK                  | The current issuer **never** used the ticker historically       |
| 2    | **`platform.issuer_history`** (populated by `corp_history_edgar_backfill`) | Per-CIK name timeline including predecessor names    |
| 3    | **`platform.issuer_securities`** (issuer ↔ classification mapping) | Time-bounded ticker ↔ issuer ownership                  |
| 4    | **`platform.ticker_history`**                                | Ticker symbol timeline (predecessor mappings if loaded)         |
| 5    | **`platform.corporate_events`** (rename / ticker_swap / share_class_collapse) | Event-shape evidence of identity transitions  |
| 6    | **CIK in `fundamentals_quarterly`** (if recorded per-row)    | Whether the row's CIK matches the current `ticker_classifications.cik` (if FMP recorded a `cik` per filing) |
| 7    | **SEC EDGAR direct lookup** for the row's `filing_date` / `period_end_date` | What CIK + name actually filed that period for that ticker — gold-standard but requires per-row HTTP |

**The dispositive evidence is rank 1 + 2 + 3 combined**: if the current
CIK's submissions JSON has no `formerNames[]` entry covering the
pre-FPFD period AND `issuer_history` shows no issuer-history row for
this CIK before the FPFD AND `issuer_securities` shows the ticker
attached to a *different* `issuer_id` before the FPFD, the row is
**high-confidence ticker reuse** and is safe to delete.

The **weak-evidence cases** are tickers where `issuer_history` and
`issuer_securities` are sparse (the issuer-lifecycle backfill hasn't
fully populated them for that CIK) and the only signal is "pre-FPFD".
Those rows should be **quarantined**, not deleted.

## 5. Authority hierarchy (per-row classification)

For each pre-FPFD `(ticker, period_end_date)` row, classify by checking
each rank above in order until a dispositive answer is found:

```
def classify_row(ticker, period_end_date, current_cik, current_fpfd):
    # Rank 1: SEC formerNames[] — was the current CIK using this
    # ticker at period_end_date?
    if sec_formernames_cover(current_cik, period_end_date):
        return "weak_evidence_keep"   # current issuer might have used it
    # Rank 2: issuer_history for the current CIK
    if issuer_history_covers(current_cik, period_end_date):
        return "weak_evidence_keep"
    # Rank 3: issuer_securities — was THIS ticker on a DIFFERENT
    # issuer_id at period_end_date?
    other_issuer = issuer_securities_at(ticker, period_end_date)
    if other_issuer is not None and other_issuer != current_issuer_for(current_cik):
        return "high_confidence_ticker_reuse"
    # No dispositive signal — quarantine, do not delete.
    return "ambiguous"
```

Each per-row classification produces one of three dispositions:
`high_confidence_ticker_reuse` (delete with archive),
`ambiguous` (quarantine), `weak_evidence_keep` (keep, mark in
`data_quality_log` for operator review).

## 6. Delete vs re-key vs quarantine options

| Option | Mechanism | Pros | Cons |
|--------|-----------|------|------|
| **A — Hard DELETE**            | `DELETE FROM platform.fundamentals_quarterly WHERE ...`           | Simplest; smallest schema impact | Irreversible; no audit-trail without explicit log entry |
| **B — DELETE + archive**       | Move row to a new `platform.fundamentals_quarterly_archive` sidecar before DELETE | Reversible; full audit trail | Requires new table (migration) |
| **C — Re-key to predecessor**  | `UPDATE fq SET ticker = '<predecessor>'` if the predecessor is known | Preserves historical data; queryable as predecessor-ticker series | Requires predecessor-ticker resolution (which we may not have for most cases); creates new data-quality-validation surface for the predecessor records |
| **D — Quarantine sidecar**     | Move row to `platform.fundamentals_quarterly_quarantine` (no validator visibility) | Reversible; non-destructive; operator can inspect | New table (migration); rows remain in storage but excluded from validator + engine views |
| **E — Soft-delete flag**       | Add `is_deleted` column to fundamentals_quarterly | Reversible; one-column migration | Adds is-deleted complexity to every downstream consumer (validator, engines, backtest) |

**Recommended: B for high-confidence rows + D for ambiguous rows.**

Option E (soft-delete flag) is the wrong shape because it would force
every consumer (validator, backtest, dashboards) to add
`WHERE is_deleted IS NOT TRUE` clauses — high blast radius for low
benefit. The DELETE + archive (B) + quarantine (D) approach keeps the
production table clean while preserving auditability + reversibility.

Option C (re-key) is theoretically sound for the small subset where
the predecessor ticker is known (via `ticker_history` or
`corporate_events.ticker_swap`), but in practice the predecessor is
unknown for most ticker-reuse cases. It can be a NEEDS_OPERATOR_DECISION
sub-option for the small subset where the predecessor *is* known.

## 7. Recommended cleanup strategy

Three parallel buckets, each with its own disposition path:

| Bucket | Trigger | Action | Schema additions |
|--------|---------|--------|------------------|
| **HC** (high-confidence ticker reuse) | rank-3 evidence in §5 fires | Move row to `fundamentals_quarterly_archive`, then DELETE; log to `data_quality_log` with `disposition='archived_ticker_reuse'` | `fundamentals_quarterly_archive` table (mirror schema + `archived_at` + `disposition_reason`) |
| **AMB** (ambiguous)              | No dispositive evidence; rank-2 sparse | Move row to `fundamentals_quarterly_quarantine`; log to `data_quality_log` with `disposition='quarantine_pending'` | `fundamentals_quarterly_quarantine` (mirror schema; can be promoted back if predecessor identity becomes available) |
| **WK** (weak-evidence keep)       | Rank-1 or rank-2 evidence says current CIK *might* have used this | KEEP row; write `data_quality_log` row with `disposition='operator_review'` | none |

The validator continues to read `fundamentals_quarterly` AS-IS (no
JOIN to archive/quarantine). Validator-visible state for affected
tickers post-cleanup:

* HC tickers: pre-FPFD rows gone → `period_end_date < FPFD` invariant
  holds → no cadence FAIL from this signal.
* AMB tickers: same as HC (rows removed from main table; visible only
  via quarantine sidecar).
* WK tickers: rows remain; they stay in the pre-FPFD inventory; the
  operator must triage manually via `data_quality_log`.

## 8. Safety invariants

This spec authorizes NO mutations. The future implementation arc must
preserve all of:

1. **Validator stays strict** — no filter, no threshold change, no
   exclusion bucket. (Per PR #435 §1 hard rule.)
2. **Archive-before-DELETE** — no row leaves
   `fundamentals_quarterly` without a corresponding insert into the
   archive table within the same transaction.
3. **No mass DELETE** — every DELETE is per-row with explicit
   `(ticker, period_end_date, current_cik_at_decision_time)` recorded
   in `data_quality_log`.
4. **dry_run-by-default** — the new stage knob defaults to
   `dry_run=true`; operator must pass `dry_run=false` explicitly.
5. **No predecessor re-key without confirmed evidence** — Option C
   from §6 is opt-in per-ticker only.
6. **Rollback path exists** — every archived/quarantined row can be
   restored to the main table from the sidecar within the same
   transaction shape (INSERT … SELECT FROM archive WHERE id=$1).
7. **Audit replayable** — every disposition row in `data_quality_log`
   carries the evidence used (which rank fired, the evidence query
   result hash, the `decided_at` UTC).
8. **No engine surface change** — engines read
   `fundamentals_quarterly`; that contract is preserved.

## 9. Dry-run manifest design

The dry-run stage produces a **manifest CSV** (NOT a DB write) with one
row per `(ticker, period_end_date)` candidate:

```
ticker,period_end_date,current_cik,current_fpfd,proposed_disposition,evidence_rank_used,evidence_summary
BLK,2016-06-30,0001364742,2024-09-30,weak_evidence_keep,1,sec_formernames=[...]; current_cik used ticker since 1999-09-30
GLXY,2006-03-31,0001972922,2025-03-31,high_confidence_ticker_reuse,3,issuer_securities@2006-03-31: different issuer_id; prior issuer GLXY held by issuer_id=I_xxx until 2009-12-31
...
```

The manifest is written to `data/fundamentals_quarterly_cleanup_manifest_<UTC>.csv`
(operator-local; gitignored). Operator inspects before authorizing the
live run.

Per-row manifest fields:

| field | description |
|-------|-------------|
| `ticker`                  | Row's ticker                              |
| `period_end_date`         | Row's period_end_date                     |
| `current_cik`             | Current `ticker_classifications.cik`      |
| `current_fpfd`            | Current `first_public_filing_date`        |
| `proposed_disposition`    | `high_confidence_ticker_reuse` / `ambiguous` / `weak_evidence_keep` |
| `evidence_rank_used`      | First rank in §5 that produced a dispositive answer |
| `evidence_summary`        | Per-rank diagnostic (truncated to 200 chars) |

The manifest itself does **not** touch the DB. The bounded-live stage
(§10) reads the manifest, re-validates each row against the live evidence
substrate, then performs the archive + delete per row.

## 10. Bounded-live strategy

Same `dry_run → bounded_live → full_live` pattern that worked for the
FPFD repair (PRs #435–#437):

| step | command shape                                                                                 | scope |
|------|-----------------------------------------------------------------------------------------------|-------|
| §10a | Read-only dry-run → manifest CSV                                                              | All 6,016 rows |
| §10b | Bounded live: process the smallest cohort first — the **74 single-row** tickers (1-row severity bucket) | 74 rows |
| §10c | Bounded live: 2–3 severity bucket (267 rows)                                                  | 267 rows |
| §10d | Full live: all remaining (4–9 / 10–19 / 20+ buckets — 5,675 rows)                              | 5,675 rows |
| §11  | Re-audit + re-classify any remaining `weak_evidence_keep` tickers                              | Manual operator triage |

Each step's acceptance gates:

* `dry_run=false` reflected in the stage payload.
* Exactly the targeted cohort's rows are mutated; zero rows outside
  the cohort touched.
* For each row moved out of `fundamentals_quarterly`: a matching row
  exists in the archive / quarantine table.
* `data_quality_log` carries one row per decision with evidence
  summary.
* No `IDENTITY_DIVERGENCE_INVESTIGATE` events.
* No validator semantic change.
* `prices_daily_completeness` zero-tolerance invariant untouched.

Stop conditions:

* Manifest classification rate < 80 % on `high_confidence_ticker_reuse`
  → evidence substrate is too sparse; abort and run
  `corp_history_edgar_backfill` (operator-on-demand) first.
* Any row's evidence query returns a different verdict on re-check
  during bounded-live → flag for operator review, do not mutate.
* Any row's `current_cik` changes between manifest generation and
  bounded-live execution → drop the row from the run.

## 11. What must remain blocking

The following cases remain **uncleaned** until separate triage:

| case | reason |
|------|--------|
| `weak_evidence_keep` rows (rank 1 / 2 hit)         | Current CIK plausibly used the ticker; cannot delete without further evidence. |
| Tickers where `issuer_history` has 0 rows           | `corp_history_edgar_backfill` hasn't populated; must run that first or escalate. |
| Tickers with `issuer_lifecycle_state` = `'deregistered'` / `'delist_effective'` | Already excluded by validator's `excluded_lifecycle_terminated`. Cleanup unnecessary — the validator already handles them. |
| The 14 mega-caps named in §3 (BLK / FERG / RTO / NEUP / CAMP / …) whose FPFD pattern looks suspect | NEEDS_REPO_VERIFICATION first — they may be FPFD-extraction defects, not ticker reuse. |
| Rows where `current_cik` IS NULL                    | No way to anchor the current-issuer identity check; skip until CIK is populated. |

## 12. Tests required (future implementation PR)

Hermetic tests in `tests/test_ticker_reuse_cleanup.py` (new file):

1. **`evidence_rank_1_formernames_keeps_row`** — SEC `formerNames[]`
   covers period_end_date → `weak_evidence_keep`.
2. **`evidence_rank_2_issuer_history_keeps_row`** — `issuer_history`
   row for current CIK covers period_end_date → `weak_evidence_keep`.
3. **`evidence_rank_3_different_issuer_owns_ticker`** —
   `issuer_securities` shows ticker on a different `issuer_id` at
   period_end_date → `high_confidence_ticker_reuse`.
4. **`no_evidence_returns_ambiguous`** — none of ranks 1–3 fire →
   `ambiguous` (NOT auto-deleted).
5. **`high_confidence_archive_roundtrip`** — DELETE from main +
   INSERT into archive; restore round-trip succeeds.
6. **`ambiguous_quarantine_roundtrip`** — same shape, quarantine
   sidecar.
7. **`weak_evidence_writes_data_quality_log_only`** — `data_quality_log`
   row written; main table unchanged.
8. **`dry_run_writes_manifest_no_db_mutation`** — manifest CSV
   produced; `fundamentals_quarterly.total` unchanged.
9. **`bounded_live_respects_scope`** — only the requested ticker /
   period rows are touched; out-of-scope rows untouched.
10. **`rollback_from_archive_restores_state`** — archive → main
    UNDO succeeds; validator sees the row again.

Plus integration sentinels in `tests/test_backfill_sec_metadata_stage.py`:

11. **`fundamentals_quarterly_total_invariant_under_dry_run`** —
    full dry-run leaves `fundamentals_quarterly.total` unchanged.

## 13. Open operator decisions

1. **Spec §3 NEEDS_REPO_VERIFICATION**: top-15 / top-20 affected
   tickers (BLK / FERG / RTO / NEUP / CAMP / MDXH / …) have suspiciously
   recent FPFDs. Before any deletes, re-run a per-ticker FPFD probe:
   what does the bulk reader produce for these specific CIKs? If a
   re-run produces a materially-earlier FPFD, they're FPFD-extraction
   defects (handle via the FPFD arc, not this one). If FPFD is genuinely
   stable, they're real ticker reuse. **Decision: NEEDS_OPERATOR_DECISION**
   (highest-priority — affects bucket sizes).

2. **`corp_history_edgar_backfill` re-run**: the evidence substrate
   for §5 ranks 2 + 3 depends on `issuer_history` + `issuer_securities`
   being populated. Should we run `_stage_corp_history_edgar_backfill`
   (bulk SEC submissions.zip walk; ~3 min wall) before generating the
   §9 manifest, or trust the current population state?
   Recommendation: re-run; it's cheap insurance. **Decision: NEEDS_OPERATOR_DECISION.**

3. **Schema choice — archive table column set**: should
   `fundamentals_quarterly_archive` mirror `fundamentals_quarterly`
   1-to-1 plus `(archived_at, disposition_reason, decided_by_run_id)`,
   or carry only the load-bearing audit columns? Recommendation:
   1-to-1 mirror (cheapest restore path; storage is essentially
   free for ~6k rows). **Decision: NEEDS_OPERATOR_DECISION** at the
   migration plan PR.

4. **Quarantine table or `is_quarantined` column**: same shape
   question for the `ambiguous` cohort. Recommendation: sidecar
   table — same precedent as `archive`. **Decision: NEEDS_OPERATOR_DECISION.**

5. **Re-key (Option C) for the small confirmed-predecessor subset**:
   `ticker_history` / `corporate_events.ticker_swap` may identify
   a known predecessor ticker for some of the 783. Should those rows
   be **re-keyed** to the predecessor ticker (preserving the data
   under its rightful symbol) rather than archived? Recommendation:
   per-ticker operator decision; default to archive unless predecessor
   is explicitly named. **Decision: NEEDS_OPERATOR_DECISION** per
   identified-predecessor case.

6. **`weak_evidence_keep` triage cadence**: these rows stay in
   `fundamentals_quarterly` and will keep firing the
   `period_end_date < FPFD` signal. Should the spec set a target
   triage cadence (e.g. weekly operator review) or accept that they're
   a known long-tail until the FPFD-extraction-defect re-audit
   (decision #1) settles? Recommendation: defer cadence-setting
   until #1 is resolved. **Decision: NEEDS_OPERATOR_DECISION.**

## 14. Non-goals (out of scope for this spec)

* Any change to `fundamentals_quarterly` schema, contents, or constraints.
* Any change to the validator.
* Any new FPFD extraction work (the FPFD arc closed in PRs #435–#438).
* Any change to the engine / risk / order / broker surface.
* Memstore writes; Anthropic API calls; Docker; Railway deploy;
  admin bypass; secret-bearing files in the diff.

## 15. Next item

If this spec merges: draft the **plan PR** for the implementation,
which:

1. Defines the migration shape for
   `fundamentals_quarterly_archive` + `fundamentals_quarterly_quarantine`.
2. Defines the new `scripts/ops.py` stage knob
   `cleanup_ticker_reuse_residual` (or similar).
3. Defines the §9 manifest CSV schema authoritatively.
4. Resolves the §13 open operator decisions.
5. Names the exact subagent/dispatch path for the implementation PR.

No row touches until the plan PR is merged AND the operator's §13 #1
NEEDS_REPO_VERIFICATION resolves cleanly.
