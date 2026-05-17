# Event-Driven Engine Services — Design (Epic + Sub-project A)

**Status:** approved 2026-05-17 (operator). This document specs the
epic decomposition and **Sub-project A** in full. B/C/D are scoped here
but each gets its own spec → plan → implementation cycle.

## 1. Problem & operator directive

Operator directive (captured in memory `project_three_service_architecture`):
*"We don't live on a timeline. We do things as soon as the setup is
ready."* Every engine/service should fire the moment its preconditions
are satisfied (data ready + market closed + setup ready), never on a
clock. **Time is a gate/precondition, never a trigger.** Per-engine
cadence is itself a precondition (engines have distinct cadences;
daily ≠ monthly).

Current state:
- `ops/engine_service.py` is **already** event-driven — polls
  `platform.application_log` for `DATA_OPERATIONS_COMPLETE` (60s,
  idempotent `recorded_at` cursor) → `scripts/run_all_engines.sh`.
- The **allocator** is the time-driven outlier (launchd Mon 13:00 UTC).
- `data_operations` is the time-gated **producer** of the readiness
  event (weekday 21:30 UTC; "market closed" is its natural gate).
- **Cadence is hardcoded per scheduler** — momentum computes "first
  trading day of month" in `momentum/scheduler.py`; sentinel uses
  `is_trading_day`; reversion/vector run every sweep. Divergent,
  uncentralised.
- A **per-engine data gate already exists**:
  `tpcore/quality/validation/capital_gate.py`
  `assert_passed_for_engine(pool, engine)` (engine→table map derived
  from the selfheal-registry SoT). Data-readiness per engine is solved.

## 2. Epic decomposition (A → B → C → D)

| | Sub-project | Depends on |
|---|---|---|
| **A** | `engine_profile` foundation — declarative per-engine SoT (cadence + preconditions) that **composes** `capital_gate`; one `should_fire()`. Pure library, landed dark. | — |
| **B** | Event-driven dispatch — `engine_service` consults `should_fire()` per engine; delete per-scheduler hardcoded cadence. | A |
| **C** | Allocator event-driven — fires on the readiness event with the `WEEKLY_FIRST_TRADING_DAY` profile + idempotent guard. | A, B |
| **D** | Two-daemon consolidation — fold allocator + AAR + forensics into the engine daemon; retire separate launchd jobs. Net: data daemon + engine daemon. | A, B, C |

Each sub-project produces working, independently-testable software.
This document fully specs **A only**; B/C/D are summarised for
context and will be brainstormed/specced when reached.

## 3. Sub-project A — `engine_profile` foundation

### 3.1 Architecture & boundary
One new module `tpcore/engine_profile.py`. **Pure library, landed dark
— no scheduler/daemon/trade-path file is modified in A** (B does the
wiring). It is the single canonical SoT for *when an engine may fire*.
Data-readiness stays 100% in `capital_gate` (called, never
re-implemented — not a parallel mechanism). Mirrors the proven flat-SoT
pattern of `tpcore.feeds.freshness_max_age_days` and
`tpcore.risk.limits_profile.limits_for`.

### 3.2 Data model
```python
class Cadence(StrEnum):
    DAILY = "daily"
    MONTHLY_FIRST_TRADING_DAY = "monthly_first_trading_day"
    WEEKLY_FIRST_TRADING_DAY = "weekly_first_trading_day"

class EngineProfile(BaseModel):           # pydantic v2, frozen
    engine: str
    cadence: Cadence
    market_closed_required: bool = True   # defense-in-depth

_PROFILE: dict[str, EngineProfile] = {
    "reversion": EngineProfile(engine="reversion", cadence=Cadence.DAILY),
    "vector":    EngineProfile(engine="vector",    cadence=Cadence.DAILY),
    "sentinel":  EngineProfile(engine="sentinel",  cadence=Cadence.DAILY),
    "momentum":  EngineProfile(engine="momentum",  cadence=Cadence.MONTHLY_FIRST_TRADING_DAY),
    "allocator": EngineProfile(engine="allocator", cadence=Cadence.WEEKLY_FIRST_TRADING_DAY),  # consumed in C
}

@dataclass(frozen=True)
class FireDecision:
    fire: bool
    reason: str
    checks: dict[str, bool]   # per-precondition, audit/observability

def profile_for(engine: str) -> EngineProfile | None
async def should_fire(engine: str, now: datetime, pool) -> FireDecision
```
`sentinel` is `DAILY`; its Bear-Score/regime logic stays **internal to
the engine** — the profile models when to *invoke* it, never its
strategy. `allocator` profile data is present now (the profile is the
SoT) but is not consumed until Sub-project C.

### 3.3 `should_fire()` composition (ordered, short-circuit)
1. `profile_for(engine)` — unknown → `FireDecision(False,
   "unprofiled engine", …)` (fail-closed).
2. **Cadence boundary** via `tpcore.calendar` (XNYS):
   `DAILY`→`is_trading_day(now)`; `MONTHLY_FIRST_TRADING_DAY`→`now` is
   the first trading day of its calendar month;
   `WEEKLY_FIRST_TRADING_DAY`→first trading day of its week. Not a
   boundary → no-fire.
3. **Market-closed** (if `market_closed_required`) via
   `tpcore.calendar`. Market open at `now` → no-fire.
4. **Data-ready**: `await capital_gate.assert_passed_for_engine(pool,
   engine)` — the existing authority. Fails → no-fire with its detail.
5. **Not-already-run**: query `platform.application_log` for this
   engine's existing run record for the current cycle (the canonical
   per-run event schedulers already emit via
   `tpcore/logging/db_handler.py` — exact literal pinned at plan time
   from that module, e.g. the `STARTUP`/`SHUTDOWN` run pair) with
   `recorded_at >= cadence_window_start`. Idempotency keys off a run
   having **occurred** this cycle (started), **not** strictly success:
   a failed run does NOT auto-refire within the same cadence window —
   failures escalate via the existing alarms, they do not silently
   loop. (An explicit force/retry path, if ever needed, is a B
   concern, not automatic re-fire.) Found → no-fire ("already ran
   this cycle"). Window: `DAILY`=current trading session;
   `MONTHLY_*`=current month's first-trading-day → now;
   `WEEKLY_*`=current week (first-trading-day → now).
6. All pass → `FireDecision(True, "ready", …)`.

### 3.4 Error handling — fail-CLOSED
A fire-gate must never fire on doubt. Any exception in any check, any
None/ambiguous state, or an unknown engine → `fire=False`,
`reason="error: …"`. Not-firing is always safe (the engine simply
doesn't trade this cycle); wrongly firing submits real orders. Every
check is individually wrapped; the `checks` dict records which
preconditions passed for observability.

### 3.5 Testing
Pure unit tests, no DB/network: fake `pool`, injected `now`,
monkeypatched `tpcore.calendar` and
`capital_gate.assert_passed_for_engine`. Matrix: per cadence ×
{boundary y/n} × {market closed/open} × {data ready/not} ×
{already-ran/not}; plus unknown-engine→fail-closed;
exception-in-each-check→fail-closed; and a `_PROFILE` ≡ live-engine-
roster coverage guard (a new engine without a profile **fails the
build**, mirroring the selfheal registry-coverage test).

### 3.6 Scope boundary — what A explicitly does NOT do
No scheduler edits; momentum's hardcoded "first trading day" logic is
**not** removed in A (→ B); no `engine_service`/daemon wiring (→ B); no
allocator behavior change (→ C); no daemon consolidation (→ D).
**Acceptance: zero files changed outside `tpcore/engine_profile.py` +
its test module; the full pre-existing test suite is unchanged and
green; `ruff`/`check_imports` clean.**

## 4. Decisions log (Sub-project A)

- **D-A1 — Idempotency source:** reuse existing `platform.application_log`
  per-engine run records (no new table/infra). The same pattern
  `engine_service` already uses. Keys off a run having **occurred**
  this cycle (not strictly success) — a failed run escalates via
  existing alarms and does not auto-refire within the cadence window.
- **D-A2 — Module boundary:** *compose* — `engine_profile` calls
  `capital_gate.assert_passed_for_engine` for data-readiness;
  `capital_gate` stays the single data-gate authority (not forked).
- **D-A3 — Cadence taxonomy:** 3 cadences (DAILY,
  MONTHLY_FIRST_TRADING_DAY, WEEKLY_FIRST_TRADING_DAY); sentinel =
  DAILY (regime stays engine-internal). All boundary math via
  `tpcore.calendar` (XNYS).
- **D-A4 — Structure:** declarative `_PROFILE` dict + pure
  `should_fire()` (Approach 1), mirroring existing SoT-profile pattern;
  landed dark.

## 5. Out of scope (future sub-projects, summarised)

- **B:** `engine_service` calls `should_fire()` per engine before
  dispatching it; per-scheduler cadence hardcoding deleted; behavior
  becomes profile-driven. Live-dispatch change → its own spec/plan,
  isolated, reviewed.
- **C:** allocator dispatched event-driven via its
  `WEEKLY_FIRST_TRADING_DAY` profile + idempotency; retire the Mon
  launchd job.
- **D:** consolidate to two daemons (data daemon + engine daemon);
  AAR/forensics/allocator run inside the engine daemon.
