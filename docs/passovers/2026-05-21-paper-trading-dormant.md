# Discovery — Paper Trading Layer Is Dormant (2026-05-21 15:30 UTC)

**Surface:** run-everything-to-find-bugs cadence, paper-trading lane.
**Status:** discovery filed. NOT investigated further pending operator triage decision.

## §1 Headline finding

**Paper trading is dormant.** The 6 PAPER engines that the autonomous edge finder will eventually promote candidates INTO have been **largely silent for 5 days**. The `engine_service` daemon process is running (PID 77455) but emits ZERO events to `application_log` and engines aren't firing on their scheduled cadences.

This means the autonomous-finder layer (Task #25 Path B v1.0, just shipped) is built on top of an engine substrate that's not running.

## §2 Engine activity per engine (last 7d, from `application_log`)

| Engine | Last STARTUP | Recent fires |
|---|---|---|
| reversion | 2026-05-16 15:06 UTC | 8 events (all 5d ago — pipeline_smoke session) |
| vector | 2026-05-16 15:07 UTC | 18 events (all 5d ago — pipeline_smoke session) |
| sentinel | 2026-05-16 15:08 UTC | 8 events (all 5d ago) |
| **momentum** | **NEVER fired in 7d window** | 0 events |
| **canary** | **NEVER fired in 7d window** | 0 events |
| catalyst | 2026-05-20 03:02 UTC | 3 events — fired ONCE 1d ago |
| (sigma — archived) | 2026-05-16 15:06 UTC | 27 events (pre-archive run) |

All "recent" engine activity from 2026-05-16 15:06-15:08 corresponds to a `pipeline_smoke` test (22 events under engine='pipeline_smoke'), not real scheduled fires. **The real-scheduled-engine cadence has been silent.**

## §3 Daemon observability gap

`launchctl list` shows engine-service / data-repair-service / llm-triage-service ALL RUNNING (with PIDs). BUT:

- `application_log` has **0 events** with `engine='engine_service'` across the entire week.
- `application_log` has **0 events** with `engine='data_repair_service'`.
- `application_log` has **0 events** with `engine='llm_triage_service'`.

**These daemons emit nothing to the canonical event substrate.** This breaks:
- The §12 audit-trail story (everything is supposed to surface via `application_log`).
- The `consolidated_daemon_topology` check's expectation (daemons must be observable).
- Any debugging of "is engine_service actually running its scheduled dispatch?" — there's no log to read.

The processes ARE up, but operationally they're unobservable from the canonical log. This is a real bug — either the daemons log elsewhere (stderr → file → never to DB), or they're stuck in a sleep/wait and never emit, or the `engine=` label is something different from the daemon name.

## §4 launchd topology violations (already surfaced by `--check`)

5 unexpected launchd labels (closed whitelist = engine-service + lane-service + data-operations):
- `com.michael.trading.allocator` (idle, no PID)
- `com.michael.trading.allocator-heartbeat` (idle)
- `com.michael.trading.llm-triage-service` (RUNNING PID 77479 — should have been consolidated into lane-service per DA-3, but is a separate daemon)
- `com.michael.trading.momentum-oneshot-2026-05-27` (idle leftover)
- `com.michael.trading.pipeline-smoke-test` (idle leftover)

## §5 Paper-trading data-shape state

| Table | Row count | Status |
|---|---|---|
| `platform.open_orders` | 0 | no live paper-trading positions |
| `platform.aar_events` | 0 | no recorded trade outcomes |
| `platform.risk_state` | 4 rows | only 4/6 engines tracked |
| `platform.allocator_budget` | **MISSING** | table doesn't exist (or renamed) |

The dashboard's "what is paper trading doing today" surface has nothing to show — there's no live activity to render.

## §6 Why this matters for Task #25 Path B

The autonomous edge finder (just shipped end-to-end in PR sequence #232 → #251) emits ProposedSpec candidates → SP-G emit_once → auto-merge → ECR-MODIFY → engine_sdlc promotes to PAPER → **engine_service dispatches the engine** → Phase E monitor reads `aar_events` for LiveOutcome → operator marks Tier-2 verdict.

**The bolded step is the broken-and-silent layer.** If engine_service isn't firing engines, the finder's emissions land in PAPER but nothing happens — no trades, no `aar_events`, no LiveOutcome for the §12 dashboard to show, no operator verdict to mark. The autonomous loop has nothing to monitor.

## §7 Triage menu (operator picks)

1. **Investigate `engine_service` NOT firing engines** — single biggest gap. Walk:
   a. Why does the daemon emit zero application_log events?
   b. Is the scheduler tickless (stuck in a sleep)?
   c. Are the engines' next-fire timestamps in the future per `engine_profile._cadence_window_start`?
   d. Manually invoke `python -m ops.engine_service` from the foreground to see what it does.

2. **Clean up the 5 unexpected launchd labels** — straightforward `launchctl unload` + plist delete; eliminates the topology violation; doesn't fix the silent-engine bug.

3. **Materialize the `allocator_budget` table** — separate sub-bug; may or may not block engine_service.

4. **Run a single engine manually** to prove the scheduler path works in isolation — e.g. `python -m reversion.scheduler` with a smoke-fake-broker. Bypasses the daemon, surfaces engine-side bugs.

## §8 Where session 869ca3ee can move next

I have the paper-trading lane (per operator 2026-05-21). The right NEXT-action given operator's "run everything to find errors + self-heal" cadence is **(4) from §7** — run a single engine manually to surface engine-side bugs WITHOUT touching the engine_service daemon (which is its own investigation). Pick the smallest engine path (likely `canary`) since it's never fired and there's likely a discoverable reason.

Filing this as a passover for now in case operator wants me on a different lane.

## §9 Investigation update — root cause + scope (2026-05-21 15:38 UTC)

A heavy-lane diagnosis of `ops/engine_service.py` against the live DB found the situation is **two distinct issues** — the operator's "engines are dormant" framing conflated them:

**Issue 1 (the observability gap — fixable here):** `ops/engine_service.py` emits ONLY to structlog → `~/Library/Logs/short-term-trading-engine/engine-service.log`. It never wrote a single row to `platform.application_log` under `engine='engine_service'`. So `SELECT * FROM platform.application_log WHERE engine='engine_service'` correctly returned zero rows — even though the daemon was alive and polling correctly. **The operator's "the daemon is silent" finding was an observability gap, not a dispatch bug.**

**Issue 2 (the data lane is red — out-of-scope for engine_service):** The daemon's trigger predicate (`DATA_OPERATIONS_COMPLETE` OR green `DATA_REPAIR_COMPLETE`) has had nothing to fire on since 2026-05-14 22:52 UTC. Exactly **ONE** `DATA_OPERATIONS_COMPLETE` row exists in the last 14 days, and the daemon's cursor advanced past it long ago. No `DATA_REPAIR_COMPLETE` has EVER been written.

The reason no trigger has emitted: `scripts/run_data_operations.sh` Step 4/4c has been **failing data_validation repeatedly** (most-recent fails 2026-05-21 14:19 UTC and earlier — `fundamentals_quarterly_completeness`, `corporate_actions_completeness`, `earnings_events_monotone`, `sec_insider_monotone`, `liquidity_tiers_completeness`, `ticker_classifications_coverage`, `macro_indicators_completeness`, `fear_greed_freshness`, `aaii_sentiment_freshness`; plus `daily_bars` timeouts + coverage-collapse refusals + a `greeks_max_pain` 401). The "100% green or don't trade" invariant is correctly preventing the pipeline from reaching Step 6 emit. Engines correctly are NOT firing — the data substrate is red.

**The engine_service daemon is doing exactly what it should**: polling for a trigger that hasn't legitimately arrived. There is no engine-dispatch bug to fix in `ops/engine_service.py`.

**The fix landing in this session** (the in-scope observability fix): add daemon-lifecycle emits to `application_log` under `engine='engine_service'` — `ENGINE_SERVICE_STARTED` / `ENGINE_SERVICE_STOPPED` / `ENGINE_SERVICE_TRIGGER_SEEN` / `ENGINE_SERVICE_SWEEP_START` / `ENGINE_SERVICE_SWEEP_DONE` / `ENGINE_SERVICE_POLL_FAILED`. Crash-isolated (a failed write logs to structlog and is swallowed — observability must never break the supervisor loop). One stable `run_id` per daemon-process lifetime ties the row family together. Verified end-to-end against the live DB: a foreground `python -m ops.engine_service` for 5s now produces matching STARTED + STOPPED rows in `application_log` under `engine='engine_service'`.

**Out of scope for this session** (separate lane(s)):

- **The data_validation red set** — operator's other session owns this. The data layer being red is the real cause of "no engine activity"; until data_validation goes green and `DATA_OPERATIONS_COMPLETE` emits, no scheduled engine sweep will fire (correct safety behavior, by design).
- **Production daemon reload** — the launchd-managed `com.michael.trading.engine-service` (PID 77455 at investigation time) is still running the pre-fix code. The operator should bounce the daemon (`launchctl kickstart -k system/com.michael.trading.engine-service`) to pick up the new observability emits.
- **The 5 unexpected launchd labels** (allocator, allocator-heartbeat, llm-triage-service-as-separate-daemon, momentum-oneshot-2026-05-27, pipeline-smoke-test) — secondary cleanup, operator-directed scope.
- **`data_repair_service` / `llm_triage_service` observability gap** — same pattern (structlog-only); follow-on for the same fix shape.
- **The missing `platform.allocator_budget` table** — separate sub-bug.
