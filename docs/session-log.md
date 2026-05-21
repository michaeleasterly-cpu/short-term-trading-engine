# Session Log

## 2026-05-12 (continued) — Phase 2 complete (cost model on Corwin-Schultz)
- B1: migration `20260512_2100_spread_observations_and_liquidity_tiers.py` — both tables live with a 30-day rolling retention trigger on `spread_observations`.
- B2: `tpcore/backtest/spread_estimator.py` — Corwin-Schultz daily H/L estimator + `rank_universe_by_liquidity` that writes per-ticker means to `spread_observations` with `source='corwin_schultz'`. 6 unit tests.
- B3: dropped — Tradier streaming is deprecated. Expert call: ship the cost model on Corwin-Schultz alone (per-ticker mean over 20+ daily bars is stable enough to discriminate T1 from T5 with the 5/15/50/200 bps thresholds), defer live intraday quotes to an on-demand REST call when a real engine actually needs a fresh quote at submission time.
- B4: `scripts/assign_liquidity_tiers.py` — aggregates `median_spread_pct` + `p95_spread_pct` per ticker, assigns tier, upserts. Source-agnostic via `--sources` so a future real-time feed plugs in without code change. First run distribution: T1:14 / T2:46 / T3:324 / T4:988 / T5:63 across 1,435 coarse-pass tickers.
- B5: `tpcore/backtest/cost_model.py` — `get_round_trip_cost(pool, ticker)` reads tiers from the DB; T4 = 1.50% round-trip default for unknowns. `SimpleCostModel.slippage_bps` default bumped from 5 bps to 75 bps (per-side, matches T4) so unconfigured backtests stop silently understating cost. 6 unit tests.
- B6: `RiskGovernor.check_cost(ticker, expected_edge_pct)` + new kwargs on `check_trade`. Engines compute their own per-trade edge from `assessment.entry_price` and the conservative TP (Sigma mid-band, Reversion 20-MA, Vector profit_target) and pass it. 6 new tests; back-compat preserved (no ticker + no pool → ALLOW).
- B7: migration `20260512_2200_parity_drift_log_spread_columns.py` adds `spread_at_order_pct` + `spread_observed_at`. `LivePaperParityHarness.submit_pair` snapshots the latest `spread_observations` row for the ticker before submission, threads it into both the returned `ParityDriftRecord` and the INSERT. 2 new tests.
- Pushed 342 passing tests (was 322), ruff + forbidden-imports green.

## 2026-05-12 (continued) — Calendar bug fix + pipeline smoke wired through tpcore.calendar
- Acceptance audit on `scripts/pipeline_smoke_test.py` caught a real contract violation: the market-hours check was a hardcoded `13:30 ≤ UTC minutes ≤ 20:00 ` range, not `tpcore.calendar`. CLAUDE.md + STYLE_GUIDE.md require the calendar.
- Replacing the hardcoded check surfaced a latent bug in `tpcore.calendar`: `session_contains` / `next_open` / `next_close` / `previous_close` passed tz-aware pandas Timestamps (carrying `datetime.timezone.utc`) into `exchange_calendars`, which now validates inputs through `calendar_helpers.parse_date` and reads `ts.tz.key`. Stdlib `datetime.timezone.utc` doesn't expose `.key`, so every call crashed with `AttributeError`.
- Fix: naive UTC Timestamps at the `exchange_calendars` boundary (same wall-clock UTC, no tzinfo to introspect); tz-aware Timestamps remain only for the open/close range comparison in `session_contains`. Stdlib `datetime.timezone.utc` stays the lingua franca — no `ZoneInfo` / `pytz` introduced.
- New `tpcore/tests/test_calendar.py` (11 tests) pins the regression: during/before/after-session, weekend, holiday, naive-input ValueError, `next_open` / `next_close` / `previous_close` stdlib-UTC round-trip, `trading_days_between` arithmetic.
- `scripts/pipeline_smoke_test.py` `SKIPPED` message now reports the live `next_open` timestamp from the calendar — e.g. `"SKIPPED — NYSE session is closed at 2026-05-12T10:27+00:00. Next open per tpcore.calendar: 2026-05-12T13:30+00:00."`
- 322 tests pass, ruff clean.

## 2026-05-12 (continued) — Pipeline smoke test + monitor run-as-module fix
- Local run of `python tpcore/trade_monitor.py` surfaced a sys.path trap: the script's directory ends up on sys.path, and the stdlib's internal `import logging` (via concurrent.futures._base via asyncio) resolves to the project's `tpcore.logging` package. Fix: invoke as `python -m tpcore.trade_monitor`. Updated docstring + `railway.json` startCommand accordingly; verified the monitor connects to `BaseURL.TRADING_STREAM_PAPER` and writes STARTUP + STREAM_CONNECTED to `application_log`.
- New `scripts/pipeline_smoke_test.py` — live end-to-end smoke that submits one Tier 1 BUY bracket on SPY, inserts the matching `open_orders` row, polls for the monitor to mark Tier 1 filled and submit Tier 2, then cancels everything and cleans up. Market-hours gated (13:30–20:00 UTC, Mon–Fri); idempotent across reruns. Documented in `docs/OPERATIONS.md` §10 alongside the existing broker-only `smoke_test.py`.

## 2026-05-12 (continued) — Trade monitor built (Phase 1.5 complete)
- M1: Alembic migration `20260512_0000_create_open_orders.py` creates `platform.open_orders` (id, engine, trade_id, ticker, order_type, alpaca_order_id, status, fill_price, filled_at, decision_data jsonb) with `UNIQUE (engine, trade_id, order_type)` + partial index on `alpaca_order_id` for the monitor's hot path.
- M4: `AlpacaPaperBrokerAdapter.submit_tier1_only(...)` — single-bracket primitive returning the placed `Order`. `submit_execution_decision` retained as deprecated wrapper for the smoke test.
- M3: All three engine order managers (Sigma, Reversion, Vector) refactored — submit Tier 1 only via the new primitive, persist `decision` + `assessment` JSON to `platform.open_orders`. `TPCORE_SCAN_ONLY` guard removed.
- M2: `tpcore/trade_monitor.py` — `TradeMonitor` class consuming Alpaca `TradingStream`, reactive Tier 2 submission on Tier 1 fill, AAR + `risk_state` write on Tier 2 close, crash-safe via `reconcile_pending_on_startup`, exponential-backoff reconnect loop. `python tpcore/trade_monitor.py` CLI entry.
- M5: `tpcore/tests/test_trade_monitor.py` — 13 tests covering helpers (`_decimal`, `_aware`, `_resolve_tier2_take_profit`, `_row_from_record`), Sigma Tier 1 → Tier 2 submission, Vector no-tier2 path, Tier 2 fill → AAR + risk_state, unmatched fills ignored, cancellation flow. All pass.
- M6: `trade-monitor` service added to `railway.json` (`restartPolicyType=ALWAYS`). Railway deploy verification deferred (Railway is paused).
- Full suite: **311 passing, 4 skipped**. Ruff + forbidden-imports green.

## 2026-05-12 (continued) — Scan-only guard + trade-monitor spec
- Attempted `scripts/start_paper_trading.py`: surfaced a real engine bug. The order managers (Sigma, Reversion, Vector) call `broker.submit_execution_decision` which submits both Tier 1 + Tier 2 sequentially. Tier 2 is an opposing-side limit (LONG → SELL at upper band); Alpaca rejects with `cannot open a short sell while a long buy order is open`. Tier 1 lands as an orphan with no managed Tier 2 exit. Architectural gap: the engines were designed assuming a live worker would react to fills; that worker was never built.
- Added a `TPCORE_SCAN_ONLY=true` env-var guard to all three order managers — runs gates + governor + signal logging, then returns `None` before any broker call. Defense-in-depth so a cron / manual run can't accidentally submit while the trade monitor is missing.
- Wrote design spec `docs/superpowers/specs/2026-05-12-trade-monitor-design.md` for the live `TradeMonitor` worker: Alpaca `TradingStream` consumer, new `platform.open_orders` table, engine refactor to Tier 1-only submission, crash-safe rehydration. Queued as the next implementation block.
- Two orphan Tier 1 orders (YUMC) from intermediate runs were cancelled at the broker.

## 2026-05-12 — Phase 1 complete + paper-trading smoke test
- **A1**: Alpaca `all_active` sweep wired (handler `_handle_daily_bars_all_active` + local driver `scripts/run_daily_bars_all_active.py`). Universe expanded from ~50 to 7,694 tickers in `platform.prices_daily`.
- **A2**: Tradier wide-export ingested via `scripts/ingest_tradier_csv.py` with Inf/overflow guards (50k bad-data rows dropped — 0.23% of source). 20.6M rows total.
- **A3**: `scripts/simulate_universe.py` rewritten to batched SQL (32 min → 57 s). Results: Sigma 187, Reversion 4, Vector 0. Vector zero is a calibration issue, not data: 65% of coarse survivors fail on `P/B < 1.5` (current market expensive — AAPL P/B 38.85).
- **Corporate-actions** handler now supports `config.universe = "all_active"`; full universe ingested (109,344 events, 250 splits across 217 tickers, 2 splits actually applied to bars — Tradier was already adjusted for the other 248).
- **Fundamentals backfill**: FMP Starter pulled 178,518 quarters across 5,981 tickers via `scripts/backfill_fundamentals.py --all-active`. `compute_fundamental_ratios.py` rewritten as a single set-based SQL UPDATE (the previous per-row loop dropped its pooler connection mid-run) + tightened input filter (`total_assets > 0 AND total_liabilities >= 0`) to reject degenerate FMP rows.
- **Paper-trading smoke test**: new `scripts/smoke_test.py` round-trips a Sigma-shaped `ExecutionDecision` through `AlpacaPaperBrokerAdapter.submit_execution_decision()` and cancels — proves the database → universe → execution risk → broker → audit-log loop end-to-end. Validated on ACAD.
- **Infra**: Supabase upgraded Free → Pro ($25/mo, 8 GB) on 2026-05-11 after the all-active sweep tripped the free-tier 500 MB read-only lock. Railway auto-deploys disabled; all daily ops run locally for now. CI ruff drift fixed; 298 tests pass.
- Total fixed monthly cost: $52 (FMP Starter $22 + Railway Hobby $5 + Supabase Pro $25).

## 2026-05-13 — Phase 0 Bootstrap
- Initialized repo structure
- Built tpcore skeleton
- Created platform schema migrations
- Ran Alpaca asset ingestion script
- Decision: FMP free tier for now; upgrade to Starter before July backtesting

## 2026-05-20 — Status Checkpoint

Catching the log up after 14 days of work (last entry: 2026-05-13).
Major work shipped 2026-05-13 → 2026-05-20:

- **Data**: SEC EDGAR bulk Form-345 ETL (2026-05-16); 14-stage
  autonomous data-operations pipeline with bounded self-heal
  (tpcore/selfheal/, 2026-05-16); CSV-first ingestion contract +
  shrinkage detection (2026-05-15); per-engine data gates
  (2026-05-16); 7 new feeds (ApeWisdom, Fear & Greed, greeks.pro,
  Finnhub, FINRA, IBorrowDesk, AAII) 2026-05-14..16.
- **Engines**: Sigma archived (2026-05-16, archive/sigma/EULOGY.md);
  Canary heartbeat engine added (2026-05-17); Engine SDLC framework
  LAB→PAPER→LIVE→RETIRED with _PROFILE SoT + ECR (2026-05-18,
  docs/superpowers/specs/2026-05-18-engine-sdlc-design.md).
- **Safety**: RiskGovernor uniform check_trade across all engines;
  batch-engine slot accounting (#251, PRs #82/#87/#88, 2026-05-19);
  reconcile_open_floor never-fail-open.
- **Governance**: Data-lane + Engine-lane Escalation & Hardening
  Ladders (2026-05-17, 2026-05-18); Data Provider Lifecycle with
  ProviderBinding SoT (2026-05-17); Consolidated Defect Register
  (#254, 2026-05-19); two-daemon consolidation DA-3 (2026-05-18);
  allocator event-driven via ops/engine_dispatch.py (2026-05-17).
- **Advisory**: LLM data-triage agent (#187, 2026-05-18); LLM
  engine-triage agent (Epic E, 2026-05-18) — both advisory-only,
  credential-starved, draft-PR-only.
- **Research**: Lab front-half epic SP-A through SP-F
  (2026-05-19..20); SP-A2 batch slot accounting + n_trials ledger.

Per-engine credibility status: all 5 PAPER engines produce
positive OOS edge candidates (~0.78–1.26) but fail the DSR≥0.95 ∧
credibility≥60 gate. No engine has graduated. Capital remains in
paper mode.

## 2026-05-20 — Session shipments (14 PRs, two parallel sessions)

This session shipped a lot across two parallel Claude windows (one
driving Lab front-half completion + SDLC reform + worktree lock-in,
the other building carver from scratch + the first real Lab candidate).

**Lab front-half epic — completed end-to-end (SP-A through SP-G):**
- PR #146 — SP-G hardened design spec (thin advisory LLM spec-emitter).
- PR #152 — SP-G build (~3900 LOC): `tpcore/lab/llm_emitter/` models +
  emitter + diff_fence + ledger_gate + tests; `ops/llm_lab_emitter.py`
  agent (credential-starved, draft-PR-only, ledger-before-PR
  ordering); third co-task in `ops/llm_triage_service.py`;
  `.claude/skills/lab-spec-emit/SKILL.md`; Carver + Chan reference
  bundles; orphaned-spend recovery runbook.

**Engine SDLC reform:**
- PR #153 — ECR `source: existing_code` for post-hoc roster
  registration (the SP-F → catalyst pattern that had no SDLC path).
- PR #154 — carver engine shipped (LAB; PRs #149 spec + #151 plan).
  First real ECR-ADD ever — surfaced the LAB-sentinel uniqueness
  over-constraint and relaxed it to presence-only.
- PR #158 — autonomous Lab criteria: replaces the absolute DSR≥0.95 ∧
  credibility≥60 gate with framework-evaluated signal-presence (new
  engines) + comparative-improvement (`fold_existing`) criteria sets.
  Spec at `docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md`.
- PR #159 — catalyst activated in PAPER (`dispatch_order=7`,
  `allocator_eligible=True`, in `roster_for_dispatch()`). Lands via
  the new `source: existing_code` ECR path + the autonomous Lab
  criteria. Backtest 2020-2026: sharpe 2.27, pf 1.36, max_dd −41%,
  24 trades over 6 years (~4/yr sparse signal).

**First real Lab candidate (Carver window):**
- PR #157 — Vector sector-relative composite Lab candidate
  (`vector_composite`, `fold_existing` intent, ranking-only with
  byte-identical-when-off seam).
- PR #160 — transient-DB retry on Lab per-window panel-load (Supabase
  pooler resilience).
- PR #161 — ruff B023 fix on the retry lambda.

**Workflow + governance:**
- PR #145 — `/doctor` $schema fix + MD drift sync (README sigma-archived,
  session-log checkpoint, `DAILY_SCAN_COMPLETE` → `DATA_OPERATIONS_COMPLETE`).
- PR #147 — new `lab-isolation-db` CI job runs 5 DB-gated suites with a
  real Postgres + alembic schema-bootstrap fix.
- PR #148 — `DBLogHandler.run_id` public accessor (closes TODO P3e
  noqa SLF001 debt at 3 scripts/ops.py sites).
- PR #150 — TODO.md as canonical work-tracking source: SessionStart
  hook auto-injects open H2 sections; CLAUDE.md gets the 5-line
  pointer; task #25 + missing engine items migrated out of memory.
- PR #155 — worktree workflow lock-in: `.worktreeinclude` carries
  `.env` into new worktrees; `worktree.bgIsolation: "worktree"` +
  `isolation: worktree` on implementer agent profiles; one CLAUDE.md
  paragraph at the workflow.
- PR #156 — worktree cleanup convention: when a worktree's PR merges,
  remove it the same turn.
- PR #162 — TODO.md drift sync: this entry's bookkeeping.
- PR #187 — Reversion PCA-residual Lab candidate (Avellaneda-Lee 2010,
  `signal_mode` opt-in, byte-identical-when-off, live plug UNTOUCHED per
  the Sigma lesson). New shared primitive `tpcore/backtest/pca_residual.py`
  (rolling 252d PCA + OU s-score + k-means PCA-implied groups substituting
  for GICS sectors). Survivorship leg: terminal-delisting full wipe-out
  per Shumway 1997, `survivorship_inclusive=False`. Single config + ONE
  pre-declared volume-overlay robustness arm = 2 n_trials spend against
  the SP-A cumulative ledger. Sweep run + adjudication + #173 live
  setup_detection parity remain (operator-deferred until the sweep
  clears the verdict bar).

## 2026-05-21 — Reversion PCA-residual sweep: FALSIFIED in walk-forward

Operator ran the canonical sweep (per spec
`docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-candidate.md`)
via `/tmp/run_reversion_sweep.sh` 2026-05-21.

**Walk-forward result (3 windows, top-5 by mean OOS score):**
1. score=+0.934 windows=3 `{signal_mode: price_z, max_hold_days: 6, stop_pct: 0.044, vol_climax: 1.46, z_threshold: 2.20}` — **winner = the existing live baseline**
2. score=+0.891 windows=2 `{signal_mode: pca_residual, max_hold_days: 9, stop_pct: 0.043, vol_climax: 2.41, z_threshold: 3.42}`
3. score=+0.817 windows=4 `{signal_mode: price_z, max_hold_days: 12, stop_pct: 0.106, vol_climax: 2.34, z_threshold: 2.05}`
4. score=+0.694 windows=4 `{signal_mode: pca_residual, max_hold_days: 11, ...}`
5. score=+0.670 windows=4 `{signal_mode: pca_residual, max_hold_days: 7, ...}`

The final held-back replay (2022-01-01 → 2026-05-15) crashed on Postgres
`statement_timeout`:
```
2026-05-21 05:15:19 [error] lab.run_failed error='canceling statement due to statement timeout'
```
No `VERDICT:` line, no dossier written. **40 trials burned on the SP-A
`reversion` ledger** (cumulative ~68 post-prior-probes).

**Honest interpretation:** The walk-forward result is sufficient to
falsify the PCA-residual hypothesis at this trial count. PCA-residual
did not beat the price_z baseline in the universe / period / cost-model.
Per operator standing rule (falsification is final; n_trials honest
accounting): **no MODIFY shipped, no re-run, no parameter tweaking**.
Live `reversion/setup_detection` parity (#173) stays deferred
indefinitely on the price_z baseline.

Engineering follow-up dispatched (subagent): chunk the Lab final-holdout
replay so the same statement_timeout doesn't bite future heavy Lab runs
(Sentinel Bear Score, Catalyst insider-cluster, Momentum vol-managed
all face the same risk). Not a reason to re-test PCA-residual.
