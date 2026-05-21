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
