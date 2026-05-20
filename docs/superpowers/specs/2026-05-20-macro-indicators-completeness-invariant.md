# `macro_indicators` completeness — the ungameable per-cadence zero-gap invariant

- **Status:** hardened spec + implementation (one-PR ship per lean cadence — `feedback_cut_process_overhead_ship`).
- **Date:** 2026-05-20
- **Lane:** **heavy** — touches `tpcore/quality/validation/` (data-acceptance gate) + `tpcore/selfheal/` (registry).
- **Branch:** `selfheal/macro-completeness` (off `origin/main` @ `72f6051`).
- **Backlog ref:** `TODO.md` L127-249 "Autonomous self-heal — EVERY data source (P0)" — `macro_indicators` is one of the **5 P0 sources** whose ungameable completeness invariant is still open. The re-pull half is already wired (`tpcore/selfheal/registry.py:124-126` — `macro_indicators_freshness → healable=True, stage=macro_indicators`). This spec closes the *invariant* half.
- **Reference implementation:** `tpcore/quality/validation/checks/prices_daily_completeness.py` — the canonical pattern. This invariant follows it exactly: shared `_evaluate`, zero-tolerance, no recency window, no percentage knob, detector/healer symmetry.

## 1. Problem

`tpcore/quality/validation/checks/macro_indicators_freshness.py` answers *"is the latest row of each indicator newer than `MAX_AGE_DAYS=90`?"* — it's a recency probe with a tolerance knob. It does **not** detect *gaps inside* a series's active history.

Concrete failure class the freshness check is structurally blind to: the **2026-05-15 BAMLH0A0HYM2 (hy_spread) truncation incident** — FRED started serving a rolling 3-year window, dropping the pre-1997 tail. Freshness stayed GREEN (latest date was current), but the series was suddenly missing **20+ years of mid-range observations**. Re-ingested 2026-05-16 from the eco-archive, but only because a human noticed.

Per `TODO.md` mandate: *"100% data, no gaps, no bullshit, runs on its own — I cannot babysit this."* and the operator memory `feedback_no_lazy_vendor_blame` — *"authoritative-source shortfall is OUR ingestion defect until proven per-ticker against the source"*. The truncation class **must self-detect** + self-heal.

## 2. The invariant (zero-tolerance, ungameable, per-cadence)

> **For every expected indicator, given its known publication cadence, there must be a row for EVERY expected publication date in `[first_observed_date, latest_observed_date]`. One missing `(indicator, date)` → the check FAILS.**

Every clause is a *principled boundary*, not a tolerance knob:

### 2.1 Per-indicator cadence (the cadence map)

FRED publishes each series on a stable, documented cadence. Direct observation of the live DB (2026-05-20) confirms each series's row count is consistent with its cadence × range — none of these are knobs, they are physical truths about each FRED series:

| Indicator             | FRED ID         | Cadence | Rule for "expected date" |
|----------------------|-----------------|---------|--------------------------|
| `vix`                | VIXCLS          | DAILY   | every NYSE session in [first, latest] |
| `yield_curve`        | T10Y2Y          | DAILY   | every NYSE session in [first, latest] |
| `credit_spread`      | BAA10Y          | DAILY   | every NYSE session in [first, latest] |
| `hy_spread`          | BAMLH0A0HYM2    | DAILY   | every NYSE session in [first, latest] |
| `initial_claims`     | ICSA            | WEEKLY  | every Thursday in [first, latest] |
| `industrial_production` | INDPRO       | MONTHLY | first day of every month in [first, latest] |
| `sahm_rule`          | SAHMREALTIME    | MONTHLY | first day of every month in [first, latest] |

The cadence map is a **module-level constant**, not a runtime configurable. Adding/removing a series is an explicit code edit + test update — it cannot accidentally bypass the invariant.

Rationale for each cadence:
- **DAILY** indicators (`vix`/`yield_curve`/`credit_spread`/`hy_spread`) — derived from market-traded instruments; publish every NYSE session. The 9k-9.2k row counts over 36 years align with ~252 business days × 36 ≈ 9072. Use `tpcore.calendar.sessions_in_range` (XNYS) — the same authority `prices_daily_completeness` uses.
- **WEEKLY** initial_claims — DOL releases every Thursday morning; 1,897 rows over 36 years ≈ 1,872 expected Thursdays. Use ISO calendar Thursday in range.
- **MONTHLY** industrial_production + sahm_rule — published on/near month-start; ~435 rows over 36 years × 12 = 432 expected. Use first day of each month in [first, latest].

### 2.2 Why per-cadence is the right partition

A single global cadence would either be (a) too strict (claiming a daily row for a monthly series — false failures every day) or (b) too loose (claiming a monthly row for a daily series — gaps of weeks would pass). The FRED-published cadence is the *physical truth* about each series; matching the invariant to it is the only correct partition.

### 2.3 Within-active-range only (the legitimate exclusion)

`[first_observed_date, latest_observed_date]` is per-indicator, computed from the actual data. Expected dates *outside* a series's observed range are NOT demanded — that's the same principle as `prices_daily_completeness` not demanding pre-IPO bars. This is the only legitimate exclusion; everything inside the range is mandatory.

### 2.4 Expected indicator set is closed

`EXPECTED_INDICATORS = ("vix", "yield_curve", "credit_spread", "hy_spread", "initial_claims", "industrial_production", "sahm_rule")` — same 7 as the freshness check. If FRED later adds a new series, that's a new constant in the cadence map + an explicit test update.

A missing *indicator entirely* (zero rows) is a structural sentinel — distinct from a gap, returned via `FailureDetail(reason="indicator_missing", …)` so the operator knows the failure class.

## 3. Detector/healer symmetry (the SP-S invariant)

Shared `_evaluate(pool) -> _Evaluation` returns:
- `sentinel`: structural blocker (zero rows for any expected indicator, calendar broken).
- `gaps`: `dict[indicator, list[date]]` — missing dates per series, within its own active range.
- `evaluated`: count of indicators evaluated.

Both `check_macro_indicators_completeness` (detection) and `compute_macro_repair_targets` (healing) call `_evaluate`. They cannot disagree — same code.

`compute_macro_repair_targets` returns `(indicators, lookback_days)`:
- `indicators`: sorted list of indicators with non-empty gaps.
- `lookback_days = (today - oldest_missing_across_all_indicators).days + buffer`.

A structural sentinel returns `([], 0)` — bars-backfill cannot fix it, escalate instead.

## 4. HealSpec wiring

The existing HealSpec for `macro_indicators_freshness` already routes to stage `macro_indicators` with `{skip_guard_days: 0}` (`tpcore/selfheal/registry.py:124-126`). The new `HealSpec` for `macro_indicators_completeness`:

```python
HealSpec(check_name="macro_indicators_completeness",
         source="macro_indicators",
         healable=True,
         stage="macro_indicators",
         params={"skip_guard_days": "0"},
         max_attempts=2),
```

Per the architecture mandate (TODO.md L207-211), the new spec is **declarative-only** — no bash edits. The orchestrator reads the registry, dispatches to `python scripts/ops.py --stage macro_indicators --param skip_guard_days=0` (the canonical re-pull stage), bounded retry, re-validate.

Targeted-repair semantics (TODO.md L215-218): for `macro_indicators` specifically, the `macro_indicators` stage is already a small fixed set (7 series), and re-pulling all 7 is the same shape as the existing freshness-heal. There is no per-ticker subset to scope — the universe IS the 7 series. The bounded-retry contract (max_attempts=2) prevents loops. This is **not** a full-universe-vs-targeted-subset distinction (that's `prices_daily`-specific); the equivalent here is "re-pull just the macro_indicators stage, NOT the broader weekly_data_refresh meta-stage." That's already what the existing HealSpec does — preserved.

## 5. Validation suite registration

Three edits to `tpcore/quality/validation/suite.py`:
1. Import `CHECK_NAME as MACRO_COMPLETENESS_NAME, check_macro_indicators_completeness`.
2. Add `MACRO_COMPLETENESS_NAME` to `KNOWN_CHECK_NAMES` (it derives `EXPECTED_SOURCES` in the capital gate — `tpcore/quality/validation/capital_gate.py:35` — so the new check participates in `DATA_OPERATIONS_COMPLETE` gating by construction).
3. Add `await _run_check(MACRO_COMPLETENESS_NAME, check_macro_indicators_completeness, pool, None)` in `run_validation_suite`.

The capital gate's frozen-anchor literal (if any) updates in the same PR.

## 6. Test contract (the make-or-break)

`tpcore/quality/validation/tests/test_macro_indicators_completeness.py` — synthetic asyncpg `_FakePool` per the existing test idioms:

- **C1 (cadence per indicator):** for each cadence class (DAILY/WEEKLY/MONTHLY), construct a fixture with no gaps → check returns `passed=True, failed=0`.
- **C2 (DAILY gap):** insert all expected sessions except one for `vix` → check returns `passed=False`, that one date in `failures[0].observed`.
- **C3 (WEEKLY gap):** insert all expected Thursdays except one for `initial_claims` → check fails on that Thursday.
- **C4 (MONTHLY gap):** insert all expected month-starts except one for `industrial_production` → check fails on that month-start.
- **C5 (missing indicator entirely):** zero rows for `hy_spread` → check returns `passed=False`, sentinel-style `reason="indicator_missing"`.
- **C6 (within-active-range only):** indicator starts at 2020-01-01; check does NOT demand pre-2020 dates.
- **C7 (truncation class — the BAMLH0A0HYM2 case):** indicator with gap of months mid-range → check flags every missing date in the gap.
- **C8 (healer symmetry):** `compute_macro_repair_targets` returns exactly the indicators with gaps + a lookback that brackets the oldest missing date.
- **C9 (no-gap → empty targets):** clean state → repair targets returns `([], 0)`.
- **C10 (sentinel → no heal):** missing indicator entirely → repair targets returns `([], 0)` (can't fix a structural failure via re-pull alone).

The HealSpec-registry-coverage test (existing — `tpcore/selfheal/tests/test_registry.py` if present, else the consistency test in `tpcore/tests/`) will auto-fail until the new HealSpec is added — that's the architecture-mandate invariant (TODO.md L211).

## 7. Non-goals

- **NOT** changing the existing `macro_indicators_freshness` check — the recency probe stays; the completeness invariant is additive.
- **NOT** changing the canonical `macro_indicators` stage in `scripts/ops.py` — re-pull mechanism is verified-working (`tpcore/selfheal/registry.py:124-126`).
- **NOT** touching any out-of-scope path (per the operator prompt): `tpcore/risk/`, `tpcore/engine_profile.py`, `ops/engine_sdlc/`, `tpcore/lab/llm_emitter/`, `catalyst/`, `ops/llm_lab_emitter.py`.
- **NOT** changing the bash wrapper (`scripts/run_data_operations.sh`) — the new check participates in `DATA_OPERATIONS_COMPLETE` gating automatically by being registered in the suite.

## 8. H-MI (hardening register)

| ID | Risk | Mitigation |
|----|------|-----------|
| H-MI-1 | Cadence map drifts from FRED's actual publication schedule. | The cadence map is the explicit code constant; a new series or cadence change is an explicit PR + test update. The expected-set test (count of expected vs row count within range) is a near-direct sanity check. |
| H-MI-2 | `tpcore.calendar` returns no sessions → false PASS. | Sentinel `FailureDetail(reason="no_sessions", …)` — matches the prices_daily pattern. |
| H-MI-3 | Cross-cadence false positive (e.g. demanding a daily row from a monthly series). | The cadence map is per-indicator; check dispatches on the indicator's cadence, never globally. C1-C4 tests pin each cadence class independently. |
| H-MI-4 | Healer re-pulls wrong scope. | `compute_macro_repair_targets` returns indicator subset; canonical stage already re-pulls all 7 series (no smaller scope possible per the stage's design). Bounded by max_attempts=2. |
| H-MI-5 | Truncation incident class re-occurs and freshness stays GREEN. | This is the *exact* failure mode this invariant catches — the C7 truncation test pins it. |
| H-MI-6 | New FRED series added but completeness check forgets it. | The closed set `EXPECTED_INDICATORS` is identical to the freshness check's set; the consistency test (existing) catches drift between the two if any future change adds to one but not the other. |
| H-MI-7 | Performance — daily series × 36 years × 7 indicators = ~50k rows in the SQL. | `prices_daily_completeness` evaluates 30 sessions × 1000+ liquid tickers = ~30k window cells with no perf issue. This is the same scale or smaller. |

## 9. Acceptance criteria

- The new check fails on a synthetic truncation fixture (the BAMLH0A0HYM2 class).
- The new check passes on the current production DB state (verified by smoke run at PR-prep time).
- `tpcore/selfheal/registry.py` carries the new HealSpec.
- `KNOWN_CHECK_NAMES` includes `macro_indicators_completeness`.
- All 10 C1-C10 tests pass.
- Whole-suite single-process pytest + reversed-module-order GREEN.
- ruff + check_imports clean.
- CI green on the build PR.

## 10. Pointers (canonical SoT)

- `tpcore/quality/validation/checks/prices_daily_completeness.py` — the template, copied + adapted (NOT cargo-culted).
- `tpcore/selfheal/registry.py` — HealSpec registry.
- `tpcore/quality/validation/suite.py` — suite registration + `KNOWN_CHECK_NAMES`.
- `.claude/rules/selfheal-auditheal.md` — the architecture mandate (generic capability, not per-source bash).
- `.claude/rules/heavy-lane.md` — heavy-lane pipeline applies.
- `TODO.md` L127-249 — the canonical autonomous-self-heal backlog.

## 11. Self-review

- **Placeholder scan:** no `TODO/TBD/???/<…>`; every cadence pinned; constants module-level.
- **Internal consistency:** `EXPECTED_INDICATORS` is the same set as the freshness check (§2.4); the cadence map covers every entry.
- **Scope:** only touches `tpcore/quality/validation/checks/` (new file), `tpcore/quality/validation/suite.py`, `tpcore/selfheal/registry.py`, new test file. Zero edits to out-of-scope paths (§7).
- **Detector/healer symmetry:** shared `_evaluate`; tests C8-C10 enforce it.
- **Honest heal:** healer re-pulls via the canonical stage; not-fixable (structural sentinel) → empty targets → escalate. No fake-green path.
