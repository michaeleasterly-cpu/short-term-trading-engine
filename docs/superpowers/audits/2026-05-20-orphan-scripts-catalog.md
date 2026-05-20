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

## Zero-allowlist sweep — END STATE 2026-05-21

Operator overruled both the "keep-as-helper" disposition AND the
conservative-deletion rule. Every remaining orphan was either
migrated to a stage (then the script deleted) or deleted outright
when wholly superseded by an existing stage. The zero-allowlist
invariant (`scripts/tests/test_no_orphan_scripts.py::test_allowlist_is_empty`)
locks the end-state in.

### ✅ `scripts/extract_tradier.py` — DELETED 2026-05-21
50-name predecessor wholly superseded by the new
`ops.py --stage extract_tradier_full` (migrated from
`scripts/extract_tradier_full.py`). The 50-name CSV's role was
absorbed when the full export shipped (22M rows, 7,710 tickers
via the paired `ops.py --stage ingest_tradier_csv`). `git rm`'d +
allowlist entry removed.

---

## Migrate to ops.py stage / delete — END STATE 2026-05-21

### ✅ `scripts/run_daily_bars_all_active.py` — DELETED 2026-05-21
Wholly superseded by `ops.py --stage daily_bars` per the ops.py:12
"replaces the previous mix of ad-hoc scripts" docstring. `git rm`'d
+ allowlist entry removed. Historical prose references in
`EDGE_VALIDATION_PLAN.md` and `session-log.md` read correctly as
supersession history and are preserved.

### ✅ `scripts/run_corporate_actions_all_active.py` — DELETED 2026-05-21
Wholly superseded by `ops.py --stage corporate_actions` per the
ops.py:13 "replaces" docstring. `git rm`'d + allowlist entry
removed. `MASTER_PLAN.md:762` "Complete" status note updated.

### ✅ `scripts/compute_fundamental_ratios.py` — MIGRATED 2026-05-20
Set-based UPDATE migrated to `ops.py --stage compute_fundamental_ratios`
(chained immediately after `fundamentals_refresh` in `_STAGE_SPECS` +
`OPS_UPDATE_STAGES`). Closes the manual operator step where pb/de sat
NULL until someone re-ran the script. Script deleted; orphan-allowlist
entry removed. Prose references updated in the migration docstring,
`DATABASE_AND_DATAFLOW.md`, and `MASTER_PLAN.md`.

### ✅ `scripts/backfill_backtest_universe.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`tpcore/backtest/universe.py::DEFAULT_BACKTEST_UNIVERSE`. The 50-name
constant now lives on the installed package path; the duplicated tuple
in `ops/cron_corporate_actions.py` was replaced with an import. The
script's only justification is gone — `git rm`'d + allowlist entry
removed.

---

## Keep as ops helper — OVERRULED 2026-05-20 (5 scripts migrated to stages)

The catalog originally classified five scripts (`test_aar_pipeline`,
`test_kill_switch`, `ingest_tradier_csv`, `compare_baselines`,
`extract_tradier_full`) as "keep as ops helper / un-orphan with
docstring polish". **The operator overruled that disposition** as
"too conservative" and wanted the full kill — every remaining orphan
migrates to an `ops.py --stage <name>` then gets deleted. The
zero-allowlist invariant (`test_allowlist_is_empty` in
`scripts/tests/test_no_orphan_scripts.py`) locks the end-state in.

### ✅ `scripts/test_aar_pipeline.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`ops.py --stage aar_pipeline_smoke`. Synthetic round-trip AAR
verification against the live `platform.aar_events`, self-cleaning
in a `finally` block. `docs/OPERATIONS.md` §10 and
`MASTER_PLAN.md:722` references updated to the new stage. Script
deleted; allowlist entry removed.

### ✅ `scripts/test_kill_switch.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`ops.py --stage kill_switch_smoke --param engine=<reversion|vector>`.
Live `platform.risk_state` flip + `scheduler.run_once()` + zero-work
assertion + reset-in-finally. `docs/OPERATIONS.md` §10 and
`MASTER_PLAN.md:714` references updated. Script deleted; allowlist
entry removed.

### ✅ `scripts/ingest_tradier_csv.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`ops.py --stage ingest_tradier_csv`. Streams the wide Tradier CSV
into `platform.prices_daily` with the Alpaca-active filter +
ON-CONFLICT-DO-NOTHING idempotency. Paired with the new
`--stage extract_tradier_full`. `EDGE_VALIDATION_PLAN.md:67` updated.
Script deleted; allowlist entry removed.

### ✅ `scripts/compare_baselines.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`ops.py --stage compare_baselines --param baseline=… --param candidate=…`.
Thin wrapper around `tpcore.backtest.compare_trade_lists`; no DB
touch. `tpcore/backtest/equivalence.py:22` docstring updated from the
old script path to the new stage. Script deleted; allowlist entry
removed.

### ✅ `scripts/extract_tradier_full.py` — MIGRATED 2026-05-20
Operator overruled the keep-as-helper disposition; migrated to
`ops.py --stage extract_tradier_full`. Wide-universe Tradier CSV
extractor (NYSE/NASDAQ/AMEX stocks+ETFs, 2000-01-01 → today), no DB
writes. Paired with the new `--stage ingest_tradier_csv` (its
downstream loader). `EDGE_VALIDATION_PLAN.md:66` updated. Script
deleted; allowlist entry removed.

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

## Followup PR scope — SHIPPED 2026-05-21

The operator overruled the keep-as-helper disposition. All ten
P5-track orphans plus the three "deliberate standalone" allowlist
entries were resolved across two PRs (this catalog's audit PR + the
zero-allowlist sweep PR). Final state:

1. ✅ `scripts/extract_tradier.py` — DELETED (superseded by the new
   `--stage extract_tradier_full`).
2. ✅ `scripts/run_daily_bars_all_active.py` — DELETED (superseded by
   `--stage daily_bars`).
3. ✅ `scripts/run_corporate_actions_all_active.py` — DELETED
   (superseded by `--stage corporate_actions`).
4. ✅ `scripts/backfill_backtest_universe.py` — DELETED; constant
   promoted to `tpcore.backtest.universe.DEFAULT_BACKTEST_UNIVERSE`;
   `ops/cron_corporate_actions.py` imports it directly.
5. ✅ `scripts/compute_fundamental_ratios.py` — MIGRATED to
   `ops.py --stage compute_fundamental_ratios`, chained immediately
   after `fundamentals_refresh` in `OPS_UPDATE_STAGES`; script
   deleted.
6. ✅ `scripts/test_aar_pipeline.py` — MIGRATED to
   `ops.py --stage aar_pipeline_smoke`; script deleted.
7. ✅ `scripts/test_kill_switch.py` — MIGRATED to
   `ops.py --stage kill_switch_smoke`; script deleted.
8. ✅ `scripts/compare_baselines.py` — MIGRATED to
   `ops.py --stage compare_baselines`; script deleted.
9. ✅ `scripts/extract_tradier_full.py` — MIGRATED to
   `ops.py --stage extract_tradier_full`; script deleted.
10. ✅ `scripts/ingest_tradier_csv.py` — MIGRATED to
    `ops.py --stage ingest_tradier_csv`; script deleted.
11. ✅ `agent_pr_label_guard` allowlist entry — REMOVED; the script
    has genuine wiring via ``.github/workflows/ci.yml``.
12. ✅ `gen_engine_manifest` allowlist entry — REMOVED; the script
    has genuine wiring via ``pyproject.toml`` sentinel-fenced
    comments + shell wrappers.
13. ✅ `audit_code_duplication` allowlist entry — REMOVED;
    ``scripts/tests/test_audit_code_duplication.py`` switched from
    ``importlib.import_module("scripts.audit_code_duplication")`` to
    a real ``import scripts.audit_code_duplication`` so the
    detector recognises the wiring.

**End state:** ``_ALLOWLIST`` is empty; ``test_allowlist_is_empty``
locks the invariant in. Every legitimate operator helper is now
reachable through ``scripts/ops.py --stage <name>``.
