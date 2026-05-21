# Per-engine broker-floor attribution (RiskGovernor #251 A1 follow-up)

**Status**: Build  
**Lane**: platform-overlay (`tpcore/risk/`) — heavy lane  
**Tracks**: TODO.md L585 "Remaining deferred: per-engine broker attribution (needs `client_order_id` engine tagging; cross-engine over-count is strictly tighter/safe meanwhile). [lane: platform-overlay (RiskGovernor)] [gate: none] [needs operator decision: no] [effort: S]"  
**Builds on**: #251 A1 (`reconcile_open_floor` opt-in, `max(proxy, broker_floor)` raise — `tpcore/risk/governor.py` L388-429).

## 1. Problem

The #251 A1 broker-floor raise computes `broker_floor = len(broker_positions)` — a CROSS-engine count of every open position on the Alpaca account. That over-counts (strictly tighter, never fail-open — safe), but it wastes per-engine slots: a momentum-owned position counts against catalyst's `max_open_positions=8` and vice versa. With seven live/lab engines all sharing one paper account, the cross-engine count regularly inflates a per-trade engine's effective floor past its own cap when other engines hold legitimate positions.

## 2. Goal

Filter `broker_positions` to those attributable to the engine being gated, by correlating each position's `symbol` against the `client_order_id` prefix of recent broker orders for that symbol. Mirror the canonical precedent at `momentum/scheduler.py` L97-145 (`_filter_to_engine_holdings`).

After the change:

- `broker_floor` = COUNT of `broker_positions` whose `symbol` is owned by `engine_id`.
- A position whose symbol has NO recent engine-tagged order on file ("unattributed") COUNTS against the engine being gated (over-count → tighter → never-fail-open) AND emits `tpcore.risk.unattributed_broker_position` WARNING so the operator can clean up.
- A broker without `list_recent_orders` (non-Alpaca / smoke fixtures) → fall back to the pre-change CROSS-engine count (current behavior) and emit one `tpcore.risk.broker_attribution_unavailable` WARNING.

## 3. Required reading (cited)

1. `tpcore/risk/governor.py` L388-429 — broker-floor block; the change site.
2. `tpcore/risk/limits_profile.py` L13-21 — `_PROFILE` flip ON for momentum/sentinel.
3. `tpcore/order_ids.py` L53-62 (`ENGINE_PREFIX`), L214-232 (`is_engine_cid`) — single source of truth for engine ↔ prefix.
4. `momentum/scheduler.py` L97-145 — canonical filter precedent; mirrored into the governor.
5. `tpcore/interfaces/broker.py` L86-94 (`Position` has `symbol`, NO `client_order_id`) — drives the symbol→orders join.
6. `tpcore/alpaca/broker_adapter.py` L412-434 — `list_recent_orders` is an Alpaca-adapter extension, NOT on `BrokerExecutionInterface`; duck-typed via `getattr`.
7. `tpcore/risk/tests/test_limits_profile.py`, `tpcore/tests/test_risk_governor_broker_floor.py` — existing test surfaces; mirrored test style.

## 4. Engine-CID-prefix coverage (audit)

Every engine in `tpcore.engine_profile._PROFILE` (excluding `allocator`/`lab`/`sigma`-RETIRED) stamps its prefix via `tpcore.order_ids.build_cid()`:

| Engine    | Prefix | Stamp site                                                              |
| --------- | ------ | ----------------------------------------------------------------------- |
| momentum  | `mo_`  | `momentum/plugs/execution_risk.py` L255 (`build_cid("momentum", ...)`)  |
| reversion | `rv_`  | `reversion/plugs/execution_risk.py` L141, L150 (`build_cid("reversion"`)|
| vector    | `vc_`  | `vector/plugs/execution_risk.py` L162 (`build_cid("vector", ticker)`)   |
| sentinel  | `sn_`  | `sentinel/scheduler.py` L334 (`build_cid("sentinel", order.ticker)`)    |
| canary    | `ca_`  | `canary/scheduler.py` L122 (`build_cid("canary", CANARY_TICKER)`)       |
| catalyst  | `ct_`  | `catalyst/plugs/execution_risk.py` L97 (`build_cid("catalyst", ...)`)   |
| carver    | `cv_`  | `carver/plugs/execution_risk.py` L106 (`build_cid(engine="carver"...)`) |

A new test `test_every_dispatchable_engine_has_a_cid_prefix` asserts the SoT: every non-retired/non-sentinel engine name in `_PROFILE` is a key in `ENGINE_PREFIX`. CI red on drift. No stamping is being added — coverage is already 100%.

## 5. Design (autonomous)

### 5.1 New helper `_count_engine_broker_floor`

```python
async def _count_engine_broker_floor(
    self, engine_id: str, broker_positions: list[Position],
) -> int:
    """Count broker_positions attributable to engine_id by symbol×recent-order CID.

    Returns the cross-engine total (degraded path) + emits
    tpcore.risk.broker_attribution_unavailable WARNING when the broker
    has no list_recent_orders primitive (non-Alpaca / smoke fixtures).

    Unattributed symbols (positions whose symbol has no recent
    engine-tagged order) count against engine_id (over-count → tighter →
    never-fail-open) and emit tpcore.risk.unattributed_broker_position.
    """
```

### 5.2 Wire into `check_trade`

```python
# After the existing broker_positions fetch (governor.py L426):
broker_floor = await self._count_engine_broker_floor(engine_id, broker_positions)
```

The CROSS-engine `len(broker_positions)` is REMOVED — replaced one-line by the per-engine count. `broker_errored` short-circuit unchanged (broker error → `broker_positions=[]` → `broker_floor=0` → proxy stands; BUY still fails-closed on error per existing A1 path).

### 5.3 Order-fetch contract

Use `getattr(self._broker, "list_recent_orders", None)` (duck-typed, mirrors `tpcore/order_management/stale_order_cancel.py` L39-41 and the canary/momentum schedulers). When absent → degraded WARNING + return `len(broker_positions)` (pre-change behavior). When present → fetch `limit=500` (matches momentum/canary's caller pattern), then build `engine_symbols = {o.symbol for o in recent if is_engine_cid(o.client_order_id, engine_id)}`.

### 5.4 Safety properties (non-negotiable)

| Property                                             | Mechanism                                                          |
| ---------------------------------------------------- | ------------------------------------------------------------------ |
| `effective_open ≥ proxy` (never-fail-open invariant) | `max(state.open_positions, broker_floor)` unchanged.               |
| Unattributed positions count against current engine  | Symbol-not-in-`engine_symbols` += 1 (over-count, fail-safe).       |
| Broker without `list_recent_orders` → degraded safe  | Fall back to `len(broker_positions)` (current cross-engine count). |
| Buggy filter returning 0 on real position            | Unattributed branch + WARNING fires; proxy still wins via `max()`. |
| Broker error → no `list_recent_orders` call          | `broker_positions=[]` short-circuits before the helper is invoked. |

### 5.5 What does NOT change

- `RiskLimits` schema (no new field).
- `_PROFILE` (already opt-in for momentum/sentinel; this change makes the existing flag more accurate, not more enabled).
- The pre-fetch + reuse of `broker_positions` for the BUY net-long check (still AT MOST one `get_positions()` per `check_trade`).
- The BUY net-long fail-closed-on-broker-error path.
- `record_fill` / `record_close` paths.
- Any engine scheduler or order_manager.

## 6. Test plan

New file `tpcore/tests/test_risk_governor_per_engine_attribution.py`:

| Test                                                          | Asserts                                                                                                                          |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `test_filter_attributes_by_engine_cid_prefix`                 | 3 broker positions (mo_X, rv_Y, ct_Z) + gating "momentum" → broker_floor=1, the rv_/ct_ positions are NOT counted against momentum. |
| `test_unattributed_position_counts_against_engine_and_warns`  | Position whose symbol has no recent engine-tagged order → broker_floor=1 + `tpcore.risk.unattributed_broker_position` log entry. |
| `test_broker_without_list_recent_orders_degrades_to_xengine`  | Broker mock lacks `list_recent_orders` → broker_floor=N (cross-engine total) + `tpcore.risk.broker_attribution_unavailable` WARNING. |
| `test_buggy_filter_zero_floor_proxy_still_wins`               | Filter returns 0 but proxy is at cap → BLOCK still fires (proxy floor preserved via `max()`). |
| `test_never_fail_open_invariant_across_attribution`           | Property: for engine_id × broker_positions × recent_orders permutations, `effective_open ≥ proxy` always; flag-OFF byte-identical. |
| `test_legacy_tier_suffix_cid_unattributable_warns`            | A `_tier1`/`_tier2` legacy cid (engine-unknowable per `parse_cid`) → that position is unattributed → WARNING fired. |
| `test_every_dispatchable_engine_has_a_cid_prefix`             | All non-retired/non-LAB-sentinel engines in `_PROFILE` are in `ENGINE_PREFIX` (drift sentinel).                                  |

Existing `test_risk_governor_broker_floor.py` cases are KEPT — the helper degrades cleanly when no `list_recent_orders` is set on the AsyncMock (those tests' brokers don't set it, so they hit the cross-engine fallback path, which is byte-identical to today). One adjustment: tests that asserted "broker shows 5 → block at 5" need a `recent_orders` mock setup matching all five symbols to "momentum", OR they continue to hit the fallback path. We extend `_broker()` to optionally set `list_recent_orders` so per-engine tests can opt in.

## 7. Gates (heavy lane)

```bash
.venv/bin/python -m pytest -p no:xdist -p no:cacheprovider -q
.venv/bin/python -m pytest -p no:randomly -p no:xdist -p no:cacheprovider -q
ruff check . --statistics
.venv/bin/python -m tpcore.scripts.check_imports tpcore ops reversion vector momentum sentinel canary catalyst carver
```

All four must be green. `gh pr checks <#>` gates on `statusCheckRollup == SUCCESS`.

## 8. References

- Alpaca Trading API `client_order_id` semantics: <https://docs.alpaca.markets/reference/getorder>
- Operator memory: `feedback_use_official_docs.md`, `feedback_no_lazy_vendor_blame.md`, `feedback_symmetry_not_copy.md` (mirroring the momentum precedent, not the data subsystem).
- Predecessor spec: `2026-05-18-batch-engine-slot-accounting-design.md` (#251 A1).
