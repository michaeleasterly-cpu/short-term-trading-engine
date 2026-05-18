# DA-3 — Two-Daemon Consolidation — Design Spec

**Status:** approved design (operator chose **Option A-full**, 2026-05-18). Lane: **ENGINE**. Final sub-project of the Deterministic Agents epic.

## 1. Principle & honest scope

The epic's original framing — *"fold AAR/forensics/weekly one-shots + DA-1/DA-2 into the engine daemon → exactly two daemons (data + engine)"* — predates Sub-projects A–C and the data lane's own supervisor/ladder. Against `main` @ `234fb0d` reality:

- **Already done (DA-3 = verify + document only, NO code):** `allocator`, DA-1 (`engine_supervisor`), DA-2 (`aar_autotune`) are already in-process in `ops/engine_dispatch.py:241-246` / `_dispatch_allocator:229-238`. `forensics` is already the final stage of `ops.py --update` (data-lane).
- **Out-of-lane / descoped (cross-lane CL-1):** `data_repair_service` (hosts `tpcore.selfheal` = the DATA healer, shares `${TMPDIR:-/tmp}/ste-data-operations.lock` with `run_data_operations.sh`, exactly-once terminal contract) and `data_operations` cron remain **separate data-lane processes**, owned by the data session. Folding them would put the data healer in the engine process — an architecture-level lane violation, not just a forbidden file edit.
- **Genuine remaining engine-lane work (this spec):** fold `trade_monitor` into the single long-lived engine daemon, and relocate the `weekly_digest` launchd cron trigger into that daemon as a crash-isolated subprocess (exact Sub-project-C allocator-cron precedent), then formally specify + audit-enforce the per-lane two-daemon invariant.

**"Exactly two daemons" is defined precisely** (D-D3-1): two long-lived `KeepAlive` daemons — **one per lane** — plus the data-lane data-operations cron (a `StartCalendarInterval` one-shot, not a daemon):

- **Engine daemon** (engine lane, this session): sweep loop + trade-monitor stream loop + weekly-digest cadence trigger.
- **Data-repair daemon** (data lane, the data session's responsibility): unchanged, untouched.
- **Data-operations cron** (data lane): unchanged, untouched, not a daemon.

A zero-data-daemon single-process target is architecturally impossible without hosting the data healer in the engine process. The spec states this so the target is not mis-measured.

## 2. Lane discipline (hard constraints)

This session is the ENGINE lane. It MUST NOT modify any data-lane file: `ops/weekly_digest.py`, `scripts/run_weekly_digest.sh`, `ops/data_repair_service.py`, `scripts/run_data_repair_service.sh`, `scripts/run_data_operations.sh`, `scripts/install_launchd_data_operations.sh`, `scripts/install_launchd_data_repair_service.sh`, `tpcore/selfheal/`, `tpcore/ladder/`, `tpcore/feeds/`, `tpcore/ingestion/`, `tpcore/datasupervisor/`, `tpcore/auditheal*`, `ops/cutover_agent.py`. Never local-merge into the shared `main` checkout; never stomp the data session. The **subprocess seam** (invoking an existing data-lane CLI entrypoint, e.g. `python -m ops.weekly_digest emit`, as a child process) is **NOT** a data-lane file edit — established precedent: Sub-project C retired the allocator launchd cron by having `engine_dispatch._invoke_allocator` (`ops/engine_dispatch.py:130-157`) run `scripts/ops.py --allocate` as a subprocess. DA-3 reuses exactly that pattern.

## 3. Target architecture

`ops/engine_service.py` becomes **the single engine daemon**: one process, one asyncio event loop, one shared `asyncpg` pool (built once in `_amain` via `tpcore.db.build_asyncpg_pool`), one signal/shutdown path, hosting these concurrent units under a structured supervisor:

1. **`sweep` task** — the existing `_main_loop` (`engine_service.py:89-116`): poll `platform.application_log` for `DATA_OPERATIONS_COMPLETE` / green `DATA_REPAIR_COMPLETE` (cursor-based, ≥-strictly-newer, 60s `POLL_INTERVAL_SEC`), and on a trigger run `scripts/run_all_engines.sh` via `loop.run_in_executor(None, _run_engine_sweep)` (subprocess in a thread — does NOT block the event loop; preserved verbatim, asserted by test).
2. **`monitor` task** — `tpcore.trade_monitor.TradeMonitor(...).run_forever()` (`tpcore/trade_monitor.py:263`): the Alpaca trade-update stream + Tier-2 limit-sell-on-Tier-1-fill cascade + `trade_monitor_heartbeat` writer (`_write_heartbeat_once`, `:253`). Pure engine-lane file.
3. **`weekly_digest` cadence trigger** — once per UTC trading-day-agnostic cadence (idempotent per ISO week by construction, so exact cadence is non-critical; gated via `tpcore.calendar`/UTC), fire `asyncio.create_subprocess_exec(sys.executable, "-m", "ops.weekly_digest", "emit")`, crash-isolated exactly like `_invoke_allocator` (log `engine_daemon.weekly_digest_failed` on non-zero/spawn error; NEVER abort the daemon). The engine daemon does NOT read/modify `live_clearance` or `ops/weekly_digest.py` — it only relocates the *trigger*; the data ladder still owns digest consumption.

Event contracts are **frozen** (D-D3-9): no change to `application_log` event types, payloads, cursors, or the `ENGINE_DATA_REQUEST`/`DATA_REPAIR_COMPLETE`/`DATA_REPAIR_ESCALATED` handshake. `data_repair_service` is NOT folded, so the request/response path is structurally untouched. DA-3 is process-topology only.

## 4. Failure isolation / ordering / idempotency

- **Structured supervisor.** Tasks run under `asyncio.TaskGroup` (3.11) or `gather(..., return_exceptions=True)` with a per-task restart wrapper. **No single task failure may bring down the daemon** (defense-in-depth atop launchd `KeepAlive`): a crashed `sweep` task is caught, logged (`engine_daemon.sweep_task_crashed`), and restarted WITHOUT killing `monitor`, and vice-versa. `trade_monitor` already self-heals its stream via internal backoff (`trade_monitor.py:279-299`, `degraded`→reconnect) — that stays; the supervisor restart is the outer guard for an unexpected escape.
- **Fill-latency protection (make-or-break).** `trade_monitor` is Tier-2 fill-latency-sensitive; a multi-minute engine sweep must never starve it. The sweep already runs in `run_in_executor` (thread/subprocess) so it does NOT occupy the event loop — the monitor stream keeps consuming during a sweep. This property is preserved verbatim and **asserted by a dedicated test** (a slow fake sweep must not delay a monitor-loop tick).
- **Shared pool sizing.** One `asyncpg` pool sized ≥ (sweep needs) + (trade_monitor's current `max_size=4`). `trade_monitor.amain()`'s standalone pool config is consolidated into the engine daemon's single pool.
- **Locks.** The engine daemon introduces NO new shared lock and MUST NOT touch `ste-data-operations.lock` (data-lane). `trade_monitor` + sweep coexist today as separate processes with no shared lock; co-hosting adds no contention (sweep is off-loop, monitor is loop-resident). Single-instance is guaranteed by launchd label uniqueness (one plist).
- **Signal/shutdown.** One `stop_event` (existing `engine_service.py:125-133` pattern). SIGINT/SIGTERM → `stop_event.set()` → sweep loop exits on its check; `TradeMonitor.run_forever()` is wrapped so the supervisor cancels it cleanly on stop; `finally` closes the single shared pool once. `KeepAlive=true` restarts the whole process on hard crash.
- **Idempotency.** Sweep cursor semantics unchanged (≥ strictly-newer, green-only repair filter preserved). Weekly-digest emit is idempotent per ISO week (dedups on the `WEEKLY_DIGEST` row) — over-firing is safe by construction; the cadence gate is best-effort, not a correctness dependency.

## 5. Migration path

**Engine-lane-owned (editable here):**
- `scripts/install_launchd_engine_service.sh` — extended to install the consolidated daemon. Launchd **label kept** `com.michael.trading.engine-service` (D-D3-7) so existing dashboard `--check` probes / log-tail references don't break; only the internal description changes.
- `scripts/install_launchd_trade_monitor.sh` — **retired**: the installer must `launchctl unload` + `rm` the old `com.michael.trading.trade-monitor` plist (idempotent migration); the script is deleted (kept in git history as the rollback artifact, D-D3-8).
- `scripts/install_launchd_weekly_digest.sh` — **retired** the same way (`launchctl unload` + `rm` `com.michael.trading.weekly-digest`); script deleted, history-preserved. Per operator decision **Option A-full** (D-D3-5 ratified): retiring the weekly-digest *launchd plist* while leaving `ops/weekly_digest.py` and `scripts/run_weekly_digest.sh` byte-unchanged is engine-lane-legal (symmetric to C dropping the allocator from the manifest); the digest still runs, now triggered by the engine daemon.
- `scripts/install_all_daemons.sh` — manifest loop reduced from 5 to 3 entries: `install_launchd_engine_service` (consolidated), `install_launchd_data_repair_service` (data-lane, left verbatim), `install_launchd_data_operations` (data-lane, left verbatim). The `trade_monitor` + `weekly_digest` installers drop out of the loop. The retirement docstring mirrors C's allocator-retirement note.

**Data-lane, NOT editable (left verbatim, including their manifest entries):** `scripts/install_launchd_data_operations.sh`, `scripts/install_launchd_data_repair_service.sh`, `ops/weekly_digest.py`, `scripts/run_weekly_digest.sh`, `ops/data_repair_service.py`, `scripts/run_data_operations.sh`.

**Rollback.** No event/schema change → rollback is pure launchd topology: revert the installer/manifest changes and re-run `install_all_daemons.sh` (idempotent unload+reload recreates the old per-daemon plists). Retired installer scripts remain in git history for one release.

## 6. Verification — objective "exactly two daemons" gate (engine-lane, additive)

- **`scripts/tests/test_two_daemon_invariant.py`** (new, engine-lane): statically parse `scripts/install_all_daemons.sh` + the per-daemon installers and assert: exactly one engine-lane `KeepAlive=true` plist (`com.michael.trading.engine-service`); the data-lane `data-repair-service` `KeepAlive` plist + the `data-operations` `StartCalendarInterval` cron are present and untouched; `com.michael.trading.trade-monitor` and `com.michael.trading.weekly-digest` are NOT installed (absent from the manifest loop). This is the build-time invariant.
- **Dashboard `--check` probe `consolidated_daemon_topology`** (engine-lane, additive — NOT added to the data-pipeline audit, which is data-only): asserts the live `launchctl list | grep com.michael.trading.` label set is exactly the expected post-consolidation set. This is the runtime gate. Wire it into the existing dashboard `--check` probe list following the established probe pattern.
- The consolidated daemon keeps emitting `db_log.startup`/`shutdown` and the `trade_monitor_heartbeat` so `trade_monitor_heartbeat` and any engine-service probes stay green.

## 7. Decisions

| ID | Decision | Choice |
|---|---|---|
| D-D3-1 | "Exactly two daemons" definition | Two long-lived `KeepAlive` daemons, one per lane (engine + data-repair) + data-ops cron. Stated in §1/§3. |
| D-D3-2 | `trade_monitor` → engine daemon | Yes — pure engine-lane co-host. |
| D-D3-3 | Co-hosting model | One process/loop/pool; structured supervisor (TaskGroup) with per-task crash-restart; sweep stays in `run_in_executor`. |
| D-D3-4 | `weekly_digest` trigger → engine daemon | Yes, via `python -m ops.weekly_digest emit` subprocess, crash-isolated, cadence-gated, idempotent (C precedent). No edit to the data-lane file. |
| D-D3-5 | Retire weekly-digest launchd plist | **Ratified engine-lane** (operator: Option A-full) — plist + manifest entry only; `ops/weekly_digest.py`/`run_weekly_digest.sh` untouched. |
| D-D3-6 | `data_repair_service` consolidation | Descoped — cross-lane **CL-1**; remains a separate data-lane daemon. |
| D-D3-7 | Consolidated daemon launchd label | Keep `com.michael.trading.engine-service`. |
| D-D3-8 | Rollback artifact | Retired per-daemon installers preserved in git history one release; idempotent `install_all_daemons.sh` is the mechanism. |
| D-D3-9 | Event contracts | Frozen — topology only, zero `application_log`/handshake change. |
| D-D3-10 | Verification | New engine-lane `test_two_daemon_invariant.py` + `consolidated_daemon_topology` `--check` probe; NOT in the data-pipeline audit. |
| D-D3-11 | Already-done items | DA-1/DA-2/allocator/forensics consolidation recorded as complete — verification + documentation only, no code. |

**Cross-lane dependency CL-1:** folding `data_repair_service` requires re-homing `tpcore.selfheal` invocation + the `ste-data-operations.lock` sharing + the exactly-once terminal contract — out of scope for the engine session; recorded for a future cross-lane epic / the data session.

## 8. Testing

- `test_two_daemon_invariant.py` (the §6 build-time invariant).
- Consolidated-daemon supervisor tests: a crashed `sweep` task is restarted without killing `monitor` (and vice-versa); SIGTERM cleanly stops both and closes the pool once; the shared pool is built exactly once.
- **Fill-latency non-regression test:** a slow/blocking fake sweep does NOT delay a `monitor`-task tick (proves the sweep stays off the event loop — the make-or-break property).
- Weekly-digest trigger: cadence gate fires the subprocess at most once per cadence; a non-zero subprocess exit is crash-isolated (logged, daemon survives) — mirror `_invoke_allocator`'s test shape.
- Existing `scripts/tests/test_engine_service.py` (trigger set, `_find_new_trigger`, green-only repair filter) stays green — the sweep behavior is unchanged.
- `scripts/run_smoke_test.sh` / `scripts/pipeline_smoke_test.py` WIRE mode: `tpcore.trade_monitor.main()` standalone entrypoint is **retained** (used by the smoke harness) even though its launchd plist is retired; confirm the smoke path still passes.
- Full repo suite green; CI-exact `ruff` + `check_imports`; lane-discipline assertion (zero data-lane file in the diff; only subprocess seam to `ops.weekly_digest`).

## 9. Out of scope

`data_repair_service`/`data_operations` consolidation (CL-1, data-lane); any `application_log`/schema/handshake change; `trade_monitor` Tier-2 logic changes (relocate only, behavior-preserving); LLM/agentic triage (Epic E); the data-lane two-daemon side (the data session's responsibility).

## 10. Self-review

Spec covers: honest already-done/out-of-lane/in-scope partition (§1), lane discipline + subprocess-seam precedent (§2), the single-engine-daemon target with the three co-hosted units (§3), failure isolation incl. the fill-latency make-or-break (§4), migration with the exact editable vs data-lane file split (§5), an objective build-time + runtime two-daemon gate (§6), all binding decisions incl. the operator-ratified D-D3-5 (§7), tests incl. the off-loop-sweep non-regression (§8), explicit out-of-scope/CL-1 (§9). No placeholders; no contradiction; scoped to a single engine-lane implementation plan; the one operator decision (scope) is resolved (A-full). Ready for expert hardening then writing-plans.
