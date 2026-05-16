# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## WEEK GOAL (2026-05-16): Data layer finalization + hardening

Single focus until further notice — no engine/Sigma-redesign work. Sequence:

1. **(blocking, in flight)** SEC backfill completes → re-measure catalyst +
   SEC 180d coverage vs thresholds → verdict held to the bar (our-defect-
   until-proven-per-ticker; no vendor-blame; threshold reframe only with
   evidence the gap is not ours).
2. Roll the 7 detection-only sources to `healable` — one bounded targeted
   repair + HealSpec flip each (fundamentals, corp_actions, catalyst, SEC,
   macro, liquidity_tiers, classifications), gated per the 6-stage contract.
3. Drive validation to 13/13 green; prove `python -m tpcore.selfheal`
   returns green end-to-end (the deferred live e2e).
4. **Hardening pass** (some items NOT blocked on the verdict — run in
   parallel while SEC backfills):
   - `prices_daily_gaps` audit check: close the 14-day-recency blind spot
     (old un-backfilled liquid holes invisible).
   - sporadic `row_velocity`: tighten (currently only fires on total
     silence; misses sustained severe partial degradation).
   - FMP handler-path CSV archive: verify end-to-end (presence unproven).
   - ✅ **HY-spread recovery — DONE 2026-05-16.** ALFRED/Nasdaq ruled
     out empirically; full history recovered (eco-archive 1996-2021 +
     Scribd FRED-graph gap, validated 772/772 exact). `hy_spread`
     contiguous 1996→present, re-activated as a maintained
     `INDICATOR_SERIES` member (FRED rolling window keeps tail fresh).
     BAA10Y also still maintained. Research spike RESOLVED.
     **Deferred (held by operator):** the HY→Sentinel Bear-Score
     scoring switch — original was binary HY>5%; current is graduated
     BAA10Y. Requires backtest-derived HY-OAS graduated thresholds
     before going live. NOT done; awaiting explicit go + validation.
   - then the tracked `catalyst→earnings` rename (below).

## Rename: `catalyst_*` → `earnings_*` (tracked, DEFERRED behind data layer)

**Decision (operator, 2026-05-16): the rename WILL happen — but only
AFTER the data layer is fully squared away. Do not start it before
then; just be aware it is coming and don't entrench the misnomer.**

`platform.catalyst_events` / the `catalyst_refresh` stage / the
`catalyst_events_freshness` check / the new selfheal `HealSpec`
(`source="catalyst_events"`) are all misnamed. Verified empirically
2026-05-16: the table holds exactly ONE `event_type` — `EARNINGS_BEAT`
(13,848 rows / 1,104 tickers, source 100% `fmp`). It is **earnings-beat
events only** — no M&A / FDA / guidance / analyst / news / insider.
"Catalyst" is aspirational for a future general catalyst engine that
does not exist; today the only consumer is the **Vector** engine.

Why it matters beyond cosmetics: the `catalyst_events_freshness`
threshold ("≥20% of T1/T2 with an event in 180d") reasons as if this
were a broad catalyst stream; it is quarterly earnings *beats*. The
misnomer actively causes threshold-reasoning confusion (relevant to the
pending catalyst coverage verdict).

Scope when unblocked: rename table (idempotent migration), the stage,
the validation check + `KNOWN_CHECK_NAMES`, the selfheal `HealSpec`
source, the audit_pipeline check, and every Vector consumer — in one
PR, all six pipeline stages kept in lockstep. Honest name candidates:
`earnings_events` / `earnings_beats`. Document the column/table rename;
never drop data.

## Autonomous self-heal — EVERY data source (P0, 2026-05-15)

**Mandate (operator, verbatim intent):** "100% data, no gaps, no
bullshit, runs on its own — I cannot babysit this." This applies to the
WHOLE data layer, not just daily bars. The 2026-05-15 build delivered
true end-to-end auto-heal for `prices_daily` ONLY (zero-tolerance
completeness invariant + Step-4 auto-heal loop in
`run_data_operations.sh`). Every other source is currently
*detected + hard-gated* (red blocks the emit / engine sweep) but
*escalates to the operator* instead of self-healing. That residual
babysitting is unacceptable per the mandate — close it.

**Scope — bring each source to the same bar as `prices_daily`:**
1. **`fundamentals_quarterly`** (FMP) — define the ungameable
   completeness/correctness invariant (every addressable T1/T2 stock has
   the expected filed quarters within its active range, no missing
   period), then an auto-heal path via the canonical
   `ops.py --stage fundamentals_refresh --param …` (no one-off script).
2. **`corporate_actions`** (Alpaca) — invariant + auto-heal via the
   canonical corp-actions stage; shrinkage detector already exists,
   wire it into the heal loop.
3. **`catalyst_events`** (FMP) — completeness invariant + auto-heal via
   `catalyst_refresh`.
4. **`sec_insider_transactions` / SEC filings** (EDGAR) — invariant +
   auto-heal via `ops.py --stage sec_filings --backfill`.
5. **`macro_indicators`** (FRED) — invariant + auto-heal (re-pull); the
   BAMLH0A0HYM2 truncation class must self-recover.
6. **`liquidity_tiers`, `ticker_classifications`** — invariant +
   auto-heal/recompute.

**ARCHITECTURE MANDATE (binding — the shape, not negotiable):**
Self-heal is a GENERIC `tpcore` capability, NOT per-source bash.
1. **One self-heal orchestrator in `tpcore`**, beside the validation
   suite (detector + healer in the same layer). Input: the suite
   result. Per red check → dispatch to the registered healer for that
   source → bounded retry → re-validate → escalate if exhausted or
   unhealable. Pure Python, unit-testable with fake healers.
2. **Each data feed contributes only a declarative `HealSpec`**:
   {invariant = the existing validation check; canonical repair =
   which `ops.py --stage X --param …`; is-auto-healable; bounded
   retry/backoff policy}. Adding a source = registering a spec —
   ZERO bash edits, zero new branches.
3. **Heal executes ONLY via the canonical `ops.py --stage` infra.**
   The orchestrator INVOKES it; it never reimplements ingestion. No
   one-off scripts. (Standard: data_adapter_pipeline.md.)
4. **Every HealSpec is BOUNDED/targeted.** Proven 2026-05-15: a
   whole-universe `force_refresh` exceeds the 3600s stage timeout and
   can never self-heal. Targeted repair only (the `repair_gaps`
   pattern: re-pull just the invariant-flagged tickers/window).
5. **Detector/healer symmetry.** The healer's target set is computed
   from the SAME code as the check (cf. `_evaluate` shared by
   `check_prices_daily_completeness` + `compute_gap_repair_targets`)
   so they can never disagree.
6. **Process concerns stay in the bash wrapper, thin:** never emit
   `DATA_OPERATIONS_COMPLETE` unless 100% green; self-exclusion lock;
   post-close/`tpcore.calendar` gating. `run_data_operations.sh`
   becomes a thin caller of the tpcore orchestrator.
7. **`prices_daily` is the reference implementation, migrated INTO
   the orchestrator** — not a bash special case. One canonical
   mechanism, no N variants (operating-identity: symmetry/standard).

**Per-source design constraints (within the architecture above):**
- Each invariant is ungameable: physical-truth, zero-tolerance, no
  recency window, no percentage knob. Scoped to exactly the data the
  engines depend on.
- Honest heal only: a source's HealSpec must actually be able to fix
  that source's failure class. No dishonest cross-source "heal";
  not-bars-fixable → escalate, never fake-green.
- **No lazy vendor-blame.** A shortfall on authoritative data (SEC
  EDGAR especially) is OUR ingestion defect until proven per-ticker
  against the source. Threshold recalibration only after the our-gap
  hypothesis is empirically killed.
- Each source's required tickers registered where the freshness check
  can see them; add/retire the matching `audit_pipeline.py` check in
  the same change.

This is the path to the operator never touching data again. Until every
item above is done, the "runs on its own" mandate is only partially met
and that must be stated plainly, not glossed.

## Engine structural redesign (post-2026-05-15 sweep)

The 2026-05-15 parameter sweeps validated the targeted fixes (Sigma SPY-
regime filter, Reversion Z-relaxation + T3 expansion) at the metric level
but DSR/credibility gates remain structurally blocked.

- **Sigma structural redesign.** 2026-05-15 sweep with regime filter
  applied: 80% of walk-forward Sharpe rows are negative (-3.265 to
  +1.454, median -0.666). The regime filter eliminated the −0.84
  parameter-stability swing — that win is real — but the underlying
  range-scalping signal is fragile across most market windows. Held-back
  +0.839 Sharpe / 86 trades / credibility 50 / DSR 0.0000. The next
  experiment is NOT more parameter sweeps. Candidate redesigns: (a) shift
  from band-touch entries to band-mean-reversion confirmations (require
  close back inside band before entry); (b) require explicit volatility-
  contraction prerequisite (BB-width percentile rank < N before entry);
  (c) abandon range-scalping for trend-pullback if the market structure
  is fundamentally different from the 2018-2023 calibration window.
  Decision deferred until operator picks a redesign path.
  **OU mean-reversion gate spike — rejected 2026-05-15.** Tested as one
  candidate redesign path; 50-trial walk-forward sweep showed the gate
  cut more trades in stable windows than fragile ones, regressing held-
  back Sharpe +0.839 → +0.366. Code archived in
  `tpcore/backtest/spread_estimator_archive.py`.

  **PATH PICKED 2026-05-16 — HMM regime classifier (QUEUED, NOT
  STARTED; deferred behind the WEEK GOAL data-layer work per operator
  decision).** This is Sigma's final test before permanent retirement.
  Full spec captured so it is executable without re-derivation when the
  data layer closes:
  - Phase 1: `tpcore/indicators/hmm_regime.py` — shared
    `HMMRegimeClassifier` (hmmlearn `GaussianHMM(n_components=2,
    covariance_type='full', n_iter=100)`), fit on SPY 252d returns
    rolling; label lower-return-autocorr state = mean-reverting;
    expose `is_mean_reverting` bool. Params: `lookback_days=252`,
    `retrain_frequency_days=21`, `require_confirmations=3`. Unit-test
    on synthetic OU(κ=5)=mean-reverting vs RW-with-drift=trending.
  - Phase 2: `sigma/plugs/setup_detection.py` — replace static
    ADX<20∧CHOP>38.2 gate with the HMM classifier behind
    `--use-hmm-regime`; keep static gate behind `--use-static-regime`
    for baseline. Sweep params: hmm_lookback {126,252,504},
    hmm_retrain_freq {10,21,42}, hmm_confirmations {1,2,3,5}.
  - Phase 3: `search_parameters.py --engine sigma --trials 100`
    (train 2018-01-01, holdout→2023-12-31, final-holdout 2024-01-01→
    2025-12-31) → `backtests/sigma_hmm_sweep_results.csv`. ~2-4h.
  - Phase 4 decision (ZERO ambiguity): adopt iff any config achieves
    **credibility ≥ 60 AND DSR ≥ 0.95 on held-back**; else Sigma is
    permanently archived (`sigma/`→`archive/sigma/`, removed from
    `run_all_engines.sh` + the selfheal/smoke loops + docs).
  - Add `hmmlearn` to `pyproject.toml` deps.
  - **Constraints carried from this review:** the archive/adopt
    *execution* is operator-confirmed at decision time, NOT an
    automatic script side-effect (engine retirement is structural).
    Default `--use-hmm-regime` OFF until the sweep adjudicates; all
    existing tests + ruff + check_imports stay green throughout.
    Tension acknowledged: the prior "next experiment is NOT more
    parameter sweeps" note stands for *static* gates — HMM is a
    classifier *redesign* (different object than the rejected OU
    gate), which is why the operator picked it as the path.

- **Reversion — reclassified as satellite 2026-05-15 (closed).** The
  signal-class-redesign decision was resolved by reclassifying Reversion
  as a satellite engine alongside S2: permanent 5–10% capital cap,
  per-trade graduation criteria, DSR gate retired. The combined filter
  (Z ≥ 3.0 + HIGH earnings quality) produces 19 trades / Sharpe +0.312
  / PF 1.755 / max DD −11.5% on 2018-2025 — strong per-trade metrics at
  a structurally bounded firing rate. See `docs/MASTER_PLAN.md` §4.2 and
  `backtests/reversion_satellite_backtest.json`.

## Data archival — CSV-first retrofit (DONE 2026-05-15)

**Closed.** The 2026-05-15 BAMLH0A0HYM2 incident exposed that the
CSV-first sub-protocol was implemented for only one handler. Rather
than patch FRED alone, all five ingest handlers were retrofitted to a
shared archive layer.

**Shipped:**
- `tpcore/ingestion/csv_archive.py` — shared write + gzip + shrinkage
  detection. 8 unit tests including the BAMLH0A0HYM2 truncation
  scenario (7,500 → 785 rows → `over_threshold=True`).
- All 5 handlers write a gzipped CSV archive before/after the DB
  upsert: `handle_macro_indicators`, `handle_daily_bars`,
  `handle_corporate_actions`, `handle_fundamentals_refresh`,
  `_stage_catalyst_refresh`.
- **Shrinkage detection** (the vendor-truncation alarm) is wired into
  the two *full-snapshot* sources only — `fred_macro` and
  `alpaca_corporate_actions` — which re-pull their entire history every
  run, so a row-count drop unambiguously means truncation. The three
  *incremental* sources (`alpaca_daily_bars`, `fmp_fundamentals`,
  `fmp_catalyst_events`) pull a variable window each run, so row-count
  shrinkage there is noise — they get the audit-trail archive but no
  alarm (a full-table baseline would false-flag their next incremental
  run; this was caught and corrected during the build).
- `scripts/dump_baseline_archives.py` — seeds baseline snapshots for
  the two full-snapshot sources so shrinkage detection has a real
  predecessor from run 1. Run once 2026-05-15.

**Compliance-matrix re-grade — DONE 2026-05-15.** The `fred` row in
`docs/superpowers/pipelines/data_adapter_pipeline.md` now rests on the
real CSV-first implementation (the ✅ previously sat on the
"trivial first pull" carve-out, which the BAA10Y backfill invalidated).
Matrix audit note + FRED row + cross-cutting summary updated. Section
fully closed — no remaining items.

## Publishing

- **Publish a GitHub gist of the entire project.** Scope: everything —
  architecture (`docs/MASTER_PLAN.md`), database + dataflow
  (`docs/DATABASE_AND_DATAFLOW.md`), operations (`docs/OPERATIONS.md`),
  style guide, engine specs (Sigma, Reversion, Vector, Momentum) with
  credibility scorecards, parameter-search methodology + walk-forward +
  held-back DSR, 5-plug architecture, FilterDiagnostics + baseline-
  equivalence framework, dashboard, the Railway/Supabase ops story.
  Public-facing — review for any embedded keys, paths, or PII before
  publishing.
- **Publish to PyPI.** Open scope — decide what gets packaged. Most likely
  candidate: `tpcore/` as a standalone library (RiskGovernor, AAR,
  parity, backtest harness, filter diagnostics, baseline-equivalence) —
  the parts that are genuinely reusable outside this repo. Engines
  (`sigma/`, `reversion/`, `vector/`, `momentum/`) and `platform/`
  schema stay private. Prereqs: pick a name (likely not `tpcore` —
  reserved/generic), pin a license, add `pyproject.toml` package
  metadata, set up `python -m build` + `twine upload`, decide on
  versioning scheme. Same key/PII review as the gist.
