# Risk Governor — Make It Real & Uniform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tpcore.risk.RiskGovernor` a real, mandatory, uniformly-enforced trade gate across all 4 live engines — it is currently inert (frozen placeholder state) and wired into only 2/4 engines.

**Architecture:** RiskGovernor already lives in `tpcore` (correct). The gaps: (A) per-trade engines (reversion/vector) wire `check_trade`/`record_fill` but state is unexercised; (B) batch engines (momentum/sentinel) have **no** OrderManager, never call `check_trade`, never `record_fill`; (C) `RiskLimits` is process-global so `max_open_positions=8` cannot fit momentum's ~130-name decile. Fix = per-engine limits + one shared tpcore enforcement helper called from every engine's submit path + batch engines recording fills so caps are real.

**Tech Stack:** Python 3.11, asyncpg, Pydantic v2, structlog, pytest. No new deps.

---

## ⚠ DESIGN DECISIONS — RESOLVE BEFORE EXECUTION

These are genuine forks. Operator decision required; the plan below assumes the **Recommended** option and is written to it. Changing a decision changes the corresponding tasks.

**D1 — Per-engine RiskLimits mechanism.**
`RiskGovernor.__init__` does `self._limits = limits or RiskLimits()` (`tpcore/risk/governor.py:170-ish`) — one limit set for all engines. `max_open_positions=8` blocks momentum (decile ≈ 130 names) after 8.
- **(Recommended) D1a:** Governor holds `dict[str, RiskLimits]` keyed by engine; `register_engine(engine_id, engine_equity, limits=None)` records per-engine limits; `check_trade` uses `self._engine_limits.get(engine_id, self._default_limits)`. No DB migration (limits are config, set at registration each run). Backward-compatible: reversion/vector pass nothing → default unchanged.
- D1b: Add `limits_json` column to `platform.risk_state` (migration) — persists limits. Heavier; only needed if limits must survive without re-registration. **Not recommended** (engines register every run).
- D1c: Keep global limits; batch engines skip the `max_open_positions` check only. **Rejected** — breaks symmetry (persona), and silently drops a real control.

**D2 — Batch per-order semantics.**
Momentum/Sentinel submit in a per-name loop (`momentum/scheduler.py:388`, `sentinel/scheduler.py:225`). `daily/weekly loss cap` + `kill_switch` are portfolio-level; `max_open_positions` + `net_long` are incremental.
- **(Recommended) D2a:** Call `check_trade` per name inside the existing loop (same as per-trade engines). A BLOCK skips that name and is logged; the rebalance continues with the rest (partial rebalance is acceptable and is what the caps are *for*). `record_fill(position_delta=+1)` on successful submit; exits decrement via the existing position-close path (Task B4).
- D2b: One portfolio-level pre-check before the loop (sum notional vs caps) + no per-name calls. Simpler but the net-long/position-count caps become coarse and diverge from per-trade semantics. **Not recommended.**

**D3 — Real `engine_equity` source.**
The allocator (`tpcore/allocator/service.py:497-506`) already writes real `engine_equity` into `platform.risk_state`; `register_engine` is idempotent and **won't clobber** an existing row (`governor.py:186-204`). So once the allocator has run, equity is real. Risk: if an engine trades before the allocator ever ran, equity is the `Decimal("10000")` placeholder.
- **(Recommended) D3a:** Add a startup guard: if `state.engine_equity == Decimal("10000")` AND no allocator decision row exists, log `WARNING tpcore.risk.equity_unallocated` (visible, non-blocking) — surfaces the placeholder instead of silently gating on a fiction. Do **not** auto-pull broker equity (allocator owns capital policy; two writers = drift).
- D3b: Governor pulls live equity from `broker.get_account().equity` at registration. **Not recommended** — conflicts with allocator as the single capital authority.

---

## File Structure

- `tpcore/risk/governor.py` — MODIFY: per-engine limits (D1a); `register_engine` gains `limits` param; `check_trade` reads per-engine limits; `_maybe_reset_counters`/`record_fill` unchanged.
- `tpcore/risk/batch_gate.py` — CREATE: one shared `gate_batch_order(...)` coroutine — the single canonical enforcement+record helper for engines without an OrderManager (persona: one canonical way).
- `tpcore/risk/limits_profile.py` — CREATE: per-engine `RiskLimits` profile (declarative, like `tpcore.feeds` profile) — the single source of truth for each engine's caps.
- `momentum/scheduler.py` — MODIFY: register engine with profile limits; per-name `gate_batch_order` before `broker.place_order` (line ~388); record fills.
- `sentinel/scheduler.py` — MODIFY: same at line ~225.
- `tpcore/templates/engine_template/scheduler.py` — MODIFY: document the batch-gate pattern so new batch engines inherit it.
- `docs/superpowers/checklists/engine_readiness.md` — MODIFY: make "every trade path through check_trade" explicit for BOTH OrderManager and batch-scheduler engines.
- `scripts/audit_data_pipeline.py` — MODIFY: add a `known_knowns` check asserting every live engine has a recent SIGNAL/submit path that called the governor (regression guard).
- Tests: `tpcore/tests/test_risk_governor.py` (extend), `tpcore/risk/tests/test_batch_gate.py` (create), `tpcore/risk/tests/test_limits_profile.py` (create), `momentum/tests/test_scheduler_governor.py` (create), `sentinel/tests/test_scheduler_governor.py` (create).

---

### Task 0: Lock in current (inert) behavior with a characterization test

**Files:**
- Test: `tpcore/tests/test_risk_governor.py`

- [ ] **Step 1: Write a test asserting per-engine limits do not yet exist (red-by-design baseline)**

```python
def test_register_engine_does_not_yet_accept_limits():
    import inspect
    from tpcore.risk.governor import RiskGovernor
    sig = inspect.signature(RiskGovernor.register_engine)
    assert "limits" not in sig.parameters  # baseline; Task 1 flips this
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tpcore/tests/test_risk_governor.py::test_register_engine_does_not_yet_accept_limits -v`
Expected: PASS (documents the starting point).

- [ ] **Step 3: Commit**

```bash
git add tpcore/tests/test_risk_governor.py
git commit -m "test: characterize pre-fix RiskGovernor (no per-engine limits)"
```

---

### Task 1: Per-engine RiskLimits in the governor (D1a)

**Files:**
- Modify: `tpcore/risk/governor.py` (`__init__`, `register_engine`, `check_trade`)
- Test: `tpcore/tests/test_risk_governor.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from decimal import Decimal
from tpcore.risk.governor import RiskGovernor, RiskLimits, InMemoryRiskStateStore
from tpcore.interfaces.broker import OrderSide

@pytest.mark.asyncio
async def test_per_engine_limits_override_default(fake_broker):
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=fake_broker)
    await gov.register_engine("reversion", Decimal("10000"))  # default limits
    await gov.register_engine(
        "momentum", Decimal("10000"),
        limits=RiskLimits(max_open_positions=150),
    )
    # momentum tolerates 150 positions; reversion still capped at 8
    st = await gov.state_for("momentum")
    st = st.model_copy(update={"open_positions": 120})
    await gov._store.put(st)
    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)
    assert res.decision.name == "ALLOW"

    rv = await gov.state_for("reversion")
    rv = rv.model_copy(update={"open_positions": 9})
    await gov._store.put(rv)
    res2 = await gov.check_trade("reversion", Decimal("100"), OrderSide.BUY)
    assert res2.decision.name == "BLOCK"
    assert "max concurrent positions" in (res2.reason or "")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tpcore/tests/test_risk_governor.py::test_per_engine_limits_override_default -v`
Expected: FAIL — `register_engine() got an unexpected keyword argument 'limits'`.

- [ ] **Step 3: Implement per-engine limits**

In `tpcore/risk/governor.py` `__init__`, replace the limits assignment:

```python
        self._default_limits = limits or RiskLimits()
        self._engine_limits: dict[str, RiskLimits] = {}
```

Replace every later `self._limits` reference in `check_trade` with a local resolved at the top of `check_trade` (right after `state` is loaded):

```python
        limits = self._engine_limits.get(engine_id, self._default_limits)
```

…and use `limits.daily_loss_pct`, `limits.weekly_loss_pct`, `limits.max_open_positions`, `limits.platform_net_long_cap_pct` in the four cap checks.

Extend `register_engine`:

```python
    async def register_engine(
        self,
        engine_id: str,
        engine_equity: Decimal,
        limits: RiskLimits | None = None,
    ) -> RiskState:
        if limits is not None:
            self._engine_limits[engine_id] = limits
        existing = await self._store.get(engine_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        state = RiskState(
            engine=engine_id,
            engine_equity=engine_equity,
            daily_reset_at=next_open(now),
            weekly_reset_at=next_monday_open(now),
        )
        await self._store.put(state)
        logger.info("tpcore.risk.engine_registered",
                    engine=engine_id, equity=str(engine_equity),
                    limits=limits.model_dump(mode="json") if limits else "default")
        return state
```

Update the Task 0 baseline test to assert `"limits" in sig.parameters` (it has now flipped — expected).

- [ ] **Step 4: Run all governor tests**

Run: `.venv/bin/python -m pytest tpcore/tests/test_risk_governor.py -v`
Expected: PASS (including the existing 8 cap/kill/cost tests — they use default limits, unchanged).

- [ ] **Step 5: Verify no other tpcore consumer of `self._limits` broke**

Run: `grep -rn "_limits" tpcore/ --include='*.py' | grep -v test`
Expected: only `governor.py` references; confirm each is `_default_limits` or the resolved local. CLAUDE.md mandate: tpcore change → check all consumers. `grep -rn "register_engine\|RiskGovernor(" reversion/ vector/ momentum/ sentinel/ archive/ ops/` and confirm none break (extra kwarg is optional).

- [ ] **Step 6: Commit**

```bash
git add tpcore/risk/governor.py tpcore/tests/test_risk_governor.py
git commit -m "feat(risk): per-engine RiskLimits (D1a) — default unchanged for per-trade engines"
```

---

### Task 2: Declarative per-engine limits profile

**Files:**
- Create: `tpcore/risk/limits_profile.py`
- Test: `tpcore/risk/tests/test_limits_profile.py`

- [ ] **Step 1: Write failing test**

```python
from decimal import Decimal
from tpcore.risk.limits_profile import limits_for

def test_momentum_basket_sized_limits():
    lim = limits_for("momentum")
    assert lim.max_open_positions >= 130  # decile basket fits
def test_sentinel_basket_sized_limits():
    assert limits_for("sentinel").max_open_positions >= 5
def test_per_trade_engines_use_default():
    assert limits_for("reversion").max_open_positions == 8
    assert limits_for("vector").max_open_positions == 8
def test_unknown_engine_returns_default():
    assert limits_for("does_not_exist").max_open_positions == 8
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tpcore/risk/tests/test_limits_profile.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the profile**

Create `tpcore/risk/limits_profile.py`:

```python
"""Single source of truth for each engine's RiskLimits.

Per-trade engines (reversion/vector) use the default 8-position cap.
Batch engines hold a basket far larger than 8, so their position cap is
sized to the basket (momentum ≈ decile of T1+T2 universe; sentinel = 5
ETFs). Loss-cap / net-long percentages stay platform-uniform unless an
engine genuinely needs otherwise — change here, nowhere else.
"""
from __future__ import annotations

from tpcore.risk.governor import RiskLimits

_PROFILE: dict[str, RiskLimits] = {
    # Batch: decile of ~1,500-name T1+T2 universe → cap with headroom.
    "momentum": RiskLimits(max_open_positions=200),
    # Batch: fixed 5-ETF defensive basket.
    "sentinel": RiskLimits(max_open_positions=5),
    # reversion / vector: omitted → default RiskLimits() (8).
}


def limits_for(engine_id: str) -> RiskLimits:
    """RiskLimits for an engine; default (8-pos) if not profiled."""
    return _PROFILE.get(engine_id, RiskLimits())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tpcore/risk/tests/test_limits_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tpcore/risk/limits_profile.py tpcore/risk/tests/test_limits_profile.py
git commit -m "feat(risk): declarative per-engine limits profile (SoT)"
```

---

### Task 3: Shared batch enforcement helper

**Files:**
- Create: `tpcore/risk/batch_gate.py`
- Test: `tpcore/risk/tests/test_batch_gate.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from decimal import Decimal
from tpcore.risk.governor import RiskGovernor, InMemoryRiskStateStore, RiskDecision
from tpcore.risk.limits_profile import limits_for
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.interfaces.broker import OrderSide

@pytest.mark.asyncio
async def test_gate_allows_and_records_position(fake_broker):
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=fake_broker)
    await gov.register_engine("sentinel", Decimal("10000"), limits=limits_for("sentinel"))
    ok = await gate_batch_order(gov, "sentinel", ticker="SH",
                                notional=Decimal("3500"), direction=OrderSide.BUY)
    assert ok is True
    st = await gov.state_for("sentinel")
    assert st.open_positions == 1  # record_fill(+1) ran

@pytest.mark.asyncio
async def test_gate_blocks_on_kill_switch(fake_broker):
    store = InMemoryRiskStateStore()
    gov = RiskGovernor(state_store=store, broker=fake_broker)
    await gov.register_engine("sentinel", Decimal("10000"))
    await store.set_kill_switch_all(active=True, reason="test")
    ok = await gate_batch_order(gov, "sentinel", ticker="SH",
                                notional=Decimal("3500"), direction=OrderSide.BUY)
    assert ok is False
    assert (await gov.state_for("sentinel")).open_positions == 0  # not recorded
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tpcore/risk/tests/test_batch_gate.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the helper**

Create `tpcore/risk/batch_gate.py`:

```python
"""Canonical risk gate for engines WITHOUT an OrderManager.

Per-trade engines gate inside BaseOrderManager.submit_decision. Batch
engines (momentum/sentinel) submit in a per-name scheduler loop; this is
the single shared function they call before each broker.place_order so
the governor enforcement is identical everywhere (persona: one canonical
way, not N variants). On ALLOW it records the opened position so
open_positions / loss caps become real for batch engines too.
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from tpcore.interfaces.broker import OrderSide
from tpcore.risk.governor import RiskDecision, RiskGovernor

logger = structlog.get_logger(__name__)


async def gate_batch_order(
    governor: RiskGovernor,
    engine_id: str,
    *,
    ticker: str,
    notional: Decimal,
    direction: OrderSide,
    expected_edge_pct: Decimal | None = None,
) -> bool:
    """True iff the order passed the governor (and was recorded as open).

    A False return means SKIP this name and continue the rebalance — a
    blocked name must not abort the whole batch.
    """
    check = await governor.check_trade(
        engine_id=engine_id,
        size=notional,
        direction=direction,
        ticker=ticker,
        expected_edge_pct=expected_edge_pct,
    )
    if check.decision is RiskDecision.BLOCK:
        logger.warning(
            "tpcore.risk.batch_order_blocked",
            engine=engine_id, ticker=ticker,
            notional=str(notional), reason=check.reason,
        )
        return False
    await governor.record_fill(
        engine_id=engine_id, realized_pnl=Decimal("0"), position_delta=1,
    )
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tpcore/risk/tests/test_batch_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tpcore/risk/batch_gate.py tpcore/risk/tests/test_batch_gate.py
git commit -m "feat(risk): shared gate_batch_order helper for OrderManager-less engines"
```

---

### Task 4: Wire momentum scheduler into the governor

**Files:**
- Modify: `momentum/scheduler.py` (registration ~line 235; submit loop ~line 388)
- Test: `momentum/tests/test_scheduler_governor.py`

- [ ] **Step 1: Write failing test** (governor is called per submitted name)

```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_momentum_gates_every_order_through_governor(momentum_run_fixture):
    # fixture builds a MomentumScheduler with 3 target names + fake broker
    with patch("momentum.scheduler.gate_batch_order", new=AsyncMock(return_value=True)) as g:
        await momentum_run_fixture.run_once()
    assert g.await_count == 3  # one gate call per submitted name
```

(If `momentum_run_fixture` does not exist, this task's Step 1 also adds a
minimal fixture in `momentum/tests/conftest.py` constructing the
scheduler with `_submit=True`, a fake broker recording `place_order`
calls, and 3 ranked candidates — mirror the existing momentum test
fixtures; do not invent broker behavior, reuse `tests` fakes.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest momentum/tests/test_scheduler_governor.py -v`
Expected: FAIL — `gate_batch_order` not imported/called in `momentum/scheduler.py`.

- [ ] **Step 3: Implement wiring**

In `momentum/scheduler.py`, add import near the top:

```python
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.limits_profile import limits_for
```

At the registration site (currently `governor = RiskGovernor(state_store=state_store, broker=broker, pool=pool)` ~line 235), add immediately after construction:

```python
            await governor.register_engine(
                "momentum", self._engine_equity, limits=limits_for("momentum"),
            )
```

In the submit loop (`for order in sells + buys:` ~line 379), replace the body around line 388:

```python
                placed = await broker.place_order(self._payload_to_order(order))
```

with:

```python
                side = OrderSide.SELL if order in sells else OrderSide.BUY
                gated = await gate_batch_order(
                    governor, "momentum",
                    ticker=order.ticker,
                    notional=Decimal(str(order.notional_usd)),
                    direction=side,
                )
                if not gated:
                    failed.append((order.ticker, "governor_blocked"))
                    continue
                placed = await broker.place_order(self._payload_to_order(order))
```

(Confirm the order payload's notional field name by reading the
`_payload_to_order` builder and the order dataclass in
`momentum/scheduler.py`; use the real attribute — do not assume
`notional_usd` if the code uses another name.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest momentum/tests/test_scheduler_governor.py momentum/tests -v`
Expected: PASS (new test + existing momentum suite still green).

- [ ] **Step 5: Commit**

```bash
git add momentum/scheduler.py momentum/tests/
git commit -m "feat(momentum): enforce RiskGovernor per order via shared batch gate"
```

---

### Task 5: Wire sentinel scheduler into the governor

**Files:**
- Modify: `sentinel/scheduler.py` (registration ~line 119; submit loop ~line 225)
- Test: `sentinel/tests/test_scheduler_governor.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_sentinel_gates_every_etf_through_governor(sentinel_run_fixture):
    with patch("sentinel.scheduler.gate_batch_order", new=AsyncMock(return_value=True)) as g:
        await sentinel_run_fixture.run_once()
    assert g.await_count == sentinel_run_fixture.expected_basket_size  # one per ETF
```

(Add a minimal `sentinel/tests/conftest.py` fixture if absent, mirroring
existing sentinel scheduler tests; the basket is the ACTIVE-phase
5-ETF set from `sentinel/models.py:BASKET_WEIGHTS_DEFAULT`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest sentinel/tests/test_scheduler_governor.py -v`
Expected: FAIL — `gate_batch_order` not used in `sentinel/scheduler.py`.

- [ ] **Step 3: Implement wiring**

In `sentinel/scheduler.py` add imports:

```python
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.limits_profile import limits_for
```

After governor construction (`governor = RiskGovernor(state_store=state_store, broker=broker, pool=pool)` ~line 119) add:

```python
            await governor.register_engine(
                "sentinel", self._engine_equity, limits=limits_for("sentinel"),
            )
```

(If `SentinelScheduler` has no `self._engine_equity`, read how sentinel
sizes the basket — `sentinel/plugs/execution_risk.py` uses a
`deployable` equity; pass that same value to `register_engine`. Use the
real field; do not introduce a new equity source.)

In the submit loop (`for order in sells + buys:` ~line 219) wrap the
`placed = await broker.place_order(self._build_order(order))` (line ~225)
exactly as in Task 4 Step 3 (same pattern, `"sentinel"` engine id,
`gate_batch_order`, `failed.append((order.ticker, "governor_blocked")); continue`).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest sentinel/tests/test_scheduler_governor.py sentinel/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sentinel/scheduler.py sentinel/tests/
git commit -m "feat(sentinel): enforce RiskGovernor per ETF via shared batch gate"
```

---

### Task 6: Position-close path for batch engines (make caps real, not just incrementing)

**Files:**
- Modify: `momentum/scheduler.py`, `sentinel/scheduler.py` (the rebalance computes which prior holdings are being exited — emit `record_fill(position_delta=-1)` per closed name)
- Test: `momentum/tests/test_scheduler_governor.py`, `sentinel/tests/test_scheduler_governor.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_momentum_decrements_open_positions_on_exit(momentum_exit_fixture):
    # fixture: governor has open_positions=5; rebalance closes 2 prior names
    gov = momentum_exit_fixture.governor
    await momentum_exit_fixture.run_once()
    st = await gov.state_for("momentum")
    # 2 closed (−2) + N new opened (+N); assert the −2 happened
    assert momentum_exit_fixture.recorded_exits == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest momentum/tests/test_scheduler_governor.py::test_momentum_decrements_open_positions_on_exit -v`
Expected: FAIL — no exit-side `record_fill`.

- [ ] **Step 3: Implement**

In each scheduler, the `sells` list already represents positions being
closed (read the existing `sells`/`buys` construction). For each
successfully-submitted SELL that closes a prior holding, after
`broker.place_order` succeeds add:

```python
                if side is OrderSide.SELL:
                    await governor.record_fill(
                        engine_id="momentum",  # or "sentinel"
                        realized_pnl=Decimal("0"),  # realized P&L lands via AAR/trade_monitor; here we only free the slot
                        position_delta=-1,
                    )
```

Rationale & honesty: realized P&L for batch day-market exits is
reconciled through the existing AAR path; `gate_batch_order` only tracks
the *slot* (open_positions) so `max_open_positions` is real. Do not
double-count P&L. Document this in a comment at the call site.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest momentum/tests sentinel/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add momentum/scheduler.py sentinel/scheduler.py momentum/tests/ sentinel/tests/
git commit -m "feat(batch): decrement governor open_positions on rebalance exits"
```

---

### Task 7: Un-allocated equity guard (D3a)

**Files:**
- Modify: `tpcore/risk/governor.py` (`register_engine` — emit warning when equity is the placeholder and no allocator row exists)
- Test: `tpcore/tests/test_risk_governor.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_register_warns_when_equity_is_unallocated_placeholder(fake_broker, caplog):
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=fake_broker)
    await gov.register_engine("momentum", Decimal("10000"))
    assert any("equity_unallocated" in r.message or "equity_unallocated" in str(r)
               for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tpcore/tests/test_risk_governor.py::test_register_warns_when_equity_is_unallocated_placeholder -v`
Expected: FAIL — no such warning.

- [ ] **Step 3: Implement**

In `register_engine`, after computing `state` for a NEW engine (no
existing row) and before/after `put`, add:

```python
        if engine_equity == Decimal("10000"):
            logger.warning(
                "tpcore.risk.equity_unallocated",
                engine=engine_id,
                detail="engine_equity is the 10000 placeholder — allocator "
                       "has not set real capital; caps are evaluated against "
                       "a fiction until tpcore.allocator runs",
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tpcore/tests/test_risk_governor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tpcore/risk/governor.py tpcore/tests/test_risk_governor.py
git commit -m "feat(risk): warn when an engine registers on the 10000 equity placeholder"
```

---

### Task 8: Regression guard — audit asserts every live engine gates trades

**Files:**
- Modify: `scripts/audit_data_pipeline.py` (`run_known_knowns` — new check `governor_enforcement`)
- Modify: `docs/superpowers/checklists/engine_readiness.md`
- Test: manual audit run (no unit harness for the audit script — verify live)

- [ ] **Step 1: Implement the audit check**

In `scripts/audit_data_pipeline.py` `run_known_knowns`, before `return findings`, add a check that, for each engine in `("reversion","vector","momentum","sentinel")`, queries `platform.application_log` for a governor signal in the last 30d (either `tpcore.risk.fill_recorded` / `tpcore.risk.batch_order_blocked` / `tpcore.risk.engine_registered`) — absence for an engine that produced SIGNAL/submit events is a `WARN` ("engine submitted but no governor activity — possible bypass"). Mirror the existing `signal_silence` check shape (lines ~922-944) for symmetry.

- [ ] **Step 2: Update the checklist**

In `docs/superpowers/checklists/engine_readiness.md`, replace the single
"Every trade path runs through RiskGovernor.check_trade()" bullet with
two explicit cases:
- OrderManager engines: `submit_decision` calls `check_trade` + `record_fill` (reversion/vector pattern).
- Batch-scheduler engines (no OrderManager): the per-name submit loop calls `tpcore.risk.batch_gate.gate_batch_order` + records exits (momentum/sentinel pattern).

- [ ] **Step 3: Verify the audit runs and the new check appears**

Run: `./scripts/run_audit_data_pipeline.sh --phase known_knowns 2>&1 | grep governor_enforcement`
Expected: a finding line for `governor_enforcement` (OK or WARN — engines are pre-graduation so WARN is acceptable and correct; the point is the check exists and can never silently regress).

- [ ] **Step 4: Commit**

```bash
git add scripts/audit_data_pipeline.py docs/superpowers/checklists/engine_readiness.md
git commit -m "feat(audit): governor_enforcement regression check + checklist for batch engines"
```

---

### Task 9: Clean up stale archived-Sigma risk_state row

**Files:**
- Data only (no code): `platform.risk_state` has a `sigma` row (archived 2026-05-16).

- [ ] **Step 1: Confirm and delete via the canonical path**

This is a data mutation — it must be audit-logged (no raw DELETE). Add a
one-line idempotent cleanup to the allocator's `run_once` (it already
owns `risk_state` writes): skip/prune engines not in `self._engines`.
Implement: after persisting allocations, `DELETE FROM platform.risk_state
WHERE engine NOT IN (<live engines>)` wrapped with an
`application_log` audit row (mirror existing allocator mutation logging).
TDD: add `tpcore/allocator/tests` case asserting a non-live engine row is
pruned and the deletion is logged.

- [ ] **Step 2: Commit**

```bash
git add tpcore/allocator/ tpcore/allocator/tests/
git commit -m "fix(allocator): prune risk_state rows for non-live engines (stale sigma)"
```

---

### Task 10: Full verification gate

- [ ] **Step 1: Pre-commit gate (persona)**

```bash
.venv/bin/ruff check tpcore/ momentum/ sentinel/ scripts/
.venv/bin/python -m pytest -q tpcore/ momentum/ sentinel/
bash -n scripts/run_data_operations.sh scripts/run_all_engines.sh
```
Expected: all green.

- [ ] **Step 2: Live effectiveness re-audit (proof the governor now does something)**

Re-run the Task-0-style live query (`platform.risk_state` + `application_log`)
after a paper run; expected: `tpcore.risk.engine_registered` for all 4
engines with profile limits, and `tpcore.risk.batch_order_blocked` /
`fill_recorded` events appearing once engines submit. Document the
before/after in the PR body.

- [ ] **Step 3: Final commit / PR**

```bash
git push -u origin <branch>
gh pr create --title "Risk Governor: real state + uniform 4-engine enforcement" --body "..."
```

---

## Self-Review

**Spec coverage:** A (real state) → Tasks 6,7 + reuse of allocator(D3)/trade_monitor; B (uniform enforcement) → Tasks 3,4,5,8; C (per-engine limits) → Tasks 1,2. Decisions D1/D2/D3 surfaced up front. Stale sigma → Task 9. Regression-proofing → Task 8. ✅ no gap.

**Placeholder scan:** No "TBD"/"handle errors" — every code step has real code; the few "confirm the real attribute name" notes are explicit verification instructions (the engineer must read the order dataclass), not hidden work.

**Type consistency:** `gate_batch_order(governor, engine_id, *, ticker, notional, direction, expected_edge_pct=None) -> bool` used identically in Tasks 3/4/5. `register_engine(engine_id, engine_equity, limits=None)` consistent Tasks 1/2/4/5. `limits_for(engine_id) -> RiskLimits` consistent Tasks 2/4/5.

**Risk note:** Tasks 1 & 9 modify `tpcore` consumed by all engines — Task 1 Step 5 explicitly enforces the CLAUDE.md "check all consumers" mandate; Task 9 routes the data mutation through the canonical allocator + audit log (no one-off DELETE).
