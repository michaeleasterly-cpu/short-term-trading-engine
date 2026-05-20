# Orphan-script audit (2026-05-20)

Master step 4 engine-lane follow-up. Audits every script flagged with
`TODO(P5): migrate to ops.py stage or remove` in
`scripts/tests/test_no_orphan_scripts.py::_ALLOWLIST`.

**Total flagged:** 10 (operator memory said ~13; the actual TODO(P5)
count in `_ALLOWLIST` is 10 — the other allowlist entries
`agent_pr_label_guard`, `gen_engine_manifest`, `audit_code_duplication`
are deliberate standalone tools, NOT P5-track orphans).

**Classification breakdown:** 0 deleted | 4 migrate-to-stage |
4 keep-as-ops-helper | 0 keep-as-test-fixture | 2 delete-safe (but
operator-gated)

Per the audit instructions' conservative-deletion rule ("Better to leave
13 in the orphan list than to delete one that turns out to be
load-bearing"), this PR executes ZERO deletions. Every entry below
includes a concrete recommendation; the operator adjudicates migrations
and the two delete-safe picks in a follow-up.

## Method recap

For each script: read the file head, ran `git log --oneline scripts/<name>.py`,
grepped for the exact path token `scripts/<name>.py` across `*.py`/`*.sh`/`*.yml`/`*.yaml`/`*.toml`/`*.plist`
(excluding `.git`/`.venv`/`.claude`/`data`/`*_archive`/`__pycache__`),
and grepped for the bare stem in code + docs to surface prose mentions.
Cross-checked `dashboard.py`/`dashboard_components/`, `pyproject.toml`,
`.github/workflows/ci.yml`, every `scripts/*.sh` wrapper, and every
`install_launchd_*.sh` shell installer. **No `.plist` files exist in
the tree.** No launchd installer references any of the 10 scripts.

---

## Deleted in this PR (0 scripts)

None. Conservative-deletion rule applied: when in doubt, catalog
rather than delete.

## Delete-safe candidates (operator decision) — 2 scripts

These two meet the strict DELETE criteria — zero genuine callers, no
live wiring, no launchd, no test, no docs as an active harness — and
are functionally superseded. The operator should rubber-stamp these
two in a follow-up PR (or veto if there's hidden reasoning).

### `scripts/extract_tradier.py` (699 lines, scripts/test_no_orphan_scripts.py:163)
- One-shot Tradier extractor scoped to the 50-name backtest universe.
  Pulls option chains / corporate calendars / pre-2020 bars / company
  profiles to CSV in `data/tradier_export/`. No DB writes.
- **Superseded by:** `scripts/extract_tradier_full.py` (the wide
  universe extractor; its own docstring calls itself a "companion to"
  the 50-name version). The 50-name CSV's role was absorbed when the
  full-universe extractor shipped — `docs/session-log.md:42` shows the
  full export (22M rows, 7,710 tickers) was completed and ingested via
  `ingest_tradier_csv.py`, with no follow-up reference to the 50-name
  output.
- **Last activity:** 2026-04-19 `b1d9236` "ops: one-shot Tradier
  extractor → CSV (no DB)" (the lint-pass touch `dd67972` was a
  repo-wide ruff drift cleanup, not functional). Effectively cold for
  ~6 weeks.
- **Callers grep:** 0 in code/launchd/CI/pyproject. The only mentions
  are self-mentions in `extract_tradier_full.py:3,64` ("Companion to
  scripts/extract_tradier.py" — prose only) and `EDGE_VALIDATION_PLAN.md`
  which references **only** `extract_tradier_full.py`, not this 50-name
  variant.
- **Recommended action:** `git rm scripts/extract_tradier.py` and
  remove the `extract_tradier` `_ALLOWLIST` entry +
  `test_no_orphan_scripts.py:158-163` TODO comment block in the same
  follow-up PR.

### `scripts/test_aar_pipeline.py` and `scripts/test_kill_switch.py` — see "Keep as ops helper" below

(Listed under helper, not deletion — these have live OPERATIONS.md
documentation.)

---

## Migrate to ops.py stage (4 scripts) — operator decision

### `scripts/run_daily_bars_all_active.py` (60 lines, scripts/test_no_orphan_scripts.py:141-145)
- One-shot local driver that invokes `tpcore.ingestion.handlers.handle_daily_bars`
  with `{"universe":"all_active","min_price":5.0,...}` against the local
  process holding the DB pool.
- **Already superseded.** `scripts/ops.py:12` docstring explicitly says
  *"replaces the previous mix of ad-hoc scripts (run_daily_bars_all_active.py,
  run_corporate_actions_all_active.py, etc.)"*. `ops.py --stage daily_bars`
  exists at `scripts/ops.py:382 _stage_daily_bars`.
- **Last activity:** `b0b0d1e` "scripts: local driver for daily_bars
  all_active sweep" — single commit, ~3 months cold.
- **Callers grep:** 0 — only prose mentions in `ops.py:12` (in the
  "replaces…" docstring), `EDGE_VALIDATION_PLAN.md:63`, and
  `session-log.md:41`.
- **Recommended stage:** already covered by `--stage daily_bars`. The
  script is dead code.
- **Effort: S** — confirm the existing `_stage_daily_bars` covers the
  exact `min_price=5.0 / min_volume=250_000 / lookback_days=7` config,
  then `git rm` the script + drop the allowlist entry.

### `scripts/run_corporate_actions_all_active.py` (53 lines, scripts/test_no_orphan_scripts.py:136-140)
- One-shot local driver for `handle_corporate_actions` with
  `{"universe":"all_active","ingest_start":"2018-01-01"}`. Mirror of
  `run_daily_bars_all_active.py`.
- **Already superseded.** `scripts/ops.py:610 _stage_corporate_actions`
  exists; the ops.py docstring explicitly lists this script as part of
  the "replaced" set.
- **Last activity:** `1f6680b` "feat(ingestion): corporate_actions
  handler supports all_active universe" — single commit, cold.
- **Callers grep:** 0 — prose in `ops.py:13` (the "replaces" docstring)
  and `MASTER_PLAN.md:762` (historical "Complete" status note).
- **Recommended stage:** already covered by `--stage corporate_actions`.
- **Effort: S** — same as above; verify the existing stage's config
  parity, then `git rm` + drop allowlist entry.

### `scripts/compute_fundamental_ratios.py` (133 lines, scripts/test_no_orphan_scripts.py:132-135)
- Set-based UPDATE that populates `platform.fundamentals_quarterly.pb`
  and `de` columns by joining to the closing price on `filing_date`.
  Idempotent (skip rows where ratios are already filled).
- **Not yet superseded** but unambiguously a recurring maintenance step —
  every new quarterly filing pulled by FMP appears with `pb`/`de` NULL
  until this is run. Currently invoked by hand after each
  `backfill_fundamentals.py --all-active` sweep.
- **Last activity:** `dd7db40` "fix(scripts/compute_fundamental_ratios):
  set-based UPDATE + tighter validation" — actively maintained.
- **Callers grep:** 0 wiring. Prose-only references in
  `platform/migrations/versions/20260511_0000_pb_de_and_catalyst_events.py:11`
  (migration note: *"scripts/compute_fundamental_ratios.py populates
  them"*), `MASTER_PLAN.md:631`, `DATABASE_AND_DATAFLOW.md:57`,
  `session-log.md:45`.
- **Recommended stage:** new `--stage compute_fundamental_ratios`,
  natural sibling of `_stage_fundamentals_refresh` (ops.py:645). Should
  chain after `fundamentals_refresh` in `OPS_UPDATE_STAGES` so the
  ratios get populated automatically after each FMP refresh.
- **Effort: M** — wrap the existing `UPDATE_SQL` and the `--force`
  argparse switch into an `async def _stage_compute_fundamental_ratios(pool, config)`,
  add the stage spec to `_STAGE_SPECS`, add to
  `OPS_UPDATE_STAGES` after `fundamentals_refresh`, update the migration
  docstring's prose reference. ~2 hours.

### `scripts/backfill_backtest_universe.py` (145 lines, scripts/test_no_orphan_scripts.py:124-127)
- One-shot 2018-2025 daily-bars backfill for a hardcoded 50-name
  backtest universe via direct Alpaca API calls. Sleeps 0.3s per symbol
  for rate-limit politeness.
- **Functionally superseded** by `ops.py --stage daily_bars` (the
  all-active sweep) — every name in `DEFAULT_UNIVERSE` is in
  `prices_daily` now. BUT the script has a **documented
  source-of-truth role**: `ops/cron_corporate_actions.py:48-50` comment
  reads *"50-name backtest universe — kept in sync with
  `scripts/backfill_backtest_universe.py:DEFAULT_UNIVERSE`. Hardcoded
  rather than imported because `scripts/` is intentionally not part of
  the installed package"*. **Deleting the script would orphan that
  comment.**
- **Last activity:** `0a89270` "feat(data): finish prices_daily
  ingestion + run CHOP backtest" — single commit, cold.
- **Callers grep:** 0 wiring; the prose mention above is a
  documentation cross-reference, not an import.
- **Recommended action:** rather than migrating to a stage, **promote
  the `DEFAULT_UNIVERSE` constant to `tpcore/backtest/universe.py`** and
  import it from `ops/cron_corporate_actions.py` (fixing the comment's
  rationale: scripts/ is not on path, but `tpcore.backtest` is). Once
  the constant is moved, the script's only justification is gone and
  it can be deleted.
- **Effort: M** — extract constant + update import site + verify the
  daily_bars stage covers 2018-2025 history for the 50 names (the
  `all_active` sweep should already have it). ~1 hour.

---

## Keep as ops helper (4 scripts) — un-orphan with docstring polish

These are legitimate operator-on-demand tools that don't fit the
`ops.py --stage X` model: each is an ad-hoc diagnostic / live-DB
verification harness that's invoked by hand when a specific question
comes up.

### `scripts/test_aar_pipeline.py` (186 lines, scripts/test_no_orphan_scripts.py:146-150)
- **Documented operator harness** at `docs/OPERATIONS.md:1188-1196`
  ("Verification Scripts" §10). Proves `AARWriter.write_aar` persists
  to `platform.aar_events` against the **live database** (synthetic
  `engine='synthetic_test'` + UUID trade_id + idempotent cleanup
  in a `finally` block). Used "any time a 'the wiring exists but the
  table is empty' question comes up".
- Also referenced in `MASTER_PLAN.md:722` ("AAR persistence: …Pipeline
  verified end-to-end against the live database via
  `scripts/test_aar_pipeline.py`") and `audit_data_pipeline.py:6`.
- **NOT an orphan** — it's a deliberate live-DB harness that touches
  the production pool, so it CANNOT be a CI test and shouldn't be a
  cron stage.
- **Last activity:** `485a4ac` "feat(scripts): synthetic end-to-end AAR
  pipeline test against live DB" — single commit, intentionally stable.
- **Suggested action:** **promote from `# TODO(P5)` to
  `# OPS_HELPER`** in `_ALLOWLIST`. Replace the TODO(P5) comment block
  with a comment like "Live-DB AAR-pipeline verification harness;
  documented in `docs/OPERATIONS.md` §10. Operator-on-demand by design
  — no wrapper / no stage / no CI test." Same pattern as the existing
  `agent_pr_label_guard` / `gen_engine_manifest` entries (deliberate
  standalones, recorded decision).

### `scripts/test_kill_switch.py` (146 lines, scripts/test_no_orphan_scripts.py:151-156)
- **Documented operator harness** at `docs/OPERATIONS.md:1199-1207`
  and `MASTER_PLAN.md:714`. Flips `platform.risk_state.kill_switch_active`
  for one engine, runs `scheduler.run_once()`, asserts
  `n_candidates == 0` and `n_submitted == 0`, resets the kill switch
  in `finally`. Verifies the startup-kill-switch short-circuit in every
  engine scheduler.
- **NOT an orphan** — same reasoning as `test_aar_pipeline.py`: live-DB
  harness, must touch production `platform.risk_state`, can't be a
  CI test or a cron.
- **The "0 references" finding in the detector is correct** (the
  `test_kill_switch` substring only collides with
  `test_kill_switch_blocks_all_trades` in `tpcore/tests/test_risk_governor.py`
  + `carver/tests/test_scheduler.py:231` — substring collision, not a
  reference). But "zero references" doesn't mean "orphan" — the
  detector is structurally correct, the **classification** is wrong:
  this is a deliberate standalone, not a one-off accretion.
- **Last activity:** `26717a6` "feat(risk): startup kill-switch check
  in all three engine schedulers" + lint touches; intentionally stable.
- **Suggested action:** same as `test_aar_pipeline.py` — promote from
  `# TODO(P5)` to `# OPS_HELPER` in `_ALLOWLIST` with a recorded
  rationale referencing `docs/OPERATIONS.md` §10.

### `scripts/ingest_tradier_csv.py` (254 lines, scripts/test_no_orphan_scripts.py:116-122)
- **One-shot but recently-active** Tradier-CSV → `platform.prices_daily`
  ingester. Reads the CSV from `extract_tradier_full.py`, filters to
  Alpaca-active tickers, idempotent INSERT with `ON CONFLICT DO NOTHING`.
- **Last activity:** `d5faea8` "fix(scripts/ingest_tradier_csv): skip
  non-finite and overflow OHLC rows" — recent (post-initial-load patch
  for the 50k overflow rows mentioned in `session-log.md:42`).
- **Documented in** `EDGE_VALIDATION_PLAN.md:67` and `session-log.md:42`
  as the canonical Tradier-CSV → DB loader. The actual data load was
  **completed** (20.56M rows, 7,710 tickers).
- **Classification call:** this is a borderline DELETE candidate — the
  load is finished, the path is unlikely to re-run because Tradier
  changes need re-extraction first — BUT keeping it as an
  operator-on-demand re-ingest tool is cheap (254 lines, well-documented)
  and matches how the operator handles Tradier (the wide extractor +
  this loader are a paired ad-hoc pipeline). Be conservative: keep.
- **Suggested action:** promote from `# TODO(P5)` to `# OPS_HELPER`
  with the rationale "Tradier-CSV → prices_daily ad-hoc loader; paired
  with `extract_tradier_full.py`. Operator-on-demand re-ingest tool
  documented in EDGE_VALIDATION_PLAN.md".

### `scripts/compare_baselines.py` (72 lines, scripts/test_no_orphan_scripts.py:128-131)
- Tiny argparse wrapper around `tpcore.backtest.compare_trade_lists`.
  Used as a regression-safety gate when refactoring engines or
  migrating strategy constructions (compare two trade-log CSVs within
  tolerance).
- **Documented operator harness** at `tpcore/backtest/equivalence.py:22`
  *"Usage in a diff CLI (`scripts/compare_baselines.py`)"*. The
  underlying `compare_trade_lists` API is in `tpcore.backtest`.
- **Suggested action:** keep as ops helper. Promote from `# TODO(P5)`
  to `# OPS_HELPER` with rationale "Regression-safety diff CLI for
  baseline trade logs; wraps `tpcore.backtest.compare_trade_lists`.
  Operator-on-demand."
- Alternative (operator decision): if the operator rarely uses this
  by hand and the `compare_trade_lists` API is enough, delete the
  script. The functionality is trivially re-creatable from a 5-line
  inline invocation.

### `scripts/extract_tradier_full.py` (412 lines, scripts/test_no_orphan_scripts.py:164-169)
- Wide-universe Tradier CSV extractor (NYSE/NASDAQ/AMEX stocks+ETFs,
  2000-01-01 → today). Streaming + resumable. Produced the 1.07 GB / 22.36M
  row dataset in `EDGE_VALIDATION_PLAN.md:66`.
- **Functionally cold** (the dataset was extracted in a single run,
  per session-log.md) but the operator-on-demand re-extraction pattern
  is the same as `ingest_tradier_csv.py` (which is its downstream
  loader). Both should be classified together: keep as a paired ad-hoc
  pipeline OR delete both.
- **Suggested action:** keep as ops helper alongside
  `ingest_tradier_csv.py`. Promote from `# TODO(P5)` to `# OPS_HELPER`.

---

## Keep as test fixture (0 scripts)

None — no orphan in the list is imported by a `tests/` module.

---

## Headline migrate-to-stage recommendation

**`compute_fundamental_ratios.py` → new `--stage compute_fundamental_ratios`
chained after `fundamentals_refresh` in `OPS_UPDATE_STAGES`.**

Highest leverage because:
1. It's the only orphan that's still being **actively re-invoked** —
   every new quarterly filing requires re-running it (one commit
   already polished the SQL set-based UPDATE).
2. It's an idempotent SQL UPDATE that already fits the
   `async def _stage_<name>(pool, config)` shape — no refactor needed,
   just wrap and register.
3. Adding it to the daily/weekly `--update` chain closes the manual
   step where `pb`/`de` ratios sit NULL for newly-pulled quarters until
   the operator remembers to invoke the script.

The other three migration candidates are either already-superseded
(run_daily_bars_all_active, run_corporate_actions_all_active) or
naturally resolved by promoting a constant
(backfill_backtest_universe → `tpcore.backtest.universe.DEFAULT_UNIVERSE`).

## Followup PR scope (operator-gated, NOT in this PR)

1. (`S`) `git rm scripts/extract_tradier.py` + drop `extract_tradier`
   allowlist entry — superseded by `extract_tradier_full.py`, no live
   wiring, 50-name dataset role absorbed.
2. (`S`) `git rm scripts/run_daily_bars_all_active.py` + drop entry —
   superseded by `--stage daily_bars`.
3. (`S`) `git rm scripts/run_corporate_actions_all_active.py` + drop
   entry — superseded by `--stage corporate_actions`.
4. (`M`) Promote `DEFAULT_UNIVERSE` to
   `tpcore.backtest.universe.BACKTEST_UNIVERSE_50`, import from
   `ops/cron_corporate_actions.py`, then `git rm
   scripts/backfill_backtest_universe.py`.
5. (`M`) Add `_stage_compute_fundamental_ratios` to `ops.py`, register
   in `_STAGE_SPECS` + `OPS_UPDATE_STAGES`, then `git rm
   scripts/compute_fundamental_ratios.py`.
6. (`XS`) Re-comment the four KEEP_AS_OPS_HELPER allowlist entries
   (`test_aar_pipeline`, `test_kill_switch`, `ingest_tradier_csv`,
   `compare_baselines`, `extract_tradier_full`) from `# TODO(P5)` to
   `# OPS_HELPER` with one-line rationale each, mirroring the existing
   `agent_pr_label_guard` / `gen_engine_manifest` recorded-decision
   pattern.

Total scope: ~half-day. Result: 10 P5 orphans → 0 (5 deleted, 5
promoted to operator-helper allowlist entries with recorded rationale).
