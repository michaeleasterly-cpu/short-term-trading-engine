# Audit — remove LLM triage from the deployed `lane-service` daemon

**Date:** 2026-05-22
**Trigger:** Operator directive 2026-05-21: *"we wont be deploying the llm data triage it will run locally with my max account"*
**Driver memory:** `llm-triage-runs-local-on-max` + `deterministic-cascade-architecture` + `self-heal-autonomous-no-operator-task`
**Predecessor PR:** #236 (PR that introduced the consolidated `lane-service` with 4 co-tasks).

## TL;DR

The deployed `lane-service` daemon (running on Railway) currently hosts FOUR co-tasks under one `asyncio.gather()`:

1. `data_repair` — deterministic — STAYS in the deployed daemon.
2. `triage_data` — invokes Anthropic at runtime (via `ops.llm_data_recovery.run_autonomous_recovery`) — REMOVED from deployed daemon.
3. `triage_engine` — invokes Anthropic at runtime (via `ops.engine_llm_triage.run_triage`) — REMOVED from deployed daemon.
4. `triage_lab_emitter` — no-op in v1, but transitively imports `anthropic` SDK at module load via `ops.llm_lab_emitter` — REMOVED from deployed daemon.

After this PR the deployed daemon hosts ONE co-task: `data_repair`. The LLM-invoking modules stay in the repo (importable libraries) but are NOT loaded by the deployed daemon — they are invoked OPERATOR-LOCALLY only.

## Anthropic-SDK call sites surveyed

| Module | Imports `anthropic`? | Called from deployed daemon? | Disposition |
|---|---|---|---|
| `ops/llm_data_recovery.py` | YES (line 65) | YES — via `ops.llm_triage_service._main_loop` (data lane) | REMOVED from deployed import graph |
| `ops/engine_llm_triage.py` | YES (line 65, line 185) | YES — via `ops.llm_triage_service._engine_loop` | REMOVED from deployed import graph |
| `ops/llm_data_triage.py` | YES (line 29) | NO (legacy; called only via `_shipped()` lazy seam) | KEPT in repo; not loaded by deployed daemon |
| `ops/llm_lab_emitter.py` | YES (line 63) | YES — via `ops.llm_triage_service._lab_emitter_loop` (no-op v1 but module loads at daemon start) | REMOVED from deployed import graph |
| `ops/llm_edge_finder_sdk.py` | YES (line 26) | NO directly; called from `ops.llm_edge_finder.run_edge_finder_cotask` | KEPT in repo; not loaded by deployed daemon |
| `ops/llm_edge_finder.py` | NO (defers `make_sdk_llm_callable` to call-time) | YES — via `ops.llm_triage_service._edge_finder_loop` (event-types empty in v1) | REMOVED from deployed import graph |
| `ops/llm_finder_outcome_monitor.py` | NO directly | YES — via `ops.llm_triage_service._outcome_monitor_loop` (event-types empty in v1) | REMOVED from deployed import graph (sibling of finder co-task) |
| `tpcore/llm_data_triage/` | NO | n/a — pure logic / fence module | KEPT (no change) |
| `tpcore/engine_llm_triage/` | NO | n/a — pure logic / fence module | KEPT (no change) |
| `tpcore/lab/llm_finder/`, `tpcore/lab/llm_emitter/` | NO | n/a — pure logic / fence module | KEPT (no change) |

### Co-task verdicts

- **`data_repair` (deterministic)** — STAYS. No Anthropic SDK imports anywhere in `ops/data_repair_service.py` or its transitive deps. This is the deterministic-cascade self-heal — keeps doing exactly what it does.
- **`triage_data` (data lane)** — REMOVED. The factory binds to `ops.llm_triage_service._main_loop`, which calls `run_autonomous_recovery` — the LLM invocation. The whole co-task leaves the deployed daemon.
- **`triage_engine` (engine lane)** — REMOVED. Same shape: `engine_run_triage` calls Anthropic.
- **`triage_lab_emitter` (SP-G)** — REMOVED. Even though `run_lab_emitter_cotask` is a `noop_v1`, the module `ops.llm_lab_emitter` imports `from anthropic import AsyncAnthropic` at module top-level — so simply importing the daemon's co-task factory pulls Anthropic SDK into the deployed process.

## Migration path

### Deployed daemon (`ops/lane_service.py`)

- Imports `ops.data_repair_service` ONLY.
- Hosts ONE co-task: `data_repair`.
- `LANE_NAMES = ("data_repair",)`.
- `POOL_MAX_SIZE` reduced from 6 → 3 (one acquire per poll tick + headroom for in-flight repair).
- The deterministic self-heal cascade (`scripts/ops.py::_auto_cascade_*`) STILL EMITS the existing escalation events (`DATA_REPAIR_ESCALATED` / `DATA_SOURCE_ESCALATED` / `INGESTION_AUTO_RECOVERY_FAILED` / `ENGINE_ESCALATED`) — no event-emission removal. The operator-local LLM-side picks them up.

### Operator-local invocation

The deployed daemon NO LONGER invokes the LLM. The operator runs the LLM-side **on their Max account** via the slash skill **`/triage-data-failures`** (new in this PR):

- Polls `platform.application_log` for unresolved `AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES` escalations.
- Fires `ops.llm_data_recovery.run_autonomous_recovery` per outstanding event.
- One-shot; operator-controlled cadence.

Engine triage (`/triage-engine-failures`) and lab emit (`/lab-spec-emit` — already exists) follow the same pattern: operator-driven, one-shot per invocation.

### What does NOT change

- `ops/llm_data_recovery.py` — STAYS (operator-local invocation surface).
- `ops/llm_lab_emitter.py` — STAYS (was already an operator-on-demand path via `/lab-spec-emit`).
- `ops/engine_llm_triage.py` — STAYS (operator-local invocation surface).
- `ops/llm_triage_service.py` — STAYS as an importable library (re-usable from the operator-local skill); the `_main_loop` / `_engine_loop` / `_lab_emitter_loop` callables remain available. They are simply NOT bound by `lane_service` anymore.
- Personas under `docs/llm_triage_personas/` — STAYS (consumed by the operator-local LLM).
- The deterministic cascade (`scripts/ops.py::_auto_cascade_*`) — STAYS, unchanged.
- The two-daemon Railway budget — STAYS: `engine-service` + `lane-service` + `data-operations` cron is the closed whitelist. The LLM is NOT a deployed daemon.

## Validation contract

- `python -c "import ops.lane_service"` MUST NOT trigger an `anthropic` import. Enforced by a new sentinel test (`tests/test_lane_service_no_anthropic.py`).
- The deployed daemon STARTS cleanly with `anthropic` absent from `sys.modules` (verified by uninstall-and-import test).
- All previously-passing tests still pass; the 4-lane assertion in `tests/test_lane_service.py` is updated to 1-lane.
