# Engine `carver` — Design Spec

**Status:** draft (operator scope confirmed 2026-05-20: monthly batch + day-market, T1+T2 universe, standard DSR≥0.95∧cred≥60 gate, register initially in `LifecycleState.LAB`). **Lane:** heavy (new engine, per `.claude/rules/heavy-lane.md` + `docs/DEV_PIPELINE_STANDARD.md` §0). **Master plan slot:** step 4b "engine improvements / future engines" — task #24, operator-prioritized (`project_master_remaining_program`). **Design basis:** `ref_carver_systematic_trading` (Robert Carver, *Systematic Trading*, Harriman House 2015) + sibling `ref_chan_algorithmic_trading` (Ernest Chan, *Algorithmic Trading*, Wiley 2013). **Template:** `tpcore/templates/engine_template/`. **Parity reference:** `momentum/` (closest topology — batch monthly portfolio-allocation, day-market, no per-name stops).

## 1. Problem & intent

Operator (`project_master_remaining_program`, 2026-05-20): build `carver`, a Carver-method systematic engine, as a net-new engine via the SDLC ADD path. The platform's current five engines (`reversion`, `vector`, `momentum`, `sentinel`, `canary`) all currently FAIL the DSR/credibility gate — *signal strength is the binding constraint, not data quality* (CLAUDE.md "Engine credibility status"). `momentum` captures one signal family (cross-sectional 12-1 price momentum); `reversion`/`vector` capture intraday/short-horizon mean-reversion; `sentinel` is a regime-defensive overlay. **None of them combine multiple orthogonal forecasts under a vol-targeted, turnover-limited portfolio framework.** That is exactly what Carver's framework is — and exactly what the platform is currently missing.

Carver's framing is *itself* the edge claim: humble, simple, capped per-rule forecasts → equal-weighted with a diversification multiplier → vol-targeted (Half-Kelly) position sizing → speed-limited turnover so transaction cost is ≤⅓ of pre-cost expected Sharpe. That structure is an explicit *anti-overfitting* answer — which aligns with the platform's sacred DSR/credibility gate doctrine (`project_ml_research_track`) and the SP-A `n_trials` ledger (every probe spends budget — `feedback_research_builder_persona`).

**Why this is the right next engine.** Three orthogonal forecast families (trend, carry/value, mean-reversion) combined under Carver's math produce a portfolio whose realized Sharpe is *expected* to be modest but stable. The combined-forecast framework + vol-targeting means `carver`'s edge is *structural*, not single-signal — so it complements rather than duplicates the existing roster.

## 2. Lane discipline (hard)

Heavy lane (new engine triggers it — `.claude/rules/heavy-lane.md`). Engine roster changes via ECR ONLY (`.claude/rules/engine-roster.md`; the hook blocks hand-edits to `tpcore.engine_profile._PROFILE`). All five engine_readiness §10 compliance checks are non-optional (the SDLC planner machine-checks the programmatic subset via `ops.engine_sdlc.planner._check_readiness`; the remainder is operator-verified before filing). Per `feedback_cut_process_overhead_ship`: lean cadence within heavy lane means one consolidated review per PR (not per-fix), tests + CI are the proof. The Standing Discipline Rules in `docs/DEV_PIPELINE_STANDARD.md` §2 hold throughout (whole-suite + order-flip authoritative; `gh pr checks` not `gh run watch`; no `git stash`).

## 3. Scope — what this engine ships

A new `carver/` package under the repo root (5-plug structure mirroring `momentum/`), plus:

1. **`carver/plugs/setup_detection.py` (Plug 1)** — scans the T1+T2 liquid-common-stock universe, computes three orthogonal per-instrument forecasts (trend / value-carry-proxy / mean-reversion — see §4.1), scales each so its rolling abs-forecast averages 10 and is capped at ±20, equal-weight-combines with the forecast diversification multiplier (FDM), and emits ranked `CarverAssessment` candidates. Populates `tpcore.backtest.filter_diagnostics.FilterDiagnostics` so the scheduler can attach per-gate pass/block counters to every `db_log.signal(...)` event.
2. **`carver/plugs/lifecycle_analysis.py` (Plug 2)** — portfolio-allocation no-op for the per-trade lifecycle path; computes monthly-cycle phase transitions (REBALANCE vs HOLD) and, on a rebalance, the *target* basket vs *current* basket diff (open / close / increase / decrease / hold buckets, mirroring `momentum.models.RebalanceDecision`).
3. **`carver/plugs/execution_risk.py` (Plug 3)** — Carver-method vol-targeted position sizing: per-instrument daily cash-vol estimate (annualized return σ × price × √(1/252)), portfolio vol-target (Half-Kelly heuristic — default 0.25 annualized, configurable in `models.py` as a module-level constant), per-name notional = `(combined_forecast / 10) × (daily_cash_vol_target / instrument_daily_cash_vol)`. Builds day-market `Order` payloads with `client_order_id` prefix `cv_` (registered in `tpcore.order_ids.ENGINE_PREFIX`). Raises `tpcore.exceptions.SizingError` on non-positive price.
4. **`carver/plugs/aar_logging.py` (Plug 4)** — builds `AfterActionReport`s for closed positions (entry on rebalance N, exit on rebalance N+k where k≥1 once the combined forecast flips sign or falls below a deminimis threshold). Uses `tpcore.aar.classify_exit_reason(..., take_profit=None, stop_loss=None)` so the classifier deterministically returns `TIME_STOP` for portfolio-allocation exits (per the AAR template guidance for engines without TP/SL on positions). Persistence goes through `tpcore.aar.AARWriter` (injected at the scheduler layer — momentum's pattern).
5. **`carver/plugs/capital_gate.py` (Plug 5)** — engine-local guard: per-trade cap (default $1.5k pre-graduation, module constant), daily-loss freeze (default 5% of engine equity, module constant), max-concurrent-positions cap. `assert_can_graduate` composes `is_graduated(stats)` + `tpcore.quality.validation.capital_gate.assert_passed(pool)` + `tpcore.backtest.credibility.graduation_ready(pool, engine_name="carver")` exactly per the template; raises `CredibilityScoreInsufficientError` on missing rubric.
6. **`carver/scheduler.py`** — `CarverScheduler.run_once(as_of=None)`: cadence boundary is enforced *exactly once* by the dispatcher via `tpcore.engine_profile.should_fire` (Carver profile = `MONTHLY_FIRST_TRADING_DAY`); the scheduler itself NO LONGER carries an internal cadence gate (momentum's pattern, deleted 2026-05-17). Wires `tpcore.calendar.is_trading_day` early-return as the *defensive* fallback for direct invocation; `db_log.startup()`/`shutdown(...)` bookend the run inside `try/finally`; submits all SELLs first then BUYs through `tpcore.risk.batch_gate.gate_batch_order` (the only sanctioned governor entry point for batch engines per engine_readiness §3); stale-order cancel delegates to `tpcore.order_management.stale_order_cancel.cancel_stale_orders` with prefix `cv_`.
7. **`carver/models.py`** — Pydantic v2 frozen models: `Phase` enum (`SCANNING|REBALANCE|HOLDING|EXIT`), `CarverForecast` (per-rule scaled + capped forecast), `CarverAssessment` (combined-forecast + per-rule breakdown + ranking score), `CarverTarget` (target-basket entry mirroring `momentum.models.RebalanceTarget`), `RebalanceDecision`, `RebalanceOrder`. Module-level constants for caps + the Carver framework parameters (`FORECAST_TARGET_ABS=10`, `FORECAST_CAP_ABS=20`, `ANNUALIZED_VOL_TARGET=Decimal("0.25")`, `MAX_TRADES_PER_INSTRUMENT_PER_YEAR=12`, `PRE_GRAD_POSITION_CAP_USD`, `MAX_CONCURRENT_POSITIONS`, `DAILY_LOSS_FREEZE_PCT`).
8. **`carver/backtest.py`** — runs against `platform.prices_daily` (T1+T2 survivorship-clean substrate, same caveat as momentum), produces per-position `CarverTrade` records on a monthly walk-forward over the configured window; computes search metrics via `tpcore.backtest.search.compute_search_metrics`; **mandatory** `tpcore.backtest.statistical_validation.write_credibility_score(pool, engine_name="carver", score=result.credibility_rubric)` before returning (carver is a graduating engine, not a heartbeat — the canary exception of `.claude/rules/engine-build.md` does NOT apply). Exposes the four uniform Lab-target callables (`run_for_search`, `load_carver_window_context`, `run_carver_with_context`, `default_params`) + the module-level `LAB_TARGET = LabTarget(...)` so SP-B's roster-driven resolver picks it up the moment carver lands in `_PROFILE`.
9. **`carver/order_manager.py`** — *intentionally minimal* for a batch engine; carver does not redeclare `BaseOrderManager` machinery. The scheduler submits via the batch path directly (momentum's pattern — momentum has no `OrderManager` instance; orders flow scheduler → batch_gate → `broker.place_order`). The template's `EngineNameOrderManager` is therefore replaced by a thin `__init__.py` re-export + a doc string explaining the batch topology.
10. **`carver/tests/`** — `test_setup_detection.py` (happy path + each gate's reject branch; FilterDiagnostics non-empty; forecast scaling preserves abs-mean≈10 and respects ±20 cap), `test_execution_risk.py` (payload shape + `SizingError` on price≤0 + qty-below-min skip + the position-size formula is exact arithmetic against a synthetic vol panel), `test_capital_gate.py` (daily-loss freeze, position-count cap, oversize reject, graduation rubric — composing `assert_passed` + `graduation_ready`), `test_scheduler.py` (idempotence: re-running `run_once` within the same session doesn't duplicate orders; non-trading-day no-op; stale-order cancel delegation; kill-switch pre-flight; STARTUP/SHUTDOWN bookend), `test_models.py` (forecast cap, FDM bounds, vol-target sizing math). AAR-construction tests verify P&L math (entry × qty vs exit × qty).
11. **Sentinel-fenced manifest regen** — `scripts/gen_engine_manifest.py` regenerates the smoke-loop loop, `scripts/run_all_engines.sh` (the file `ops/engine_service.py` invokes after `DATA_OPERATIONS_COMPLETE`), `ops/platform_pipeline.py` docstring, and pyproject testpaths/include. **Do NOT hand-edit inside a fence.** Run `gen_engine_manifest.py --check` to confirm CI won't red on drift.
12. **`tpcore.order_ids.ENGINE_PREFIX["carver"] = "cv_"`** — registered so cross-engine attribution works (the YUMC 2026-05-14 incident is why every engine's prefix must be registered).
13. **CRITICAL_TICKERS — not applicable.** Carver scans the T1+T2 universe (≥ 1000 names) and does not depend on any *specific* ticker (unlike Sentinel's SPY-as-VIX-proxy). `tpcore/quality/validation/checks/prices_daily_freshness.py::CRITICAL_TICKERS` requires no update; the general `row_integrity` + `prices_daily_completeness` checks already cover the universe.

## 4. Carver method — what the math actually does (§4.1 plug responsibilities; §4.2 sizing math)

### 4.1 The three forecasts (Plug 1)

A *forecast* in Carver's framework is a scaled, capped number whose magnitude is proportional to expected risk-adjusted return and whose sign is the position direction. The platform's `carver` v0 uses three orthogonal rules:

- **Trend forecast (EWMAC pair).** Exponentially-weighted moving-average crossover, default fast/slow pair (8, 32) trading days → raw signal = `(EWMA_fast(close) − EWMA_slow(close)) / σ_24m(returns)`. Forecast-scaled by a per-rule constant calibrated so the rolling 24-month average abs-forecast equals `FORECAST_TARGET_ABS=10`; capped at ±20. Rationale: trend is the most-replicated systematic edge in the literature; Carver chapters 7-8 give the canonical scaling table for the (2,8)…(64,256) family.
- **Carry/value proxy (12-1 momentum-as-carry, mean-reversion-aware).** Equity-only platform, no spot/futures basis ⇒ no true carry signal; carver v0 uses the *value proxy* Carver discusses in chapter 9: long-horizon (12-1 month) total return reversion, signed *negative* — i.e. a forecast that is positive when the stock is cheap relative to its own 12-month mean. Raw signal = `−(12-1 month total return − cross-sectional median 12-1 total return) / σ_24m`. Scaled + capped identically.
- **Mean-reversion forecast (Bollinger Z-score, 20-day).** Short-horizon. Raw signal = `(EWMA_20(close) − close) / σ_20d(close)`. Positive when the price is below its 20-day mean. Scaled + capped identically. (Chan ch.4 mean-reversion pattern, simplest framing.)

**Combined forecast.** Equal-weight average × FDM (forecast diversification multiplier), then capped at ±20. FDM ≈ √(N) / √(1ᵀρ1 / N²) where ρ is the rolling correlation matrix of the three scaled forecasts over a 24-month window; bounded [1.0, 2.5] per Carver chapter 8 to prevent runaway scaling on low estimated correlation. **All three forecasts are scaled INDEPENDENTLY first, then combined** — equivalent to Carver's separation-of-concerns. The combined forecast is the input to sizing.

`FilterDiagnostics` counters: `universe_total`, `gate_min_history_blocked` (need 24m of returns), `gate_tradeable_common_stock_blocked` (reuses `momentum.models.is_tradeable_common_stock`), `gate_finite_forecast_blocked` (NaN/inf), `candidates_passed`.

### 4.2 Position sizing (Plug 3)

Per-instrument: `position_notional_usd = (combined_forecast / FORECAST_TARGET_ABS) × (daily_cash_vol_target / instrument_daily_cash_vol)`, where:

- `daily_cash_vol_target = engine_equity_usd × (ANNUALIZED_VOL_TARGET / √252)` — Half-Kelly heuristic at default 25% annualized (Carver chapter 9; the operator can drop to 0.20 in `models.py` if early-paper PnL says we're running hot).
- `instrument_daily_cash_vol = price × σ_daily_returns_24m` — the instrument's daily price standard deviation in dollars.

Position is signed by `combined_forecast`'s sign; `carver` is long-only on the equity book (negative forecasts size 0 — we don't short the substrate; consistent with momentum's long-only posture and the platform's paper-only mandate). **Quantity** = `round(position_notional_usd / price)`. Quantities below `MIN_QTY` (default 1 share) skip — `FilterDiagnostics` gets a `gate_qty_below_min_blocked` increment.

**Speed limit (turnover bound).** Carver chapter 11: trade frequency is capped so cost ≤ ⅓ of expected pre-cost Sharpe. Operationalized: per-instrument, a fresh rebalance does not flip the position direction (open↔close) more than `MAX_TRADES_PER_INSTRUMENT_PER_YEAR=12` (default — once per month is the natural cap for a `MONTHLY_FIRST_TRADING_DAY` engine, so this is a *soft* upper bound at default cadence; the constant becomes load-bearing if cadence is ever shortened). The execution plug enforces it as a hold-suppression: if the candidate would flip direction within the per-instrument 12-month window AND has already done so 12 times, the trade is suppressed and `FilterDiagnostics.gate_speed_limit_blocked` increments.

### 4.3 Why the framework matches the platform substrate

- `platform.prices_daily` is the daily-close substrate — Carver's framework is daily-bar-native (chapters 7-11). No new data prereqs.
- `platform.liquidity_tiers` (T1+T2) supplies the universe — same query momentum uses (`tier <= 2`), same survivorship caveat.
- `tpcore.backtest.cost_model.load_tier_costs` already exposes per-tier round-trip cost — the backtest applies it identically to momentum (`SLIPPAGE_PER_SIDE` fallback + `_TIER_ROUND_TRIP_COSTS` per-ticker), so the speed-limit math has real cost numbers to compare against.

## 5. Order semantics + cadence + risk wiring (operator-confirmed)

- **Cadence:** `MONTHLY_FIRST_TRADING_DAY` (operator confirmation 2026-05-20, recommendation accepted). Enforced *exactly once* by `tpcore.engine_profile.should_fire`; the scheduler has no internal cadence gate. `--force-rebalance` is the documented manual-invocation escape hatch (mirrors momentum).
- **Order type:** day-market only — `OrderType.MARKET`, `TimeInForce.DAY`, `OrderClass.SIMPLE`. No bracket, no per-name TP/SL, no Tier 2. Risk is managed structurally (vol-target + diversification + speed limit + monthly rebalance discipline).
- **Risk path:** every submitted order passes `tpcore.risk.batch_gate.gate_batch_order(governor, "carver", ticker=..., notional=..., direction=...)` BEFORE `broker.place_order`. SELL orders also call `governor.record_close(engine_id="carver", trade_id=build_close_id("carver", ticker, as_of), realized_pnl=Decimal("0"))` (the idempotent #251 B1 path momentum uses).
- **Kill-switch pre-flight:** the scheduler reads `governor.state_for("carver")` early; if `kill_switch_active`, returns a no-rebalance `RunSummary` (momentum's F3 audit pattern).
- **Drawdown breaker (optional v0+1):** an equity-drawdown circuit-breaker (mirror `MomentumCapitalGate.check_drawdown` + `DRAWDOWN_BREAKER_LOOKBACK_DAYS`) is in-scope for v0 since it's a 12-line composition; gates: every rebalance reads `EQUITY_SNAPSHOT` peak over the lookback window and skips the rebalance if current equity is more than the configured % below peak.
- **Cross-engine attribution:** `_filter_to_engine_holdings(positions, recent_orders, prefix="cv_")` filters broker positions to ours (`is_engine_cid(client_order_id, "carver")`). Without this filter the rebalance would diff against the whole account and could liquidate other engines' holdings (the YUMC 2026-05-14 incident). Carver reuses momentum's helper or a literal twin in `carver/scheduler.py`.

## 6. Backtest + credibility (the graduation path)

- `carver/backtest.py` runs a monthly walk-forward over `[start - 24m warmup, end]`, producing `CarverTrade` records (one per held position per rebalance cycle).
- `compute_search_metrics(engine="carver", parameters=..., trades_for_diag=..., sharpe=..., profit_factor=..., max_drawdown=..., n_trials=len(parameters), price_data=..., rubric_inputs={"lookahead_clean": True, "survivorship_inclusive": False, "pit_fundamentals": True, "regime_coverage": True, "monte_carlo_drawdown": True}, search_trades=search_trades)` bundles DSR + credibility rubric (momentum's pattern; the `survivorship_inclusive=False` flag is honest about the substrate caveat).
- **Mandatory:** `await write_credibility_score(pool, engine_name="carver", score=result.credibility_rubric)` before returning. Without it the capital gate's `graduation_ready` will never succeed.
- **`LAB_TARGET` (SP-B uniform contract):** the four callables (`run_for_search`, `load_carver_window_context`, `run_carver_with_context`, `default_params`) + the module-level `LAB_TARGET = LabTarget(param_ranges={...}, ...)` declaration. The moment carver lands in `_PROFILE` (LAB lifecycle), SP-B's roster-driven resolver picks it up — no `_runner_for`/`PARAM_RANGES` hand-edit needed.
- **Lab-targetable PARAM_RANGES (`LabTarget.param_ranges`):**
  - `trend_fast` (int, 4–16 — EWMAC fast window)
  - `trend_slow` (int, 16–64 — EWMAC slow window; constrained `slow == 4 × fast` is enforced inside `run_carver_with_context`, not as a ranges constraint, so SP-D's primary-metric resolver doesn't need to know about it)
  - `value_lookback_months` (int, 9–15)
  - `meanrev_window` (int, 10–30)
  - `annualized_vol_target` (float, 0.15–0.30)
  - `idm_cap` (float, 1.5–2.5 — the FDM upper bound)
- **`primary_metric: LabPrimaryMetric.SHARPE_ANNUALIZED`** (SP-D pluggable scoring) — Carver's literature framing is that realized Sharpe is the legitimate primary objective for a vol-targeted multi-forecast portfolio; max-drawdown reduction (sentinel's metric) is structurally irrelevant because the engine has no defensive-overlay role.

## 7. SDLC integration (ECR-ADD path)

- **ECR file:** `ecr_carver.txt` (filed via `python -m ops.engine_sdlc --ecr ecr_carver.txt`; the operator approves the binary diff per spec §6 operator-interaction policy). Block (action=ADD, source=new_scaffold, cadence=monthly_first_trading_day, allocator=true, dispatch_order=next unique among non-RETIRED — planner-validated, gate_dsr=0.95, gate_cred=60, need=Carver-method multi-forecast vol-targeted monthly-rebalance equity portfolio; LAB lifecycle initially per operator confirmation).
- **Lifecycle start:** `LifecycleState.LAB` (`project_master_remaining_program` + operator confirmation 2026-05-20). LAB engines are filtered out of every dispatch/allocator accessor by `_DISPATCHABLE = {PAPER, LIVE}`, so registering carver as LAB is *safe-by-construction* — it cannot enter the dispatch loop until an ECR LAB→PAPER promotion (an automated, gate-verified transition per the ECR spec). Until then, `python -m ops.lab --target-engine carver` produces dossiers, walks the parameter ranges, and the n_trials ledger spends budget against the cumulative cap (SP-A2 ledger). **An ADD into LAB does not require the build PR to clear the gate** — only that all readiness machinery exists; the gate is what later promotes LAB → PAPER.
- **Consistency tests:** `tpcore/tests/test_engine_lifecycle_consistency.py` already exercises the LAB→non-PAPER half-state legs; carver as LAB will satisfy them by construction (no top-level package required for the LAB state to be honored — but carver DOES ship a top-level package because the LAB target needs `carver.backtest` for `run_for_search`/`load_carver_window_context`/`run_carver_with_context`). The `test_lab_sentinel_is_not_wired` leg is specifically the *sentinel* LAB row, not carver — carver is a LAB-but-runnable Lab target, not the unwired sentinel.
- **Lab targetability:** `tpcore.engine_profile.lab_targetable_engines()` returns `{LAB, PAPER, LIVE} \ {allocator, lab-sentinel, canary}` — so carver as LAB is *included* by construction. The frozen anchor in `tpcore/tests/test_lab_targeting_consistency.py` + `test_lab_targetable_accessor.py` must be updated when the ECR adds carver (one anchor update per ECR — that's the contract; `.claude/rules/engine-roster.md`).

## 8. Decisions D-CV-1 .. D-CV-9

| ID | Decision |
|----|----------|
| D-CV-1 | Five-plug structure mirrors momentum exactly (batch portfolio-allocation topology). No OrderManager; orders flow scheduler → batch_gate → `broker.place_order`. |
| D-CV-2 | Three orthogonal forecasts: EWMAC trend (fast/slow=8/32 default), 12-month value proxy (mean-reversion-signed), 20-day Bollinger Z. Operator-confirmed equity universe ⇒ no spot/futures carry; value-proxy is the equity-substrate carry analogue. |
| D-CV-3 | Forecast scaling: per-rule scaling constant calibrated so rolling 24m abs-forecast averages 10; per-rule cap ±20; FDM bound [1.0, 2.5]. Constants live in `carver/models.py` as module-level. |
| D-CV-4 | Vol-target sizing: `position_notional = (combined_forecast/10) × (daily_cash_vol_target / instrument_daily_cash_vol)`. Default `ANNUALIZED_VOL_TARGET = 0.25` (Half-Kelly). Long-only; negative forecasts size 0. |
| D-CV-5 | Cadence `MONTHLY_FIRST_TRADING_DAY`; day-market only; no per-name stops; risk via batch_gate + diversification + speed limit + drawdown breaker. Operator-confirmed. |
| D-CV-6 | Universe = T1+T2 from `platform.liquidity_tiers` (`tier <= 2`); same survivorship caveat as momentum. Operator-confirmed. |
| D-CV-7 | Register in `_PROFILE` initially as `LifecycleState.LAB` (operator-confirmed). LAB → PAPER promotion is the standard ECR-gated transition once the Lab dossier shows DSR ≥ 0.95 ∧ cred ≥ 60. The build PR does NOT need to clear the gate; it needs to ship a valid `LAB_TARGET` so `python -m ops.lab --target-engine carver` works. |
| D-CV-8 | Engine prefix `cv_` registered in `tpcore.order_ids.ENGINE_PREFIX`. Reserves namespace so cross-engine attribution + reconcile filters are correct from day-zero. |
| D-CV-9 | `primary_metric: LabPrimaryMetric.SHARPE_ANNUALIZED` for SP-D. Max-drawdown-reduction (sentinel) does not apply — carver has no defensive role. |

## 9. Risks + how the design defends against them

- **R1 — overfitting to the EWMAC pair / lookback / vol-target.** Defense: ranges are Lab-bounded (SP-A2 `n_trials` ledger spends budget per probe); `survivorship_inclusive=False` is flagged honestly in the rubric; DSR ≥ 0.95 is the graduation bar and DSR penalizes multi-trial fishing by construction.
- **R2 — long-only equity ⇒ no true carry signal.** Defense: the carry slot is structurally replaced by the 12-month value proxy; this is an honest acknowledgement, not a hidden gap. If the value-proxy forecast underperforms the other two in walk-forward, the diversification multiplier naturally down-weights it.
- **R3 — vol-target sizing under regime shifts.** Defense: 24-month rolling vol window damps regime transitions; drawdown breaker (engine-equity peak vs current) is in-scope for v0; the platform-wide `RiskGovernor` is the final backstop via `batch_gate.gate_batch_order`.
- **R4 — speed-limit becomes load-bearing if cadence shortens.** Defense: at default `MONTHLY_FIRST_TRADING_DAY`, the speed limit is a soft upper bound (12 trades/yr/instrument ≥ 12 monthly rebalance opportunities). If cadence ever shortens via ECR MODIFY, the speed-limit constant becomes the binding turnover cap — explicitly noted in `carver/models.py` docstring.
- **R5 — cross-engine attribution drift.** Defense: prefix `cv_` registered in `ENGINE_PREFIX`, the rebalance diffs ONLY against carver-prefixed positions (`_filter_to_engine_holdings`), and the YUMC-incident invariants are honored from build time.
- **R6 — registration in LAB but Lab not yet finding the new engine.** Defense: `LAB_TARGET = LabTarget(...)` is declared in `carver/backtest.py` from day-zero; SP-B's `_lab_target_for` resolver lazy-imports `<engine>.backtest:LAB_TARGET` from the roster SoT; adding carver to `_PROFILE` (LAB) is the SINGLE change needed to make carver Lab-targetable. The `test_lab_targeting_consistency.py` anchor update is the ECR contract.
- **R7 — sentinel-fenced manifests drift on first build.** Defense: `scripts/gen_engine_manifest.py` (NOT hand-editing inside a fence) is the canonical regen; `gen_engine_manifest.py --check` is the CI gate; the build PR includes a regen step + a green `--check` invocation as proof.
- **R8 — momentum AAR-plug finding ([[momentum-aar-plug-finding]]) might reapply to carver.** Defense: carver's scheduler is built fresh with explicit `AAR` plug wiring (rather than scheduler-internal trade-monitor path); the build PR's test_scheduler.py asserts the AAR plug is actually instantiated and called. This pre-empts the same defect-class for carver.

## 10. Universal invariants (CLAUDE.md — non-negotiable)

- All timestamps UTC; market hours via `tpcore.calendar` (XNYS).
- No `yfinance`; no Discord; no manual execution. Alpaca SIP. Paper-only (LAB lifecycle ⇒ doubly enforced).
- Pydantic v2 frozen models; structlog (`structlog.get_logger(__name__)`); fully type-hinted; `from __future__ import annotations`.
- `write_credibility_score` IS called by `carver/backtest.py` — carver is a graduating engine, the canary exception does not apply.
- Scheduler `await db_log.startup()` after the `try:` opens; `await db_log.shutdown(duration_ms=..., exit_code=...)` in `finally:` — without these the dispatcher's idempotency cannot see the cycle closed.
- Engine prefix `cv_` registered in `tpcore.order_ids.ENGINE_PREFIX["carver"] = "cv_"`.
- `setup_detection` populates `FilterDiagnostics`; the scheduler lifts it onto every `db_log.signal(...)`.
- Stale-order cancel via `tpcore.order_management.stale_order_cancel.cancel_stale_orders` (1-line delegate, momentum pattern).
- The 22-site shadow drift incident (Sigma archive, PR #170) is why `_PROFILE` is edited via ECR ONLY and the sentinel-fenced regions are regenerated, never hand-edited.

## 11. Out of scope (this engine v0)

- LIVE lifecycle (LIVE is reserved platform-wide — paper-only mandate; carver's only forward path is LAB → PAPER).
- Short positions (carver v0 is long-only; structurally adding shorts is a future MODIFY, not a v0 scope item).
- Sub-asset filters / sector neutrality (T1+T2 universe-wide; sector caps could be a future MODIFY).
- Per-name TP/SL brackets (incompatible with Carver's portfolio framing — explicit operator-rejected option).
- Futures/spot-basis carry (no spot data feed; equity-substrate value proxy replaces it).
- The actual LAB → PAPER promotion (automated, gate-verified, ECR-MODIFY when the Lab dossier shows DSR ≥ 0.95 ∧ cred ≥ 60).
- Cross-engine improvement work on `reversion`/`vector`/`momentum`/`sentinel` using Carver's toolkit (separately tracked under master step 4b (i); Lab candidates only — never hand-tuned past the gate; `project_master_remaining_program`).
- Research-LLM edge discovery (#242 / SP-G; separate epic; references the same toolkit family).

## 12. Reuse-vs-new (compose vs minimal-new)

REUSE: `tpcore.calendar`, `tpcore.engine_profile.should_fire` + `MONTHLY_FIRST_TRADING_DAY`, `tpcore.risk.RiskGovernor` + `batch_gate.gate_batch_order` + `limits_for`, `tpcore.order_management.stale_order_cancel.cancel_stale_orders`, `tpcore.order_ids.{build_cid, build_close_id, parse_cid, is_engine_cid, ENGINE_PREFIX}`, `tpcore.aar.{AARWriter, classify_exit_reason}`, `tpcore.backtest.{search.compute_search_metrics, statistical_validation.write_credibility_score, credibility.graduation_ready, cost_model.load_tier_costs, filter_diagnostics.FilterDiagnostics}`, `tpcore.lab.target.LabTarget` (SP-B contract), `tpcore.quality.validation.capital_gate.assert_passed`, `tpcore.exceptions.SizingError`, `tpcore.models.graduation.PerTradeGraduationStats`, `momentum.models.is_tradeable_common_stock`, `tpcore.logging.DBLogHandler`, `tpcore.alpaca.AlpacaPaperBrokerAdapter`, `tpcore.interfaces.broker.{Order, OrderClass, OrderSide, OrderType, TimeInForce}`, `tpcore.db.build_asyncpg_pool`, `tpcore.interfaces.engine_plug.BaseEnginePlug`, the engine_template scaffold.

NEW (minimal — carver-specific): the three forecast computations (EWMAC, 12-month value proxy, 20-day Bollinger Z), forecast scaling + capping + FDM, vol-target sizing math, speed-limit enforcement, the `Carver*` Pydantic models, the carver tests. Effectively ~85/15 REUSE/NEW.

## 13. Self-review — what could still kill this

1. **The forecast scaling constant calibration is empirical.** The spec specifies WHAT scaling means (rolling-abs-forecast averages 10); the exact per-rule constants are a build-time empirical calibration. The implementer must compute them on a representative T1+T2 window and bake them as module-level defaults; the test_models.py assertion is `0.8 ≤ rolling_abs_forecast_24m / 10 ≤ 1.2` on synthetic + a real-data fixture.
2. **FDM correlation estimation is a 3×3 rolling matrix.** Spec calls it "rolling 24-month"; implementation must handle the cold-start (< 24 months data on a recent listing) — the natural fallback is FDM=1.0 (identity weighting) until the matrix can be estimated. The test asserts that fallback explicitly.
3. **`LAB_TARGET` is declared at module top-level in `carver/backtest.py`.** That means the live-trading path (which imports `carver.scheduler` → `carver.backtest` via the `LAB_TARGET` declaration) will import the LabTarget object. Acceptable per momentum's precedent (momentum/backtest.py:557 has the same pattern); live trading does not USE the LabTarget, but the import path is consistent with the rest of the roster.
4. **Carver's book uses absolute-return-target framing; the platform uses Sharpe-target framing.** They're mathematically equivalent under vol-targeting (Sharpe = excess return / σ; once σ is fixed, max-Sharpe ⇔ max-return). The spec's framing is Sharpe-first because SP-D's `LabPrimaryMetric.SHARPE_ANNUALIZED` is what the gate reads.
5. **Speed-limit at MONTHLY cadence is loose; the design must NOT pretend it's the binding constraint at v0.** Spelled out in §4.2 + R4 above.

## 14. Acceptance criteria (the gate the build PR clears)

- All 10 sections of `docs/superpowers/checklists/engine_readiness.md` are operator-verifiable on the build PR diff (ECR machine-checks the §1 subset + scaffold-dir existence + 5 `BaseEnginePlug` subclasses; the remaining 9 sections operator-verified).
- `python -m ops.engine_sdlc --ecr ecr_carver.txt` produces a clean planner-validated diff; operator approves via `APPROVE? (y/n)`.
- `scripts/gen_engine_manifest.py --check` is green.
- `ruff check .` clean.
- Full `pytest -q` whole-suite + reversed-module-order green (the §11 authoritative gate of `docs/DEV_PIPELINE_STANDARD.md`).
- `grep "from tpcore" carver/**/*.py` returns hits (universal reuse honored).
- `grep -E "class\s+\w+\(BaseEnginePlug\)" carver/plugs/*.py | wc -l` returns `5`.
- `grep "write_credibility_score" carver/backtest.py` returns a hit.
- `grep "is_trading_day" carver/scheduler.py` returns a hit (defensive fallback).
- `grep -E "db_log\.startup\(|db_log\.shutdown\(" carver/scheduler.py` returns hits.
- `grep "classify_exit_reason" carver/plugs/aar_logging.py` returns a hit.
- `grep "cv_" tpcore/order_ids.py` returns a hit (`ENGINE_PREFIX["carver"] = "cv_"`).
- `grep "carver" scripts/run_all_engines.sh` returns a hit inside the sentinel-fenced engine loop (regenerated, not hand-edited).
- `grep "carver" scripts/run_smoke_test.sh` returns a hit inside the per-engine smoke loop.
- `python -m ops.lab --target-engine carver --dry-run` resolves the LAB_TARGET (proves SP-B roster-driven resolver picks it up).

## 15. Pointers (canonical SoT — load on demand during build)

- `tpcore/templates/engine_template/` — copy-paste scaffold.
- `docs/superpowers/checklists/engine_readiness.md` — the 10-section non-optional build gate.
- `docs/superpowers/checklists/engine_change_request.md` + `python -m ops.engine_sdlc --ecr` — the ECR-ADD path.
- `.claude/rules/engine-build.md` — the per-touch compliance shortlist.
- `.claude/rules/engine-roster.md` — `_PROFILE` ECR-only hard rule.
- `.claude/rules/heavy-lane.md` — the heavy-lane pipeline mandate.
- `docs/DEV_PIPELINE_STANDARD.md` §0/§1/§2/§3 — the lanes, the pipeline, the discipline rules, the lean integration.
- `scripts/gen_engine_manifest.py` — the sentinel-fenced manifest regenerator.
- `momentum/` — closest topology parity reference (batch monthly, day-market).
- `sentinel/` — secondary parity reference (batch daily, day-market, defensive-overlay).
- `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md` — the Engine SDLC spec naming engine_readiness as the ADD build gate.
- `docs/superpowers/specs/2026-05-18-engine-lab-design.md` — the Lab spec (LAB_TARGET contract).
- `docs/superpowers/specs/2026-05-19-lab-sp-b-roster-driven-targeting-design.md` — SP-B's roster-driven Lab targeting (the design carver's `LAB_TARGET` plugs into).
- `docs/superpowers/specs/2026-05-20-lab-sp-d-pluggable-scoring-design.md` — SP-D's `LabPrimaryMetric` machinery (carver picks `SHARPE_ANNUALIZED`).
- `docs/superpowers/specs/2026-05-20-sentinel-maxdd-lab-candidate.md` — SP-E's worked Lab-target example (close structural twin to what carver's LAB phase will produce).
- `ref_carver_systematic_trading` (memory) — Carver book PDF + the operator's design-basis framing.
- `ref_chan_algorithmic_trading` (memory) — Chan book PDF (cross-engine improvement reference; mean-reversion / momentum / overfit cautions).
- `project_master_remaining_program` (memory) — task #24 priority + master sequence slot.

---

**Next steps (post-spec-merge):**
1. Operator spec-read gate (heavy-lane step 4) — this PR.
2. Writing-plans skill → plan PR (heavy-lane step 5).
3. ECR-ADD filing + operator approval (`ecr_carver.txt` + `python -m ops.engine_sdlc --ecr ecr_carver.txt`).
4. `engine-implementer` subagent dispatch — scaffold + 5 plugs + scheduler + backtest + tests + sentinel-fenced manifest regen.
5. Build PR(s), one per logical chunk, heavy-lane split-review (spec-reviewer then code-quality-reviewer per `.claude/rules/heavy-lane.md`).
6. Whole-suite + order-flip authoritative gate green; `gh pr checks` SUCCESS; squash-merge `--delete-branch`; `git switch main && git pull` sync.
