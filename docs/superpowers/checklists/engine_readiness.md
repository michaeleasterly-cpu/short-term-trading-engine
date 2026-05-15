# Engine Readiness Checklist

Pre-merge checklist for any new engine (or substantial change to an existing one) under `<engine_name>/`. Every box must be checked before the PR ships.

Template: copy `tpcore/templates/engine_template/` as the starting point — it satisfies most of these by construction.

## 1. Five Plugs present

- [ ] `<engine_name>/plugs/setup_detection.py` — scans universe, returns `PhaseAssessment`s.
- [ ] `<engine_name>/plugs/lifecycle_analysis.py` — phase transitions + post-fill bookkeeping.
- [ ] `<engine_name>/plugs/execution_risk.py` — sizing + Alpaca order payload construction.
- [ ] `<engine_name>/plugs/aar_logging.py` — builds + logs `AfterActionReport`s.
- [ ] `<engine_name>/plugs/capital_gate.py` — per-trade cap + daily loss freeze + graduation gate.
- [ ] Every plug subclasses `tpcore.interfaces.engine_plug.BaseEnginePlug` and implements both `validate_dependencies` and `healthcheck`.

## 2. Shared tpcore reuse (no duplication)

- [ ] Indicators come from `tpcore.indicators` (`compute_adx`, `compute_bbands`, `compute_chop`). No engine-local indicator implementations.
- [ ] OrderManager inherits from `tpcore.order_management.BaseOrderManager`. `__init__`, `_persist_tier1_to_open_orders`, and `_fetch_recent_orders` are NOT redeclared.
- [ ] Sizing exceptions raise `tpcore.exceptions.SizingError` (not an engine-local exception).
- [ ] Per-trade engines use `tpcore.models.graduation.PerTradeGraduationStats` (subclass to add fields if needed).
- [ ] Client-order-ids are built via `tpcore.order_ids.build_cid` and parsed via `tpcore.order_ids.parse_cid`. The engine's prefix is registered in `tpcore.order_ids.ENGINE_PREFIX`.
- [ ] AAR persistence goes through `tpcore.aar.AARWriter`. Read-side via `tpcore.aar.AARReader`. Exit-reason classification via `tpcore.aar.classify_exit_reason`.
- [ ] Filter pass/block counters use `tpcore.backtest.filter_diagnostics.FilterDiagnostics` on every SIGNAL event.

## 3. Risk + capital gates

- [ ] Every trade path runs through `tpcore.risk.RiskGovernor.check_trade()` **after** the engine-local capital gate. Both must approve.
- [ ] Capital gate's `assert_can_graduate` requires stats thresholds AND a fresh `tpcore.quality.validation.assert_passed` AND a credibility-rubric score ≥ 60 in `platform.data_quality_log` (via `tpcore.backtest.credibility.graduation_ready`).
- [ ] Pre-graduation hard caps + daily-loss freeze + max-concurrent-positions are module-level constants in `models.py`, not magic numbers.

## 4. Order layout

- [ ] **Per-trade engine (sigma/reversion-style):** Tier 1 bracket (TP + SL together) + Tier 2 GTC limit. The order manager submits only Tier 1; the trade-monitor daemon submits Tier 2 reactively on Tier 1 fill.
- [ ] **Per-trade engine (vector-style):** Single bracket entry + TP + SL. No Tier 2.
- [ ] **Batch engine (momentum-style):** Day-market orders only — no per-name stops. Risk is managed by diversification + rotation cadence.
- [ ] `client_order_id` carries the engine prefix (`sg_`, `rv_`, `vector_`, `mo_`, …) so cross-engine attribution works.

## 5. Logging

- [ ] `structlog.get_logger(__name__)` — never `print()`, never stdlib `logging`.
- [ ] INFO for trade submissions / fills / blocks with structured context (`ticker=`, `qty=`, `notional=`).
- [ ] WARNING for governor blocks and pre-fill cancellations.
- [ ] DEBUG for per-bar scanner output inside a loop.
- [ ] Log event names follow `<engine>.<area>.<action>` (e.g. `sigma.order_manager.trade_submitted`).

## 6. Tests

- [ ] `<engine_name>/tests/test_setup_detection.py` — happy path + each gate's reject branch.
- [ ] `<engine_name>/tests/test_execution_risk.py` — payload shape + SizingError on bad price + qty-below-min skip.
- [ ] `<engine_name>/tests/test_order_manager.py` — submit_decision happy path, governor-block path, reconcile idempotence (calling twice doesn't double-log AARs).
- [ ] `<engine_name>/tests/test_capital_gate.py` — daily-loss freeze, position-count cap, oversize reject, graduation rubric.
- [ ] AAR construction tests verify P&L math (entry × qty vs exit × qty, fees applied if any).
- [ ] No `yfinance` imports. No Discord. No `print()` debug residue.

## 7. Scheduler + daemon integration

- [ ] `<engine_name>/scheduler.py` exposes a `run_once` (or analogous) async entry point.
- [ ] Engine is dispatched by `ops/engine_service.py` on the `DAILY_SCAN_COMPLETE` trigger. **Not** called from `scripts/run_data_operations.sh` — data ops and engine execution are decoupled.
- [ ] Idempotent: re-running `run_once` within the same session doesn't duplicate orders (relies on `(engine, trade_id, order_type)` unique constraint on `platform.open_orders`).

## 8. Backtest + credibility

- [ ] `<engine_name>/backtest.py` runs against `platform.prices_daily` (survivorship-clean — see the caveat in `momentum/backtest.py`).
- [ ] Writes a credibility-rubric row via `tpcore.backtest.credibility.score_run` so the graduation gate has something to read.
- [ ] OOS score + DSR reported in the search output so the operator can see whether the engine clears the DSR ≥ 0.95 / credibility ≥ 60 gate.

## 9. Final checks

- [ ] `ruff check .` clean.
- [ ] Full `pytest -q` passes — no regressions in other engines.
- [ ] Engine added to the roster in `CLAUDE.md` (status line) and `docs/MASTER_PLAN.md` (§4 engine specs).
- [ ] Engine prefix added to `tpcore.order_ids.ENGINE_PREFIX`.
- [ ] If the engine adds new daemons, they're installed via `scripts/install_all_daemons.sh`.

## 10. Compliance verifications (added 2026-05-15 after the Sentinel audit)

Six gaps surfaced in the Sentinel compliance audit that the build-time spec
review and template didn't catch. Each gap closes with a one-line `grep` —
running these before merge prevents the same gaps in the next engine.

- [ ] **All 5 plugs subclass `BaseEnginePlug` and implement
      `validate_dependencies` + `healthcheck`.**
      `grep -E "class\\s+\\w+\\(BaseEnginePlug\\)" <engine>/plugs/*.py | wc -l` returns `5`.
      Why: `ops/engine_service` and the operator dashboard rely on the
      `healthcheck()` contract for liveness probes.
- [ ] **`FilterDiagnostics` populated in setup_detection and attached to
      SIGNAL events.**
      `grep "filter_diagnostics" <engine>/scheduler.py` shows at least one
      `db_log.signal(..., extra_data={"filter_diagnostics": ...})` call.
      Why: "why didn't a signal fire today?" requires per-gate
      pass/block counters on every SIGNAL event.
- [ ] **Backtest persists the credibility rubric to
      `platform.data_quality_log` via `write_credibility_score`.**
      `grep "write_credibility_score" <engine>/backtest.py` returns a hit.
      Why: `tpcore.backtest.credibility.graduation_ready` reads the row;
      without it the capital gate's graduation check will never succeed
      regardless of trade performance.
- [ ] **Scheduler checks `tpcore.calendar.is_trading_day` before scanning.**
      `grep "is_trading_day" <engine>/scheduler.py` returns a hit.
      Why: weekends + holidays should be a no-op return, not a DB
      query / order-submission attempt.
- [ ] **AAR plug uses `tpcore.aar.classify_exit_reason` — never hardcodes
      `ExitReason.*`.**
      `grep "classify_exit_reason" <engine>/plugs/aar_logging.py` returns
      a hit. Hardcoded `ExitReason` defaults are forbidden; the
      classifier is the canonical bracket-fill / fallback mapper.
- [ ] **Scheduler cancels its own stale orders before submitting.**
      `grep "_cancel_stale_" <engine>/scheduler.py` returns a hit.
      Mirrors `MomentumScheduler._cancel_stale_momentum_orders`. Without
      this, an unfilled prior order leaves the position `held_for_orders`
      and the next sell is rejected.

These six rules also live in `docs/STYLE_GUIDE.md` (the canonical Don't-Do
list) and the scaffolds at `tpcore/templates/engine_template/` so a fresh
engine inherits the wiring rather than re-inventing it.

---

## Why this exists

Before this checklist, every engine on the platform had its own:

- duplicated `_persist_tier1_to_open_orders` / `_fetch_recent_orders` (~75 lines each, byte-identical).
- engine-local `SizingError` / `GraduationStats` (byte-identical across sigma + reversion + vector).
- ad-hoc `_compute_adx` / `_compute_bbands` implementations inside `setup_detection`.
- different patterns for cross-engine isolation (momentum's `_filter_to_engine_holdings` vs the per-trade engines' implicit tier-suffix filtering).

Phases 1–3 of the 2026-05-14 standardization sweep consolidated the shared concerns into `tpcore/`. This checklist guarantees the next engine doesn't grow new copies. See the corresponding commits:

* Phase 1 (`59ee050`) — `tpcore.indicators` (ADX + BB + CHOP).
* Phase 2 (`ba615aa`) — `tpcore.order_management.BaseOrderManager`.
* Phase 3 (`cd10cc5`) — `tpcore.exceptions.SizingError` + `tpcore.models.graduation.PerTradeGraduationStats`.

Reference implementations:

- `sigma/` — full per-trade engine (tier-cascade).
- `vector/` — full per-trade engine (flat-bracket).
- `momentum/` — full batch engine (cross-sectional monthly rebalance).

Template:

- `tpcore/templates/engine_template/` — copy-paste-start scaffold.
