---
name: engine-inventory-2026-05-23
description: "Full inventory of every engine in the platform — what each one does, direction (long/short), cadence, lifecycle state, data dependencies, and which engines could consume options vs macro-sentiment signals"
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Standing reference for every session that asks 'what does engine X do?'.** Authoritative source: `tpcore/engine_profile.py::_PROFILE` (which I should re-read if any field looks stale).

## Engines in the roster (7 production + 1 RETIRED + 2 special)

| Engine | Role | Cadence | Direction | Lifecycle | Data deps |
|---|---|---|---|---|---|
| **reversion** | Mean-reversion per-trade (bracket orders); SATELLITE since 2026-05-15 | DAILY | Long-only (mean revert UP from oversold) | PAPER | prices_daily, fundamentals_quarterly |
| **vector** | Momentum swing per-trade | DAILY | Long-only | PAPER | prices_daily, fundamentals_quarterly, earnings_events |
| **momentum** | Cross-sectional 12-1 batch-monthly rebalance | MONTHLY (first trading day) | Long-only top decile | PAPER | earnings_events, liquidity_tiers, prices_daily |
| **sentinel** | **MACRO DEFENSE / inverse — allocates up to 20% capital to defensive basket (SH, PSQ, TLT, GLD, SQQQ) on bear-regime confirm.** Only inverse engine in production. Activates on 3-day Bear Score ≥ 60 with no SPY counter-trend >5%. | DAILY | **Macro-level inverse via defensive ETFs** | PAPER | prices_daily, macro_indicators |
| **canary** | DELIBERATELY non-graduating heartbeat (1-share SPY round-trip); exercises DA-1/DA-2/AAR/forensics dispatch paths without signal risk. No `write_credibility_score`. Allocator-excluded by omission. | DAILY | Long 1 share | PAPER | prices_daily |
| **catalyst** | Insider-cluster swing (≥3 distinct Form-4 BUYs in cluster window + trend filter close>50-SMA + liquidity gate) | DAILY | Long-only | PAPER | earnings_events, prices_daily, sec_insider_transactions |
| **carver** | Carver-method vol-targeted monthly portfolio (12 forecasts, IDM-bounded, 12 flips/year speed limit) | MONTHLY (first trading day) | Long-only | LAB | (LAB stage) |
| ~~sigma~~ | RETIRED (data-SDLC RETIRED symmetry) | — | — | RETIRED | — |
| allocator | Capital allocation across engines (not an alpha source; separate dispatch path) | WEEKLY (first trading day) | — | PAPER | prices_daily |
| lab | LifecycleState.LAB proof — NOT a runnable engine | DAILY (inert placeholder) | — | LAB | — |

## S2 — REJECTED short-squeeze engine

**Never built.** Originally specced as satellite (5-10% cap, per-trade graduation parallel to Reversion). Was meant to consume FINRA short-interest + securities-lending + options-derived signals (IV skew, put/call OI, gamma-weighted strike concentration).

**Rejected 2026-05-15** because point-in-time SL utilization + options-positioning history not available within budget. The 113K rows in `platform.tradier_options_chains` were the prepared substrate for S2's Layer 1.5 (deferred options signals); they've been dormant since.

**2026-05-23 boundary refinement** (operator probed the exact gap):
- HARD blockers: SL utilization + available supply (only S3 Partners/DataLend/Markit at $50k+/yr have it)
- Structural: daily short-interest impossible (FINRA biweekly by regulation)
- API-rate impractical: Tradier historical options chains work per-option-symbol but ~4.5M calls for 2y backtest
- Soft blockers: iborrowdesk forward rates (we'd accumulate, no history); options daily snapshots (Tradier works going forward)

**Two paths forward:**
- **Reduced S2** (proxy-based): FINRA biweekly + tightening borrow rate + put/call OI surge + IV skew widening. Different strategy than the original spec — trades on proxies that historically correlate with utilization-driven squeezes but aren't the causal signal.
- **Full S2**: requires acquiring S3 Partners-tier SL utilization vendor. Budget decision.

## Direction map for options vs macro-sentiment consumption

Operator question 2026-05-23: which engine consumes options data vs macro sentiment.

- **Macro-level engine that wants options-derived signals**: SENTINEL (the macro-defense engine). Options inputs that fit at macro level: VIX term structure, SKEW index, market-wide put/call OI, SPX gamma exposure. Enriches the Bear Score input.
- **Ticker-level engines that benefit from macro sentiment as REGIME GATE**: Reversion, Vector, Catalyst, Momentum, Carver. They're long-only; macro sentiment (AAII, fear_greed, hy_spread) throttles position sizing in risk-off regimes. They don't need options data unless a specific candidate spec calls for ticker-level options skew.
- **Hypothetical ticker-level inverse engine**: would consume per-ticker options skew + open interest + borrow rate (when it exists). Maps to S2 (reduced or full).

## How options data was originally prepared

`platform.tradier_options_chains` — 113,834 rows from 2026-05-10 one-shot export, 50 tickers, expirations through 2028-12-15. Prepared specifically for S2's Layer 1.5. Tradier API still works (verified 2026-05-23) — `scripts/refresh_tradier_options.py` + `scripts/run_refresh_tradier_options.sh` exist as dormant infrastructure for when S2 (or another engine) consumes it.

## Related

- [[database-architecture-state-2026-05-23]] — schema dependencies
- `tpcore/engine_profile.py::_PROFILE` — authoritative source
- `docs/MASTER_PLAN.md` §4.x — per-engine design sections
- `TODO.md:783` — S2 rejection record
- `docs/runbooks/options-data-turn-on.md` — when to turn on options data
