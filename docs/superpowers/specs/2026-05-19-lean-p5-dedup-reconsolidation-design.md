# Lean P5 — De-dup / tpcore Reconsolidation — Design **v1 (expert-hardened, operator-approved)**

**Status:** design **v1** 2026-05-19. Brainstorm → expert-harden →
operator-approved (Full P5 P5.1–P5.5, autonomous) → **spec (this doc)**
→ implementation plan → phased subagent build. The code-mutating tail
of the Lean Dev Env initiative, deferred from
`2026-05-19-lean-dev-env-codebase-health-design.md` to its own
brainstorm. Input artifact: `docs/audits/2026-05-19-tpcore-duplication-audit.md`
(Lean P3c, AST-hash near-dup scan). Continuity:
`[[lean-dev-env-state]]`.

## 0. Problem

The P3c audit found 28 duplication clusters (15 cross-package). The
genuinely-shared engine logic (backtest helpers, the per-trade
capital-gate, stale-order-cancel) is copy-pasted across
`reversion/ vector/ momentum/ sentinel/`, drifting independently. P5
consolidates the *actionable* subset into `tpcore/` (engines consume
it) **without regressing any observable behavior** — on a live-money
platform, a naive "extract two look-alike functions" of a risk/order
path is catastrophic, so equivalence is proven, not assumed.

## 1. Verdict — 7 of 28 clusters actionable; behavior-preservation is the contract

Each cluster was triaged by **reading and diffing the actual
implementations** (not the AST-hash alone): **A** safe-consolidate
(byte-identical, off live path), **B** consolidate-with-care (shared
logic, real per-engine divergence preserved via an explicit
parameter), **C** do-not (coincidental AST collision / false-dedup /
out-of-scope). Consolidated code lands in `tpcore/` only — the
already-enforced `tpcore/scripts/check_imports.py` layering
(engine→tpcore one-way, no engine→engine) is preserved by
construction. **No latent divergence bug surfaced** (never-mask
cleared — see §4).

## 2. Per-cluster triage (evidence-grounded)

| # | Cluster (file:line) | Verdict | Equivalence finding |
|---|---|---|---|
| 5 | `_overrides_from_args` ×3 (`reversion/backtest.py:891`, `vector/backtest.py:736`, `momentum/backtest.py:392`) | **A** | Bodies byte-identical; only the engine-local `*_OVERRIDE_KEYS` tuple (data, not logic) differs. |
| 11 | `_slippage_per_side` ×2 (`reversion/backtest.py:109`, `vector/backtest.py:95`) | **A** | Byte-identical; reads only module-level `SLIPPAGE_PER_SIDE` (==0.0005 both) + engine-local `_TIER_ROUND_TRIP_COSTS`. Backtest-only. |
| 7 | cap-gate `healthcheck` ×2 (`reversion/plugs/capital_gate.py:83`, `vector/plugs/capital_gate.py:68`) | **A** | Byte-identical dict; only `engine` value differs (already `self.engine_name`). |
| 2 | `_load_prices` ×2 (`reversion/backtest.py:232`, `vector/backtest.py:228`) | **B** | SQL/parse identical; min-bar filter **intentionally** diverges (reversion `MA_50_PERIOD+5`, vector `SMA_200+5`) → parameterize `min_bars`, do NOT erase. |
| 1 | stale-order-cancel (`momentum/scheduler.py:495`, `sentinel/scheduler.py:344`) | **B** | 194 nodes, logic identical; only log-namespace prefix differs (sentinel docstring acknowledges the mirror). **Live-money** (cancels real broker orders). |
| 3 | `check_trade` ×2 (`reversion/plugs/capital_gate.py:95`, `vector/plugs/capital_gate.py:80`) | **B** | Logic byte-identical (4 reject branches + drawdown); divergence = log-event engine string + `DAILY_LOSS_FREEZE_PCT` source (both resolve to `0.05`). **Live-money risk gate.** |
| 4 | `assert_can_graduate` ×3 (momentum:140, reversion:141, vector:125) | **B** | Identical shape; only engine-name string + message differ; `is_graduated` stays per-engine (different thresholds). |
| 8 | `run_for_search` ×3 | **C** | Wrapper calls engine-specific `load_*_window_context`/`run_*_with_context`; consolidation = injecting 2 engine callables > the 6-line body. False-dedup. |
| 6,9,10,12,13,14,15 | trivial `__init__`/`__repr__`/dataclass dunders across unrelated classes | **C** | Coincidental AST-shape collision; no shared concept; consolidation couples unrelated modules. |
| intra-tpcore 1–13 (borrow/short-interest freshness, adapter `aclose`, calendar `next_*`) | **OUT of P5** | Real (esp. intra#1 borrow/SI-freshness, 198 nodes) but intra-`tpcore`, not cross-package engine dedup. Separate "tpcore-internal hygiene" follow-up — conflating inflates P5 blast radius. |

## 3. Consolidation homes + API (A/B)

All new code in `tpcore/` (engine→tpcore one-way; no engine→engine; no tpcore→engine):

- **`tpcore/backtest/cli_overrides.py`** (NEW) — #5: `overrides_from_args(args, keys) -> dict` (pure; engine passes its own `*_OVERRIDE_KEYS`). Each engine `_overrides_from_args` → 1-line delegate shim.
- **`tpcore/backtest/cost_model.py`** (EXISTS) — #11: `slippage_per_side(ticker, tier_round_trip_costs, default) -> float` (engine passes its own constant + tier dict; no engine state in tpcore).
- **`tpcore/backtest/price_loader.py`** (NEW) — #2: `async load_prices(pool, tickers, start, end, *, min_bars: int)` — `min_bars` is the explicit divergence parameter (reversion passes `MA_50_PERIOD+5`, vector `SMA_200+5`).
- **`tpcore/interfaces/capital_gate_base.py`** (NEW) — #3/#4/#7: `PerTradeCapitalGateBase(BaseEnginePlug)` with concrete `check_trade`/`healthcheck`/`assert_can_graduate`; subclass supplies `engine_name` (class attr), `_daily_loss_freeze_pct`, abstract `is_graduated` (thresholds stay per-engine). Log event names derive from `self.engine_name` — **observably identical** today; **the structlog event string is observable behavior** (forensics/dashboards may key on it) → characterization tests assert the exact emitted event name per engine.
- **`tpcore/order_management/stale_order_cancel.py`** (NEW) — #1: `async cancel_stale_orders(broker, *, order_prefix, log_namespace) -> int`; momentum/sentinel keep `_cancel_stale_*` as 1-line delegates.

## 4. Never-mask / fatal-objection self-check

- **No divergence bug to report:** the two highest-suspicion cases verified — `_load_prices` min-bar diff is intentional (different lookback windows), `check_trade`'s two `DAILY_LOSS_FREEZE_PCT` sources both `== 0.05`. Nothing masked.
- **Where could de-dup silently change a live result?** Only P5.4 (broker cancel) / P5.5 (risk gate). Bounded by: (a) characterization tests written **before** the refactor pinning exact outputs **incl. structlog event strings**; (b) a `_legacy_*` parallel-run differential test in the introducing PR (new == legacy over a fuzzed input grid) deleted at cutover; (c) **per-engine staged cutover** — never flip both engines' live paths in one PR; (d) the authoritative whole-suite single-process + module-order-flip gate per phase.
- **Excluded deliberately:** the 7 coincidental-dunder clusters, `run_for_search`, all intra-tpcore clusters (YAGNI / coupling / blast-radius).

## 5. Scope boundary

**OUT:** any cluster not in the §2 A/B set; intra-tpcore dedup (separate follow-up); behavioral change of ANY engine; touching the tracked-but-separate findings ([[momentum-aar-plug-finding]], the 13 orphan scripts, the `DBLogHandler.run_id` accessor); call-site cosmetic cleanup beyond the thin delegates.

## 6. Phasing (gated PR per phase; subagent-driven; ordered value×safety, lowest-risk first)

| Phase | Deliverable | Risk | Test strategy |
|---|---|---|---|
| **P5.1** | `tpcore/backtest/cli_overrides.py` + 3 engine delegate shims (#5) | pure, none | char-test: each engine helper returns identical dict for a representative `Namespace` pre/post |
| **P5.2** | `cost_model.slippage_per_side` (#11) + cap-gate `healthcheck` (#7) | pure, backtest/diag only | char-test: parametrized known/unknown ticker + tier dict; healthcheck dict equality per engine |
| **P5.3** | `tpcore/backtest/price_loader.py` (#2, `min_bars` param) | backtest drift | golden-fixture char-test: exact surviving-ticker set per engine pre/post on a fixed price fixture |
| **P5.4** | `tpcore/order_management/stale_order_cancel.py` (#1) | **live-money** | char-test FIRST (fake broker, mixed statuses/prefixes; assert cancelled IDs + count + **log event names** per engine); staged: tpcore fn + momentum delegate PR, then sentinel delegate PR |
| **P5.5** | `PerTradeCapitalGateBase` + `check_trade`/`assert_can_graduate` (#3,#4) | **highest — live risk gate** | exhaustive char-test FIRST (every reject branch, `drawdown == -0.05` boundary, `equity==0` skip, exact log events, `assert_can_graduate` raise/return matrix); `_legacy_*` parallel-diff in the intro PR; staged per-engine cutover (reversion → vector; momentum shares only `assert_can_graduate`, lands last); delete `_legacy_*` at cutover |

P5.1–P5.3 pure-mechanical. P5.4–P5.5 carry the staged-cutover + legacy-diff treatment.

## 7. Decisions (expert-recommended; operator-approved)

- **D1** intra-tpcore clusters → **OUT of P5**, separate follow-up (recorded).
- **D2** **dedicated `PerTradeCapitalGateBase(BaseEnginePlug)`** (NOT a `BaseEnginePlug` extension — batch engines must not inherit per-trade `check_trade`; mirrors the per-trade-only `BaseOrderManager` precedent).
- **D3** keep thin private delegate shims for the pure phases (minimal diff/risk); call-site cleanup is optional later cosmetic, not P5.
- **D4** no CLAUDE.md / best-practice conflict (consolidation respects `check_imports` layering + the `BaseOrderManager`/`tpcore.models.graduation` reuse precedent + the per-trade-vs-batch split).

**Design ready for the implementation plan.** 7 actionable clusters, 5 ordered gated-PR phases, behavior-preservation proven per phase (char-tests-before-refactor + the authoritative gate; legacy-diff + staged cutover for the two live-money phases); 21 clusters correctly excluded; no latent divergence bug.
