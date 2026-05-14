# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## Platform integrity

- **Make pipeline-test non-destructive.** `ops/platform_pipeline.py --force`
  (and any other "test the full pipeline now" path) currently writes real
  partial data when run mid-session: `ops.py --update --force` invokes the
  `daily_bars` stage which pulls Alpaca's intraday bars for the more-liquid
  tickers and INSERTs them into `platform.prices_daily` for today's date.
  Verified destructive 2026-05-14: a single `--force` run during market
  hours wrote 474 corrupted today-rows that had to be `DELETE`'d manually
  before the scheduled 21:30 UTC daemon fire could run cleanly. The engine
  sweep half of the pipeline can also fire real paper-trading orders.
  Fix sketch: `platform_pipeline.py` should accept `--dry-run` and forward
  it to `ops.py --update --dry-run` (the stage runner already returns
  `DRY_RUN` status without invoking the handler — non-destructive by
  construction at the stage level); when `--dry-run` is set, also skip
  the engine sweep entirely (or extend each engine scheduler to honor
  `--dry-run` if not already). Goal: `platform_pipeline.py --dry-run
  --force` becomes the canonical "verify the wire path works during
  market hours" command with zero side effects on `prices_daily`,
  `open_orders`, `aar_events`, or Alpaca.

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
