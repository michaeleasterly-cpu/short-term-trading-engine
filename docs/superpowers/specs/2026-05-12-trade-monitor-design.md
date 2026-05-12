---
title: Trade Monitor — live event-driven order-lifecycle worker
status: draft
created: 2026-05-12
owner: trading-platform
scope: tpcore + sigma + reversion + vector
prereq: none (this work unblocks paper trading; once landed, remove TPCORE_SCAN_ONLY)
---

# Trade Monitor — Design

## Problem

The engines (Sigma, Reversion, Vector) submit two order payloads per
`ExecutionDecision`:

- **Tier 1** — a bracket BUY (or SELL for Reversion shorts) at market
  with TP at the mid-band / 20-day MA and SL at the hard stop.
- **Tier 2** — a GTC limit on the *opposite* side at the upper band /
  50-day MA, intended to scale out the second half of the position
  after Tier 1 fills.

The order managers (`sigma/order_manager.py`,
`reversion/order_manager.py`, `vector/order_manager.py`) submit both
payloads in a single call:

```python
placed = await self._broker.submit_execution_decision(decision)
# ↓ inside AlpacaPaperBrokerAdapter.submit_execution_decision:
# for payload in decision.order_payloads:
#     placed.append(await self.place_order(order))
```

Alpaca rejects the second payload with
`{"code":40310000,"message":"cannot open a short sell while a long buy order is open"}`
because you can't have an open opposing-side order on the same symbol.
The Tier 1 leg lands successfully; the Tier 2 leg never goes on the
book; the engine raises, treating the whole decision as failed and
leaving the Tier 1 as an orphan position with **no managed exit** apart
from the bracket TP/SL Alpaca itself manages.

In other words: the engine architecture *assumes* a state-aware
submitter that defers Tier 2 until Tier 1 fills, but no such submitter
exists. The cron-driven scheduler design is fundamentally limited for
any multi-leg or time-stop-bearing strategy.

## Solution: live trade monitor

A new long-running asyncio service, `tpcore/trade_monitor.py`, owns the
full lifecycle of every order the engines emit. The engines change to
emit *only Tier 1*; the monitor reacts to Alpaca's `trade_updates`
stream and submits Tier 2 (and cancellations, time stops, AAR writes)
in response to broker-observed state transitions.

### Components

#### 1. `platform.open_orders` (new table)

```sql
CREATE TABLE platform.open_orders (
    client_order_id   TEXT PRIMARY KEY,
    broker_order_id   TEXT,
    parent_client_id  TEXT,                       -- NULL on Tier 1; set on Tier 2
    engine            TEXT NOT NULL,              -- 'sigma' | 'reversion' | 'vector'
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,              -- 'buy' | 'sell'
    qty               NUMERIC NOT NULL,
    tier              SMALLINT NOT NULL,          -- 1 or 2
    order_class       TEXT NOT NULL,              -- 'bracket' | 'limit'
    status            TEXT NOT NULL,              -- 'new'|'accepted'|'filled'|'partial'|'cancelled'|'rejected'
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at         TIMESTAMPTZ,
    filled_qty        NUMERIC NOT NULL DEFAULT 0,
    avg_fill_price    NUMERIC,
    assessment_json   JSONB NOT NULL,             -- frozen at submission for reactive use
    decision_json     JSONB NOT NULL,             -- frozen at submission for reactive use
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX open_orders_status_idx ON platform.open_orders (status)
    WHERE status IN ('new', 'accepted', 'partial');
CREATE INDEX open_orders_engine_idx ON platform.open_orders (engine);
CREATE INDEX open_orders_parent_idx ON platform.open_orders (parent_client_id)
    WHERE parent_client_id IS NOT NULL;
```

Alembic migration: `platform/migrations/versions/20260513_0000_create_open_orders.py`.

The monitor uses this table to (a) rehydrate state on restart and
(b) match incoming stream events back to the assessment/decision that
spawned the order.

#### 2. `tpcore/trade_monitor.py` (new module)

```python
class TradeMonitor:
    """Long-running consumer of Alpaca trade_updates.

    Subscribes to alpaca.trading.stream.TradingStream, persists every
    in-flight order to platform.open_orders, and reacts to fill/cancel
    events by submitting Tier 2 legs, cancelling orphans, and writing
    AARs.
    """

    async def run_forever(self) -> None: ...

    async def on_trade_update(self, event: TradeUpdate) -> None:
        # event.event ∈ {'new','fill','partial_fill','canceled','rejected','done_for_day', ...}
        # Match by client_order_id → row in platform.open_orders.
        # On 'fill' / 'partial_fill' for a Tier 1 leg:
        #   - Persist the fill to open_orders + risk_state.
        #   - Look up the decision from open_orders.decision_json.
        #   - Build the Tier 2 limit order with parent_client_id = Tier 1's client_order_id.
        #   - Submit; persist the new open_orders row.
        # On 'canceled' / 'rejected' / SL fill of Tier 1:
        #   - If a Tier 2 was already submitted, cancel it.
        #   - Write the AAR (using assessment_json + decision_json).
        # On Tier 2 fill:
        #   - Write the final AAR.
```

Implementation notes:

- Each `on_trade_update` runs inside a per-event transaction
  (`async with pool.acquire() as conn: async with conn.transaction(): ...`)
  so partial state never persists.
- Idempotency: `event.execution_id` is deduplicated by an
  `INSERT ... ON CONFLICT DO NOTHING` on a small `processed_events`
  table; replaying a missed update is safe.
- Reconciliation on startup: `SELECT * FROM platform.open_orders WHERE
  status IN ('new','accepted','partial')`, then `broker.get_order()`
  for each — catches any state drift that happened while the worker
  was down.

#### 3. Engine order-manager refactor

Each `*/order_manager.submit_decision()`:

- Continues to run the capital gate + governor check (unchanged).
- **Submits only `decision.order_payloads[0]`** (the Tier 1 bracket)
  via a slimmed `submit_tier1_only(payload, decision, assessment)`
  primitive on the broker adapter.
- Persists the full `decision` + `assessment` to
  `platform.open_orders` keyed by Tier 1's `client_order_id` so the
  monitor can find them.
- Returns the placed Tier 1 order list (length 1).

The Tier 2 payload still lives on the `decision` for the monitor to
use; the order manager never submits it directly.

#### 4. `AlpacaPaperBrokerAdapter` adjustment

- New method `submit_tier1_only(payload) -> Order` — places exactly one
  order.
- `submit_execution_decision()` is **kept** for completeness but
  becomes a thin wrapper that submits Tier 1, persists to
  `open_orders`, and emits a `TIER1_SUBMITTED` event. Callers that
  haven't been refactored still work; they just don't get Tier 2.

### Deployment

Run the monitor as a dedicated Railway service:

```jsonc
// in railway.json
"trade-monitor": {
  "buildCommand": "...",
  "startCommand": "/app/.deps/bin/python -m tpcore.trade_monitor",
  "watchPatterns": ["tpcore/**/*.py", "pyproject.toml", "railway.json", ".python-version"],
  "ipv6EgressEnabled": true,
  "restartPolicyType": "always"  // persistent, not cron
}
```

The engine schedulers stay on their existing cron schedules. The
monitor is the only long-running service; if it goes down, the
engines stop submitting (the scan-only guard catches that — see
"Until the monitor lands" below).

### Testing

- **Unit**: a stream-event simulator that feeds synthetic
  `TradeUpdate` messages and asserts the monitor's state
  transitions / DB writes / Tier 2 submissions.
- **Integration**: run against the Alpaca paper account; submit a
  $100 bracket via the engine, observe the monitor pick up the fill,
  submit Tier 2, observe the second fill, AAR row appears.
- **Crash recovery**: kill the monitor mid-flight, restart, confirm
  it picks up state from `open_orders` and reconciles correctly.

## Until the monitor lands

The order managers now check `TPCORE_SCAN_ONLY`. When set to `true`,
`submit_decision()` returns `None` after the gates run, never reaching
the broker. The engines remain useful for scanning (they still emit
`UNIVERSE_SIMULATION` rows, run setup_detection, log signals) but
produce no live orders.

Recommended setting **today**: `export TPCORE_SCAN_ONLY=true` in the
local environment and any Railway service that runs an engine
scheduler. Remove the flag in the commit that ships the trade
monitor.

The smoke test (`scripts/smoke_test.py`) is unaffected — it bypasses
the order manager and calls `place_order` directly. Useful for
proving the broker is reachable without flipping `TPCORE_SCAN_ONLY`
off.

## Out of scope (intentionally)

- **Real-time price streaming.** The monitor only listens to
  `trade_updates` (broker → us). Engine entries still use end-of-day
  bars from `platform.prices_daily`. Adding live bar streaming is a
  separate piece of work; the daily-bars cadence is correct for
  Sigma/Reversion/Vector.
- **Position sizing override.** The "$100 per trade" idea from the
  paper-trading-test prompt belongs in the engine constants
  (`sigma.models.PRE_GRAD_POSITION_CAP_USD` etc.), not in the
  monitor. The monitor sizes per the decision it receives.
- **Cross-engine concurrency limits.** Each engine already enforces
  `MAX_CONCURRENT_POSITIONS` via the capital gate; the monitor
  trusts those upstream and doesn't add its own concurrency cap.

## Acceptance

- `platform.open_orders` migration applied.
- `tpcore/trade_monitor.py` exists with `TradeMonitor.run_forever` and
  unit-tested `on_trade_update` handlers.
- Engine order managers refactored to submit Tier 1 only.
- Integration test: submitting one Sigma decision produces Tier 1,
  Tier 2 fires after Tier 1 fill, both AARs land in
  `platform.aar_events`.
- `TPCORE_SCAN_ONLY` removed from the order managers in the same PR
  that wires the monitor on Railway.
- `docs/OPERATIONS.md` §1 grows a `trade-monitor` row in the service
  table; §11 grows a troubleshooting entry for "monitor lag".
