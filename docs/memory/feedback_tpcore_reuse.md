---
name: tpcore-reuse
description: "Always use tpcore for shared concerns; check existing engines for parity. When building a new engine, route every shared concern through tpcore (especially tpcore.calendar for session questions); grep existing engines for tpcore module usage and match."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 6626da25-0752-45ca-99c0-beeb2f8af7bb
---
When building a new engine, **every shared concern must route through `tpcore`**. CLAUDE.md is explicit: market hours via `exchange_calendars` (NYSE) — that means `tpcore.calendar`, not hand-rolled SQL queries against `platform.prices_daily` or `SPY`'s bars.

**Why:** Calendar questions ("is today a rebalance day", "what's the last trading day of the month", "is the market open right now") need to be answered by a single source of truth so all engines agree. A hand-rolled SQL query against SPY bars works *most* of the time but breaks on early-close days, NYSE-only-closed holidays, regional power outages that gap a single ticker's bars, etc. `exchange_calendars` is the authoritative source — `tpcore.calendar` wraps it.

**How to apply:** Before writing any helper that touches sessions, dates, or universe selection in a new engine, **grep the existing engines** (`grep -rn "from tpcore" reversion/ vector/ momentum/ sentinel/ canary/ catalyst/`) to see which `tpcore.*` modules they import. Match the pattern. Specifically:

- `tpcore.calendar` — every session-aware decision (rebalance day, trading-day delta, last-bar-of-month, early-close detection)
- `tpcore.db.build_asyncpg_pool` — DB pool construction (with `statement_cache_size=0` for the Supabase txn pooler)
- `tpcore.alpaca.AlpacaPaperBrokerAdapter` — broker
- `tpcore.risk.governor.RiskGovernor` (+ `RiskStateStore`) — risk gates
- `tpcore.aar.AARWriter` — AAR persistence
- `tpcore.backtest.cost_model.{load_tier_costs, get_round_trip_cost}` — tier-aware costs
- `tpcore.backtest.search.{BacktestRunResult, compute_search_metrics, write_trade_log_csv}` — parameter-search infra
- `tpcore.backtest.credibility.{BacktestCredibilityRubric, graduation_ready}` — credibility gate
- `tpcore.quality.validation.capital_gate.assert_passed` — pre-graduation gate
- `tpcore.interfaces.engine_plug.BaseEnginePlug` — plug ABC
- `tpcore.interfaces.broker.{Order, OrderClass, OrderSide, OrderType, TimeInForce}` — broker types
- `tpcore.engine_profile` — declarative engine roster SoT (cadence/lifecycle/dispatch_order)

**Concrete failure mode (2026-05-13):** When building the Momentum engine I wrote a SQL query against SPY bars in `momentum/plugs/lifecycle_analysis.py` to decide "is today the first trading day of the month?" That worked but violated the convention — the user caught it right before live paper submission and shut it down. The same hand-rolled date logic appeared in `momentum/backtest.py`'s `_month_end_dates_within`. Both had to be ripped out and replaced with `tpcore.calendar` before the kickoff could proceed.

The check is mechanical and one minute long: `grep -rn "from tpcore" {existing_engines}/` before writing any new plug. (Sigma is archived — `archive/sigma/` — and is NOT a parity reference any more.)
