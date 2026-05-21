# Sub-project B — Event-Driven Engine Dispatch — Design

**Status:** approved 2026-05-17 (operator). Sub-project B of the
event-driven engine-services epic (see
`2026-05-17-event-driven-engine-services-design.md` for A→B→C→D).
Sub-project A (`tpcore/engine_profile.py`, `should_fire`) is merged
(PR #4) and landed dark; B is what wires it in.

## 1. Problem

Today `ops/engine_service.py` (event daemon, polls `application_log`
for `DATA_OPERATIONS_COMPLETE`) subprocesses `scripts/run_all_engines.sh`,
a bash `for engine in reversion vector momentum sentinel` loop that
runs every scheduler unconditionally. Cadence is hardcoded *inside*
each scheduler (momentum `is_rebalance_day`/"first trading day of
month"; sentinel `is_trading_day`). Nothing consults
`engine_profile.should_fire()`. An engine that is data-blocked either
runs on bad data or fails opaquely; there is no self-correcting path.

## 2. Architecture

New `ops/engine_dispatch.py` (Python). `scripts/run_all_engines.sh`
loses its bash `for`-loop and becomes a thin caller
(`python -m ops.engine_dispatch`). `ops/engine_service.py` is
**unchanged for the trigger path** (still subprocesses
`run_all_engines.sh` on `DATA_OPERATIONS_COMPLETE` — crash isolation +
minimal blast radius) — with ONE addition: it also treats
`DATA_REPAIR_COMPLETE` as a re-dispatch trigger (§4).
Deployment-agnostic & idempotent-on-restart (Railway-portable: env +
`build_asyncpg_pool`, no launchd/macOS coupling).

New engine-lane helper: `tpcore/quality/validation/capital_gate.py`
gains `async failing_sources_for_engine(pool, engine) -> list[str]` —
a NON-raising companion to `assert_passed_for_engine` returning the
engine's failing data sources **in `HealSpec.source` vocabulary** (the
`tpcore/selfheal/registry.py` `source` namespace `ENGINE_TABLES` already
derives from). This is the vocabulary locked with the data lane.

## 3. Dispatch flow (per readiness cycle)

For each engine in the roster (`reversion, vector, momentum,
sentinel`):

1. `decision = await should_fire(engine, now, pool)`.
2. `decision.fire` → invoke that engine's scheduler as a subprocess
   (`python -m {engine}.scheduler`; per-engine crash isolation; a
   non-zero exit logs + continues to the next engine). Log
   `ENGINE_DISPATCHED`.
3. `not decision.fire` and `decision.checks["data_ready"] is False` →
   compute `sources = await failing_sources_for_engine(pool, engine)`,
   **emit one `ENGINE_DATA_REQUEST`**, **skip this engine, do NOT block
   the sweep, do NOT call `run_self_heal()` in-process**. (No
   trade-latency coupling, no Supabase-pooler contention with the data
   lane — this resolves the design's weakest-part critique.) Dedup:
   at most one open request per `(engine, cadence-window)` — do not
   re-emit on subsequent 60s cycles while a request is open with no
   terminal event.
4. `not decision.fire` for any other reason (off-cadence /
   market-open / already-ran) → skip with a logged reason (normal,
   not an error).

The dispatcher **never repairs data**. It requests; the data lane
heals and replies.

## 4. Re-evaluation trigger

`ops/engine_service.py` adds `DATA_REPAIR_COMPLETE` as a second
trigger event type in its existing poll loop (idempotent
`recorded_at`-column cursor, same pattern as `DATA_OPERATIONS_COMPLETE`).
On a `DATA_REPAIR_COMPLETE` with `green=true`, it re-invokes the
**full sweep** (`run_all_engines.sh` → `engine_dispatch`). Re-running
the whole sweep (not just the requested engine) is safe and simpler
because `should_fire` is idempotent — engines that already fired this
cadence cycle are skipped by the STARTUP check; only the now-unblocked
engine proceeds. It fires the same evening. Rationale: waiting for the next *normal* readiness cycle
could make a **monthly** engine miss its rebalance window entirely.
This is the only change to the otherwise-untouched daemon.

## 5. Inter-lane event contract (LOCKED with the data session)

Bus: `platform.application_log` (columns: `event_type`, `engine`,
`data` jsonb, `recorded_at` DB-set, `run_id`). **No client timestamp
in payloads** — cursor/dedup key off the DB `recorded_at` column only
(Manila/UTC-skew discipline). `request_id` = engine-generated `uuid4`
string, the sole correlation key. **Liveness guarantee:** every
`ENGINE_DATA_REQUEST` receives exactly one terminal event
(`DATA_REPAIR_COMPLETE` xor `DATA_REPAIR_ESCALATED`) with the same
`request_id`. `sources` are always `HealSpec.source` names.

```
ENGINE_DATA_REQUEST    data={schema:1, request_id, engine,
                             sources:[HealSpec.source...], reason}
DATA_REPAIR_COMPLETE   data={schema:1, request_id,
                             sources_healed:[...], sources_still_red:[...], green:bool}
DATA_REPAIR_ESCALATED  data={schema:1, request_id,
                             sources_unhealed:[...], reason, attempts:int}
```

`green = (requested sources ⊆ healed)`. The data lane owns
`ops/data_repair_service.py` (the consumer), the Step-4 lock reuse,
the validate-first fast-path, and the `source → HealSpec` mapping —
**all explicitly OUT of B's scope**.

## 6. Idempotency, timeout, Railway

- `should_fire`'s `application_log` STARTUP check makes re-dispatch and
  Railway restarts safe (no double-fire). `request_id`/`(engine,
  cadence-window)` dedup prevents request spam.
- **Bounded no-terminal-event timeout (engine-side, B owns it):** if a
  request has no terminal event within a bounded wall-clock window
  (default **90 minutes**, env-overridable — comfortably covers a
  targeted heal yet still leaves post-close trading time; exact
  constant + env name pinned in the plan), the engine is treated as
  escalated for that cadence cycle — skipped + operator alarm. The
  engine never hangs on a silent data lane. The timeout is evaluated
  on subsequent dispatch cycles by comparing `now` to the request's
  DB `recorded_at` (no in-process blocking wait).
- **Partial heal:** `green=false` is treated exactly like
  `DATA_REPAIR_ESCALATED` for the cycle (no fire); B does NOT
  re-request in the same cycle (the terminal event closes the
  `request_id`; the next cadence cycle opens a fresh request).
- **Inherited sharp edge fixed here:** `should_fire` idempotency keys
  off run-*started* (`STARTUP`). A scheduler crashing post-STARTUP /
  pre-trade would silently skip the engine for the whole cadence
  cycle (up to a month for momentum). B adds a guard: if `STARTUP`
  exists for the cycle but no completion (`SCAN_COMPLETE`/`SHUTDOWN
  exit_code=0`) and the started run is older than a bounded threshold
  (default **2 hours**, env-overridable; exact constant + env name
  pinned in the plan), treat as not-run (re-fire allowed). This guard
  lives in the dispatcher (B), reading the same `application_log`.

## 7. Scheduler cadence removal

Delete momentum's `is_rebalance_day` / "first trading day of month"
computation and sentinel's `is_trading_day` gating — `engine_profile`
is the sole cadence authority via the dispatcher. Keep **only** each
scheduler's existing explicit operator `--force`/override flag (the
manual escape hatch). A bare manual `python -m X.scheduler` runs (the
operator's responsibility); the dispatcher is the gate. No engine
*strategy* logic is modified — only the cadence-gating plumbing is
removed.

## 8. Testing

Pure unit tests, no real DB/subprocess (mock `should_fire`,
`failing_sources_for_engine`, the scheduler-invoke boundary, a fake
pool). Matrix:
- fire=True → scheduler invoked exactly once; `ENGINE_DISPATCHED`
  logged.
- data_ready=False → exactly one `ENGINE_DATA_REQUEST` with `schema:1`,
  a fresh `uuid4` `request_id`, `sources` from
  `failing_sources_for_engine`; scheduler NOT invoked; sweep NOT
  blocked; `run_self_heal` NEVER called.
- dedup: two cycles, same data-blocked engine, open request → second
  cycle emits no new request.
- off-cadence / market-open / already-ran → skip, no request, no
  invoke.
- crashed-STARTUP guard → re-fire allowed when STARTUP-without-completion
  is stale.
- `DATA_REPAIR_COMPLETE green=true` re-dispatch path; `green=false`
  and `DATA_REPAIR_ESCALATED` and timeout → engine stays skipped +
  alarm.
- `failing_sources_for_engine` returns `HealSpec.source` names for an
  engine with a red source; `[]` when all green.

## 9. Scope boundary

B delivers: `ops/engine_dispatch.py`; thin `scripts/run_all_engines.sh`;
the `engine_service.py` `DATA_REPAIR_COMPLETE` re-trigger;
`capital_gate.failing_sources_for_engine`; momentum/sentinel cadence
deletion (strategy untouched); the crashed-STARTUP guard; tests. B does
**NOT** touch `tpcore/selfheal`, `tpcore/feeds`, ingestion, or build
`ops/data_repair_service.py` (data lane). Acceptance: engine sweep is
profile-gated; a data-blocked engine emits a request and never blocks
the sweep; `run_self_heal` is never called from the engine process;
full pre-existing suite green; ruff/check_imports clean.

## 10. Decisions log

- **D-B1** Python dispatcher (`ops/engine_dispatch.py`); bash is a thin
  caller; `engine_service` trigger path unchanged.
- **D-B2** (supersedes the earlier "in-path self-heal" answer):
  data-not-ready ⇒ **emit `ENGINE_DATA_REQUEST`, skip, never heal
  in-process**. Async hand-off to the data lane. Resolves the
  trade-latency/pooler-contention weakness.
- **D-B3** Delete per-scheduler bespoke cadence math; keep only the
  explicit `--force` operator escape hatch; `engine_profile` is the
  sole cadence authority.
- **D-B4** Event contract LOCKED with the data lane (§5): `HealSpec.source`
  vocabulary; one-terminal-event liveness; `request_id` uuid4
  engine-generated; no client timestamps; `schema:1`.
- **D-B5** `engine_service` re-dispatches on `DATA_REPAIR_COMPLETE`
  (timeliness for monthly engines).
