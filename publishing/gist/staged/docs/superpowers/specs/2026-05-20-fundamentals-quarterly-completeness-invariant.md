# `fundamentals_quarterly` completeness — ungameable per-ticker quarterly-cadence zero-gap invariant

- **Status:** hardened spec + implementation.
- **Date:** 2026-05-20
- **Lane:** **heavy** (`tpcore/quality/validation/` + `tpcore/selfheal/`).
- **Branch:** `selfheal/fundamentals-completeness` (off `origin/main` @ cf1f8b9 post #168).
- **Backlog ref:** TODO.md L127-249, source 2 of 5 in the P0 autonomous-self-heal backlog. **Re-ordered ahead of `earnings_events`** per expert recommendation (2026-05-20): `earnings_events` completeness uses fundamentals as the companion-table expected set, so fundamentals must land first.
- **Reference implementations:** `tpcore/quality/validation/checks/prices_daily_completeness.py` (the canonical pattern) + `tpcore/quality/validation/checks/macro_indicators_completeness.py` (the per-cadence pattern, just merged #168).

## 1. Problem

`tpcore/quality/validation/checks/fundamentals_integrity.py` (existing) validates PB/DE/revenue NULL-handling and row-level integrity. It does NOT detect missing-quarter gaps in a ticker's filing history. A company that filed Q1+Q2+Q4 but is missing Q3 passes the integrity check; the engines silently lose a quarter's signal.

This invariant closes that hole.

## 2. The invariant (ungameable, zero-tolerance)

> **For every T1/T2 stock that is currently live (most-recent filing within the last 120 days), every consecutive pair of `period_end_date` rows within its active filing range must be spaced ≤ MAX_QUARTERLY_GAP_DAYS apart. Any gap > MAX_QUARTERLY_GAP_DAYS → one or more missing quarters → FAIL.**

### 2.1 Why gap-based, not calendar-anchored

Calendar quarter-ends (Mar 31 / Jun 30 / Sep 30 / Dec 31) are NOT the universal anchor. AAPL's fiscal year ends late September; retailers often Jan 31; ag/energy frequently Feb/Aug. A calendar-anchored expected-set would false-fail every company with a non-calendar fiscal year. **Gap-based detection is universal** — every quarterly filer files ~every 92 days regardless of fiscal-year alignment.

### 2.2 The threshold (zero-knob, physical-truth derived)

`MAX_QUARTERLY_GAP_DAYS = 100`. Derivation:
- The longest calendar quarter is Q4 = 92 days (Oct 1 → Dec 31).
- Some filings land late by a few days (the SEC's 10-Q deadline is 40-45 days after period end; companies sometimes file on the deadline or just after).
- 100 days = 92 + 8-day slack. A gap > 100 days is GUARANTEED to span > 1 quarter.
- This is NOT a tunable tolerance — it is the math-derived bound. Lowering it false-fails legitimately-late filings; raising it lets a missed quarter hide.

### 2.3 Universe boundary (same as prices_daily_completeness)

- `tier <= 2 AND asset_class = 'stock'` — the engines' tradeable set; symmetric with the other completeness invariants.
- **Liveness gate**: most-recent filing within the last `LIVE_WITHIN_DAYS = 120` days. A stock that hasn't filed for >120 days is a halt/delist/private (a different failure class owned by `delistings`), not a fundamentals-ingest gap.

### 2.4 Active-range only

`[first_period_end, last_period_end]` per ticker — pre-IPO and post-delisting quarters are never demanded. This is the only legitimate exclusion (same principle as prices_daily not demanding pre-IPO bars).

## 3. Detector/healer symmetry

Shared `_evaluate(pool) -> _Evaluation` returns:
- `sentinel`: structural blocker (empty universe, etc.).
- `gaps`: `dict[ticker, list[date]]` — inferred missing period_end_dates per ticker.
- `evaluated`, `excluded_dark`.

Both `check_fundamentals_quarterly_completeness` (detection) and `compute_fundamentals_repair_targets` (healing) call `_evaluate`. They cannot disagree.

`compute_fundamentals_repair_targets(pool) -> (tickers, lookback_days)`:
- `tickers`: sorted list of tickers with at least one gap.
- `lookback_days = (today - oldest_missing_quarter_across_all_tickers).days + buffer`.

## 4. HealSpec wiring

Stage: `fundamentals_refresh` (canonical FMP re-pull stage; already exists per TODO.md L179-183).

```python
HealSpec(check_name="fundamentals_quarterly_completeness",
         source="fundamentals_quarterly",
         healable=True,
         stage="fundamentals_refresh",
         params={"skip_guard_days": "0"},
         max_attempts=2),
```

Targeted-repair semantics: the canonical stage already supports per-ticker scoping (TODO.md L179-183 references `--param …` filtering). For this candidate, the orchestrator passes the set of gap-flagged tickers; the stage re-pulls each.

## 5. Validation suite registration

Symmetric to macro_indicators (#168): import + add to `KNOWN_CHECK_NAMES` + add to `run_validation_suite`'s gather. The capital gate `EXPECTED_SOURCES` derives automatically.

## 6. Test contract (the make-or-break)

`tpcore/quality/validation/tests/test_check_fundamentals_quarterly_completeness.py`:

- **C1 (clean quarterly cadence → pass)** — 4 filings per year per ticker, ≤92-day gaps → passes.
- **C2 (single missing quarter)** — drop Q3 of one ticker → check flags exactly that gap with inferred period_end.
- **C3 (two consecutive missing)** — gap of ~184 days → check flags both inferred quarter-ends.
- **C4 (universe-boundary respected)** — tier-3 stock with a gap is NOT flagged.
- **C5 (asset_class non-stock respected)** — ETF with a gap is NOT flagged.
- **C6 (liveness gate excludes dark)** — stock that hasn't filed for 200 days → excluded, not gap-flagged.
- **C7 (pre-IPO quarters not demanded)** — ticker's first filing is 2023-03-31; check does NOT demand pre-2023 filings.
- **C8 (healer symmetry)** — `compute_fundamentals_repair_targets` returns exactly the gap-flagged tickers.
- **C9 (clean → empty targets)** — no gaps → `([], 0)`.
- **C10 (sentinel → no heal)** — empty universe → `([], 0)`.

## 7. Non-goals

- NOT changing `fundamentals_integrity.py` — the row-level integrity probe stays; this completeness invariant is additive.
- NOT pre-empting `earnings_events` completeness (the next P0 source which will use THIS check's data as its expected set).
- NOT touching `scripts/ops.py` or the `fundamentals_refresh` stage — re-pull mechanism is verified-working.
- NOT changing the bash wrapper — the new check participates in DATA_OPERATIONS_COMPLETE gating by being registered in the suite.
- NOT touching out-of-scope paths (`tpcore/risk/`, `tpcore/engine_profile.py`, `ops/engine_sdlc/`, `tpcore/lab/llm_emitter/`, `catalyst/`).

## 8. H-FQ hardening register

| ID | Risk | Mitigation |
|----|------|-----------|
| H-FQ-1 | Fiscal-year-aligned tickers (non-calendar) false-fail. | Gap-based detection is fiscal-year-agnostic — only consecutive-gap days matter. C1 fixture includes a non-calendar example. |
| H-FQ-2 | Late-filing legitimate stragglers (110-day filers) false-fail. | The 100-day bound is +8 days slack over the longest quarter (Q4=92 days). The SEC's 10-Q deadline is 40-45 days after period-end; companies file by then. 110-day gaps are not legitimate. |
| H-FQ-3 | Newly-listed ticker with only 1 filing → no gap analysis possible. | A ticker with <2 filings is `evaluated += 1` but cannot contribute a gap (no consecutive pair). Reported in summary, not a fail. |
| H-FQ-4 | Liveness gate hides recent delistings. | Same precedent as prices_daily — `excluded_dark` count surfaced in the result, never hidden. |
| H-FQ-5 | Healer over-pulls (full-universe refresh when only 5 tickers missing). | Target list passed to stage as comma-sep param (canonical pattern). Bounded by `max_attempts=2`. |
| H-FQ-6 | Performance — 178k rows × universe filter. | Single SQL with `JOIN liquid USING (ticker)` + `JOIN vol USING (ticker)` + window scoping → returns only T1/T2 live universe (~1000 tickers, ~40 filings each = ~40k rows max). |

## 9. Acceptance criteria

- New check fails on synthetic gap fixture.
- New check passes on current live DB (smoke at PR-prep).
- `tpcore/selfheal/registry.py` carries the new HealSpec.
- `KNOWN_CHECK_NAMES` includes `fundamentals_quarterly_completeness`.
- All 10 C1-C10 tests pass.
- Whole-suite + ruff + check_imports green.

## 10. Sequencing for the P0 backlog

After this lands:
- **Source 3 (earnings_events)** — uses fundamentals_quarterly.period_end_date as the companion-table expected set; ingestion-side NO_BEAT sentinel; one PR per the expert's note.
- **Source 4 (sec_insider_transactions)** — SEC EDGAR-derived; per-filer-cluster pattern.
- **Source 5 (corporate_actions)** — shrinkage detector + heal.

The 5-source sequence is now: macro (✅ #168) → fundamentals (this PR) → earnings → sec → corp_actions.
