---
name: three-service-architecture
description: "Operator's canonical system topology — three first-class services (data, engine, aar) with platform-level overlays managing their interaction."
metadata: 
  node_type: memory
  type: project
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

The operator's mental model of this platform (stated + clarified
2026-05-17), to be used when reasoning about scope, naming, and where
a concern belongs:

**Three first-class services, each its own pipeline:**
- **Data** — ingestion → `platform.*` tables → validation/self-heal.
  Has its own comprehensive audit (`scripts/audit_data_pipeline.py`,
  renamed 2026-05-17 from the ambiguous `audit_pipeline`).
- **Engine** — setup_detection → signal → execution → orders.
- **AAR** — fills → classify → forensics/dossiers.

**Flow:** data **feeds** engine; engine **feeds** aar; aar **feeds
back** into engine.

**Resolved design points (operator, 2026-05-17):**
1. **RiskGovernor is a PLATFORM-LEVEL overlay**, not part of the engine
   service — it (and the allocator, forensics scanner,
   validation/self-heal) sits *across* the three services managing
   their interaction. Every engine plugs into it from tpcore.
2. **The aar → engine feedback edge is meant to be mechanical
   (auto-tune), not just observability.** Intent: the AAR loop should
   automatically tune engines so they actually fire/trade — "whatever
   makes the shit fire." Treat aar→engine as an active control path,
   not a dashboard.
3. **Per-service audit symmetry (engine/AAR analogs of the data
   audit): expert's call.** Expert recommendation on record: yes,
   long-term each service should get its own `audit_<svc>_pipeline`
   surface for symmetry — but it is future scope; today only the data
   audit exists, engine/AAR are covered by smoke tests + the live
   forensics scanner (weaker, point-in-time).

**Execution model — EVENT-DRIVEN EVERYWHERE (operator directive,
2026-05-17, load-bearing):** "We don't live on a timeline. We do things
as soon as the setup is ready." Every service/engine fires the moment
its **preconditions** are satisfied — data present + market closed +
setup ready — NOT on a cron/clock. Time is only ever a **gate /
precondition** (e.g. `tpcore.calendar` market-closed check), NEVER a
**trigger**. Today the engine service is already event-driven
(`ops/engine_service.py` polls `application_log` for
`DATA_OPERATIONS_COMPLETE` → `run_all_engines.sh`); the **allocator is
the time-driven outlier** (launchd Mon 13:00 UTC) and the operator wants
it (and any other time-driven daemon) converted to fire on a readiness
event + an idempotent "already ran this cycle / first-trading-day-of-week"
guard. Target end state: no service is launched by a calendar; each
subscribes to the event that means "your inputs are ready" and self-
gates on the time *constraints* that still apply. Engines fire as soon
as they can, not because a schedule said so.

**Per-engine cadence is itself a precondition (operator, 2026-05-17):**
event-driven does NOT mean every engine runs every day. Engines have
distinct cadences — reversion/vector **daily**, momentum **monthly**
(12-1 rebalance), sentinel **regime/event-driven**, allocator
**weekly**. In the event model the common readiness event (data ready +
market closed) fires for everyone; each engine then **self-gates on its
own cadence boundary** ("is today my rebalance point?" via
`tpcore.calendar`) and runs idempotently (a "already ran this
cycle" guard) so a daily event can't double-fire a monthly/weekly
engine. Cadence is a *gate*, like market-closed — never a *trigger*.
Understanding each engine's cadence is required before migrating it off
the clock.

**Declarative engine profile (operator + expert-endorsed, 2026-05-17):**
the firing model should be a per-engine declarative profile — the SAME
proven pattern as the `tpcore.feeds` data profile (`freshness_max_age_days`,
which scheduled data downloads/validation) and `tpcore.risk.limits_profile`.
An `engine_profile` declares each engine's cadence + precondition gates
(daily/monthly/weekly, market-closed required, data-ready, setup-ready);
the engine daemon, on the readiness event, consults the profile to
decide "is this engine's cadence boundary now AND preconditions met?"
One canonical SoT, symmetric, no per-engine cron. This is the design
vehicle for the event-driven migration. **EXTEND, don't fork:** a
per-engine DATA GATE already exists ("Per-engine data gates — DONE
2026-05-16"; the `capital_gate`/per-engine gate plumbing) — the
`engine_profile` must build ON that existing per-engine gate setup, not
introduce a parallel mechanism. The epic's first step is to inventory
the existing per-engine gate and extend it with cadence/preconditions.

**Data-not-ready ⇒ self-heal-then-recheck + agentic framing (operator,
2026-05-17):** when an engine's data precondition fails, the dispatcher
does NOT just skip — it triggers the EXISTING `tpcore.selfheal.run_self_heal()`
(canonical bounded repair; re-derives its own targets from the
validation suite) then re-evaluates. The operator's "engine agent: hey
data, engine 1 needs this → data agent fixes it → data agent: ready,
re-check" is this loop wearing an agent hat: the two "agents" ARE the
two daemons (data + engine), the message channel IS
`platform.application_log` events (already how `DATA_OPERATIONS_COMPLETE`
decouples data-ops↔engine-sweep). Implement as two typed events +
consume/emit on existing daemons — NOT a new agent framework or message
broker. "Use existing shit" = application_log bus + tpcore.selfheal +
should_fire. Scoped to Sub-projects B/D, not the engine_profile
foundation (A).

**Future Epic E — agentic forensics/triage layer (operator endorsed
2026-05-17, "brainstorm later"):** LLM/agentic AI is wanted, but the
expert-agreed position is: NEVER an LLM in the live trade-submit or
data-repair hot path (breaks the determinism/fail-closed/audit
discipline — the Connor/spider-web risk). It belongs OFF the hot path
at the *escalation boundary*: when self-heal honestly escalates, an
LLM agent investigates + correlates + drafts a root-cause dossier +
*proposes* (never auto-executes) a fix — the natural evolution of the
existing `tpcore/forensics/` Sprint Dossiers; advisory/gated. Also
research/strategy ideation for human-gated review (no in-sample→live
auto-promotion). The deterministic A→D substrate is the foundation;
the agentic layer sits ON TOP of it. Its own brainstorm/spec when
reached — do NOT fold into A→D. **When Epic E is built it MUST use the
official Anthropic/Claude documentation via the `claude-api` skill
(prompt caching, tool use, latest models) — never winged from training
data (operator: "refer to claude documentation").**

**"Fix itself + keep itself running" = the DETERMINISTIC autonomous
layer = Sub-projects B + D (already specced), NOT a new thing and NOT
Epic E.** B = data-not-ready ⇒ existing `tpcore.selfheal` ⇒ re-check ⇒
fire (self-repairing, no human, no LLM, hot-path-safe). D = supervised
two-daemon topology (bounded retry / honest escalation / locks). A
(`engine_profile.should_fire`) is the keystone B consumes — A lands
first, then B IS the self-healing autonomous behavior. Order A→B→C→D
is not reorderable.

**Two-daemon target topology (operator, 2026-05-17):** consolidate to
exactly TWO daemons — (1) the **data daemon** (data service:
ingestion→validation→self-heal, emits the readiness event) and (2) the
**engine daemon** (everything else, event-driven inside it: engine
firing per the engine_profile, the allocator, AAR, and forensics —
governor enforcement is already in-process here). AAR + forensics +
allocator all move INTO the engine daemon (no separate launchd jobs).
End state: data daemon emits "ready" → engine daemon does all
downstream work, each piece self-gated by profile/cadence/idempotency.

**Why this matters:** the generic name "pipeline" conflated all three
services and caused real confusion; naming/scoping must always specify
WHICH service. Cross-service concerns belong in tpcore as platform
overlays, never inside one engine. See
[[operating-identity-for-this-system]] (tpcore-first / symmetry).
