# Lean P5 — De-dup / tpcore Reconsolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Consolidate the 7 actionable duplication clusters into `tpcore/` without regressing any observable engine behavior, in 5 ordered gated-PR phases (lowest-risk first).

**Architecture:** Spec `docs/superpowers/specs/2026-05-19-lean-p5-dedup-reconsolidation-design.md` (v1, operator-approved). Consolidated code → `tpcore/` only (engine→tpcore one-way; `tpcore/scripts/check_imports.py` already enforces this). Each phase: characterization tests written **BEFORE** the refactor (TDD-pin exact current behavior incl. structlog event strings), then consolidate, then prove identical via the authoritative gate. P5.4/P5.5 (live-money) additionally use a `_legacy_*` parallel-diff test and **staged per-engine cutover** (never flip both engines' live paths in one PR).

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), ruff (DTZ+SLF now enforced), pydantic v2, structlog. Subagent-driven; gated PR per phase; CI authoritative via `gh pr checks`; the whole-suite single-process `pytest -p no:xdist` + bidirectional module-order-flip is the AUTHORITATIVE behavior gate (`-n auto --dist loadgroup` accelerator-only); branch-hygiene (`git switch -c`, verify branch pre-commit, no `git stash`).

**Reference (read, do not re-derive):** the spec §2 triage / §3 consolidation homes+API / §4 never-mask / §6 phase test-strategy; `docs/audits/2026-05-19-tpcore-duplication-audit.md`; the actual duped sites cited in §2; `tpcore/scripts/check_imports.py`; the existing `tpcore/order_management/` (`BaseOrderManager` per-trade precedent) + `tpcore/models/graduation.py` + `tpcore/backtest/cost_model.py` + `BaseEnginePlug`; `docs/DEV_PIPELINE_STANDARD.md` (the canonical pipeline).

**Standing acceptance gate EVERY phase:** parallel `python -m pytest -n auto --dist loadgroup -q` (0 failed) + AUTHORITATIVE serial `python -m pytest -p no:xdist -q` (0 failed, == parallel, ≥ current main baseline — re-measure at phase start) + order-flip `python -m pytest -p no:xdist -q tpcore/tests/test_ops.py tpcore/tests/test_ops_helpers.py tests/test_defect_register.py` AND reversed, both green + `ruff check` (file + CI-scoped `reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`) clean. CI verified via `gh pr checks`, never `gh run watch` exit code.

**Behavior-preservation rule (all phases):** the consolidated function must be behaviorally byte-equivalent to what each engine did before (A clusters), or equivalent given the engine's existing parameter values (B clusters — divergence preserved via an explicit param, never erased). structlog event NAMES are observable behavior — characterization tests MUST assert the exact emitted event name per engine.

---

## Phase P5.1 — `overrides_from_args` (#5, pure; gated PR)

Branch `feat/lean-p5-1-overrides` off fresh `main`.

**Files:** Create `tpcore/backtest/cli_overrides.py`, `tpcore/tests/test_cli_overrides.py`; modify `reversion/backtest.py`, `vector/backtest.py`, `momentum/backtest.py` (delegate shims only).

### Task P5.1.1: characterization test (pin current behavior) — TDD
- [ ] **Step 1:** Read the 3 `_overrides_from_args` (`reversion/backtest.py:891`, `vector/backtest.py:736`, `momentum/backtest.py:392`) + each engine's `*_OVERRIDE_KEYS`. Confirm bodies byte-identical (spec §2 #5).
- [ ] **Step 2 (failing test):** `tpcore/tests/test_cli_overrides.py` — for a representative `argparse.Namespace` (some keys set incl. `None`, some absent), assert `overrides_from_args(ns, <engine KEYS>)` returns the exact dict each engine's current `_overrides_from_args(ns)` returns, for ALL 3 engines' key sets. Import the engines' current private fns to capture the golden expectation. Run → FAIL (`tpcore.backtest.cli_overrides` absent).
- [ ] **Step 3:** Create `tpcore/backtest/cli_overrides.py`: `def overrides_from_args(args: argparse.Namespace, keys: Sequence[str]) -> dict[str, object]:` — exact body of the duped fn, `keys` passed by caller (no engine knowledge in tpcore). Typed, ruff-clean (DTZ/SLF enforced).
- [ ] **Step 4:** Replace each engine `_overrides_from_args` body with `return overrides_from_args(args, <ENGINE>_OVERRIDE_KEYS)` (keep the private name as a thin delegate — no call-site churn). Run char-test → PASS.
- [ ] **Step 5:** Full acceptance gate. `git diff --name-only` = the 5 files only. Commit. Gated PR → split spec/intent then code-quality review → fold → CI `gh pr checks` → squash-merge → sync.

---

## Phase P5.2 — `slippage_per_side` (#11) + cap-gate `healthcheck` (#7) (pure; gated PR)

Branch `feat/lean-p5-2-slippage-healthcheck` off prior-merged `main`.

**Files:** Modify `tpcore/backtest/cost_model.py` (+ its test); `tpcore/interfaces/`/wherever the cap-gate base will live is NOT created here (healthcheck consolidates into the same future base only in P5.5 — for P5.2 consolidate `healthcheck` via a tiny shared helper to avoid premature base-class coupling); `reversion/backtest.py`, `vector/backtest.py`, `reversion/plugs/capital_gate.py`, `vector/plugs/capital_gate.py` (delegate shims).

### Task P5.2.1: `slippage_per_side` char-test + consolidate — TDD
- [ ] **Step 1:** Read `_slippage_per_side` (`reversion/backtest.py:109`, `vector/backtest.py:95`); confirm byte-identical, deps = module `SLIPPAGE_PER_SIDE` + engine-local `_TIER_ROUND_TRIP_COSTS`.
- [ ] **Step 2 (failing test):** in `tpcore/tests/test_cost_model.py` (or the existing cost_model test): `slippage_per_side(ticker, tier_round_trip_costs, default)` returns identical values to each engine's current `_slippage_per_side` for: a known-tier ticker, an unknown ticker, empty tier dict. Run → FAIL.
- [ ] **Step 3:** Add `def slippage_per_side(ticker, tier_round_trip_costs: Mapping[str, float], default: float) -> float:` to `tpcore/backtest/cost_model.py` — exact duped logic; engine passes its own `_TIER_ROUND_TRIP_COSTS` + `SLIPPAGE_PER_SIDE`.
- [ ] **Step 4:** Engine `_slippage_per_side` → 1-line delegate. char-test PASS.

### Task P5.2.2: cap-gate `healthcheck` char-test + consolidate — TDD
- [ ] **Step 1:** Read `healthcheck` (`reversion/plugs/capital_gate.py:83`, `vector/plugs/capital_gate.py:68`); confirm byte-identical dict, only `engine` value differs (`self.engine_name`).
- [ ] **Step 2 (failing test):** assert each engine plug's `healthcheck()` dict is unchanged after consolidation (capture current dict as golden). Run → FAIL.
- [ ] **Step 3:** Add a small shared helper `def capital_gate_healthcheck(engine_name: str) -> dict:` in `tpcore/backtest/cost_model.py` OR a focused `tpcore/interfaces/` helper (NOT a base class yet — premature; P5.5 introduces `PerTradeCapitalGateBase`). Engine `healthcheck` → `return capital_gate_healthcheck(self.engine_name)`.
- [ ] **Step 4:** char-test PASS.
- [ ] **Step 5:** Full gate. `git diff --name-only` = only the intended files. Commit. Gated PR → split review → fold → CI → merge → sync.

---

## Phase P5.3 — `load_prices` (#2, parameterized divergence; gated PR)

Branch `feat/lean-p5-3-price-loader` off prior-merged `main`.

**Files:** Create `tpcore/backtest/price_loader.py`, `tpcore/tests/test_price_loader.py`; modify `reversion/backtest.py`, `vector/backtest.py` (delegate shims).

### Task P5.3.1: golden-fixture char-test + parameterized consolidate — TDD
- [ ] **Step 1:** Read `_load_prices` (`reversion/backtest.py:232`, `vector/backtest.py:228`). Confirm SQL/parse identical; the ONLY divergence is the min-bar filter (reversion `len < MA_50_PERIOD+5`, vector `len < SMA_200+5`). This divergence is intentional — it becomes the `min_bars` param, NOT erased.
- [ ] **Step 2 (failing test):** `tpcore/tests/test_price_loader.py` — a fixed in-memory price fixture (fake pool returning deterministic rows incl. some tickers with too-few bars for each engine's threshold). Assert: `load_prices(pool, tickers, s, e, min_bars=MA_50_PERIOD+5)` yields EXACTLY the surviving-ticker set + DataFrames that reversion's current `_load_prices` yields; same for vector with `min_bars=SMA_200+5`. The surviving-ticker set MUST differ between the two min_bars values (proves the divergence is preserved, not flattened). Run → FAIL.
- [ ] **Step 3:** Create `tpcore/backtest/price_loader.py`: `async def load_prices(pool, tickers, start, end, *, min_bars: int) -> dict[str, pd.DataFrame]:` — exact duped SQL/parse; the min-bar filter uses the `min_bars` param. Tz-aware datetimes (DTZ enforced).
- [ ] **Step 4:** Engine `_load_prices` → delegate passing its own `min_bars` (reversion `MA_50_PERIOD+5`, vector `SMA_200+5`). char-test PASS.
- [ ] **Step 5:** Full gate (this is a backtest-result-affecting path — the golden-fixture surviving-set equality IS the proof). Commit. Gated PR → split review (spec/intent adversarial on "min_bars divergence preserved per engine; no backtest drift") → fold → CI → merge → sync.

---

## Phase P5.4 — stale-order-cancel (#1, LIVE-MONEY; staged gated PRs)

Branch `feat/lean-p5-4a-stale-cancel-tpcore-momentum` off prior-merged `main`.

**Files:** Create `tpcore/order_management/stale_order_cancel.py`, `tpcore/tests/test_stale_order_cancel.py`; modify `momentum/scheduler.py` (delegate). Sentinel delegate is a SEPARATE follow-up PR (P5.4b).

### Task P5.4a.1: characterization test FIRST (live-money) — TDD
- [ ] **Step 1:** Read `_cancel_stale_*` (`momentum/scheduler.py:495`, `sentinel/scheduler.py:344`). Confirm logic identical; only divergence = the structlog namespace/`order_prefix`. This CANCELS REAL BROKER ORDERS — exactness is non-negotiable.
- [ ] **Step 2 (failing char-test):** `tpcore/tests/test_stale_order_cancel.py` — a fake broker with a mix: open orders matching/not-matching the engine prefix, already-filled, already-cancelled, age over/under threshold. Assert `cancel_stale_orders(broker, order_prefix=..., log_namespace=...)` produces the EXACT same set of cancelled order IDs, the same return count, AND the same emitted structlog event names as momentum's current `_cancel_stale_*` (capture via a structlog capture/caplog). Run → FAIL.
- [ ] **Step 3:** Create `tpcore/order_management/stale_order_cancel.py`: `async def cancel_stale_orders(broker, *, order_prefix: str, log_namespace: str) -> int:` — exact duped logic; `log_namespace` parameterizes the only divergence.
- [ ] **Step 4:** Replace `momentum/scheduler.py._cancel_stale_*` body with a delegate (`cancel_stale_orders(broker, order_prefix=momentum_prefix, log_namespace="momentum.scheduler")`). Sentinel UNCHANGED this PR. char-test PASS.
- [ ] **Step 5:** Full gate. Commit. Gated PR (split review — spec/intent adversarial: cancelled-ID set + count + log-event names byte-identical for momentum; sentinel path untouched) → fold → CI → merge → sync.

### Task P5.4b: sentinel cutover (separate gated PR off P5.4a-merged main)
- [ ] **Step 1:** char-test for sentinel (same shape as P5.4a.2 but sentinel prefix/namespace) → confirm `cancel_stale_orders` reproduces sentinel's current behavior exactly.
- [ ] **Step 2:** Replace `sentinel/scheduler.py._cancel_stale_*` with the delegate. Run → PASS. Full gate. Gated PR → split review → fold → CI → merge → sync. (Both engines now consume the shared fn; never flipped in one PR.)

---

## Phase P5.5 — `PerTradeCapitalGateBase` (#3, #4, HIGHEST RISK live risk gate; staged gated PRs)

Branch `feat/lean-p5-5a-capgate-base-reversion` off prior-merged `main`.

**Files:** Create `tpcore/interfaces/capital_gate_base.py`, `tpcore/tests/test_capital_gate_base.py`; modify `reversion/plugs/capital_gate.py` (P5.5a). Vector cutover = P5.5b (separate PR); momentum `assert_can_graduate`-only = P5.5c (separate PR).

### Task P5.5a.1: exhaustive characterization + `_legacy_*` parallel-diff — TDD
- [ ] **Step 1:** Read `check_trade` (`reversion/plugs/capital_gate.py:95`, `vector/plugs/capital_gate.py:80`) + `assert_can_graduate` (momentum:140, reversion:141, vector:125) + each engine's `is_graduated`, `engine_name`, `DAILY_LOSS_FREEZE_PCT` source. Confirm `check_trade` logic byte-identical; divergence = log-event engine string + `_daily_loss_freeze_pct` (both `0.05`); `is_graduated` stays per-engine (abstract).
- [ ] **Step 2 (failing char-test):** `tpcore/tests/test_capital_gate_base.py` — EXHAUSTIVE over `check_trade`: nonpositive size, oversize, position-count limit, daily-loss, the `drawdown == -0.05` exact boundary, `engine_equity == 0` skip; assert exact return/raise + the exact structlog event name per branch matches reversion's CURRENT `check_trade`. And `assert_can_graduate`: the `is_graduated` short-circuit, `assert_passed_for_engine` path, `graduation_ready` true/false, raise-vs-return matrix (mock pool). Capture reversion's current behavior as golden. Run → FAIL.
- [ ] **Step 3:** Create `tpcore/interfaces/capital_gate_base.py`: `class PerTradeCapitalGateBase(BaseEnginePlug)` with concrete `check_trade`, `healthcheck` (reuse the P5.2 helper), `assert_can_graduate`; subclass supplies class attr `engine_name`, `_daily_loss_freeze_pct`, and **abstract** `is_graduated`. Log event names derive from `self.engine_name` (must equal today's strings — assert in the char-test).
- [ ] **Step 4 (legacy-diff):** In `reversion/plugs/capital_gate.py`, make the plug subclass `PerTradeCapitalGateBase`, but KEEP the old methods renamed `_legacy_check_trade`/`_legacy_assert_can_graduate`. Add a differential test: over a fuzzed grid of inputs, `new == _legacy` for every case. Run → PASS (proves equivalence). Full gate.
- [ ] **Step 5:** Gated PR (split review — spec/intent adversarial: every reject branch + boundary + log-event-name byte-identical for reversion; vector/momentum UNTOUCHED) → fold → CI → merge → sync.

### Task P5.5b: vector cutover (separate gated PR)
- [ ] Same as P5.5a for `vector/plugs/capital_gate.py`: exhaustive char-test vs vector's current behavior, subclass + `_legacy_*` parallel-diff, prove new==legacy, gate, gated PR, merge, sync.

### Task P5.5c: momentum `assert_can_graduate` + delete `_legacy_*` (separate gated PR)
- [ ] Momentum shares ONLY `assert_can_graduate` (it's a batch engine — NO `check_trade` inheritance; do NOT make it subclass the per-trade base). Consolidate momentum's `assert_can_graduate` via the shared implementation appropriately (a free function `assert_can_graduate(pool, engine_name, is_graduated_fn)` that the base also calls — so momentum reuses without inheriting per-trade `check_trade`). char-test momentum's current `assert_can_graduate` behavior. Then DELETE the `_legacy_*` methods from reversion/vector (the parallel-diff has served its purpose). Full gate. Gated PR → split review → fold → CI → merge → sync.

---

## Self-Review

**1. Spec coverage:** §2 triage A/B set (#5,#11,#7,#2,#1,#3,#4) → P5.1–P5.5 exactly; §3 consolidation homes/APIs → the Create files per phase; §4 never-mask (char-before-refactor + legacy-diff + staged cutover + log-event-name assertions) → embedded in every phase, exhaustively in P5.5; §5 OUT (intra-tpcore, tracked findings, cosmetic) → no task touches them; §6 phase order/risk/test-strategy → the 5 phases lowest-risk-first; §7 D2 dedicated `PerTradeCapitalGateBase` (momentum NOT subclassing per-trade) → P5.5c explicit; D3 thin delegates → P5.1–P5.3. ✓

**2. Placeholder scan:** every task has exact file:line (from the spec's evidence), the actual API signature, a concrete char-test contract (golden capture of current behavior), and the staged-cutover/legacy-diff mechanics spelled out. No "extract appropriately" hand-waving — the equivalence proof is specified per phase. ✓

**3. Type/name consistency:** `overrides_from_args`, `slippage_per_side`, `capital_gate_healthcheck`, `load_prices(*, min_bars)`, `cancel_stale_orders(*, order_prefix, log_namespace)`, `PerTradeCapitalGateBase(BaseEnginePlug)` with abstract `is_graduated` — consistent spec↔plan. Branches `feat/lean-p5-N-*`. ✓

Execution: subagent-driven-development — fresh implementer per phase, split spec/intent-then-code-quality reviews, gated PR per phase (P5.4/P5.5 multi-PR staged), CI authoritative via `gh pr checks`, whole-suite+order-flip authoritative gate, char-tests-before-refactor, `_legacy_*` parallel-diff + per-engine staged cutover for the live-money phases, branch-hygiene + no `git stash`.
