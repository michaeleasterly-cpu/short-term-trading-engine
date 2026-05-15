# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

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
