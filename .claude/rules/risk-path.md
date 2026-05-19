---
name: risk-path
paths:
  - "tpcore/risk/**"
description: "Path-scoped rule: live-money risk path. Every trade goes through RiskGovernor.check_trade uniformly; per-trade vs batch enforcement; idempotent close arbiter; reconcile_open_floor; never-fail-open."
---

# Risk path — live-money invariants

Canonical SoT: `tpcore/risk/__init__.py`, `tpcore/risk/limits_profile.py`, `tpcore/risk/batch_gate.py`, `tpcore/risk/state.py` (RiskStateStore).
Authoritative external: <https://code.claude.com/docs/en/extend>.

This is the heavy lane. Every change here is mandatorily routed through the full §1 pipeline of `docs/DEV_PIPELINE_STANDARD.md` (see `heavy-lane` rule).

Live-money invariants:

- **Every trade path goes through `tpcore.risk.RiskGovernor.check_trade()`** — uniformly enforced across all live engines.
- **Per-trade engines (reversion/vector) gate inside `BaseOrderManager.submit_decision`** → `check_trade()` + `record_fill()`.
- **Batch engines (momentum/sentinel) have no OrderManager.** Their scheduler submit loop calls the shared `tpcore.risk.batch_gate.gate_batch_order()` before each `broker.place_order`; closes go through the idempotent `RiskStateStore.record_close(engine, trade_id, pnl)` arbiter (backed by `platform.risk_close_ledger` PK `(engine,trade_id)`) — **never raw `record_fill(−1)`**.
- **Momentum/sentinel get `reconcile_open_floor=True`** (`tpcore.risk.limits_profile`): `check_trade` uses `effective = max(proxy, broker_floor)` as the position count — strictly tighter or equal, **never looser** (never-fail-open, A1 #88).
- **Per-engine `RiskLimits`** come from `tpcore.risk.limits_profile.limits_for()` (declarative SoT: momentum=200, sentinel=5, others default 8).
- **`RiskGovernor` emits `tpcore.risk.equity_unallocated` WARNING** when an engine's effective equity is still the 10000 placeholder.
- **Broker-error → BLOCK** (never fail-open, never crash; A1 #88).

Order semantics by engine: reversion/vector use Alpaca bracket orders (TP + SL together). Momentum uses day-market orders only — no per-name stops between monthly rebalances; risk managed by diversification + rotation. Sentinel uses day-market batch orders for the defensive ETF basket — no per-name stops, lifecycle-driven exits.

Never access private attributes (`._store`, `._pool`) on tpcore classes — extend with a public accessor; no new `# noqa: SLF001`.
