---
name: event-driven-not-scheduled
description: Operator prefers event-driven invocation on the existing application_log bus (sibling daemon) over scheduled workflows / linear pipeline steps
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

When wiring a new component's invocation, default to **event-driven
on the existing `platform.application_log` bus via a sibling daemon**
mirroring `ops/data_repair_service.py` / `ops/engine_service.py`
(`_main_loop` cursor-poll + `_run_supervised` + `main()` shim,
installed via `scripts/install_all_daemons.sh`) — **not** a scheduled
GitHub workflow / launchd cron, and **not** a hardcoded linear step
inside `scripts/run_data_operations.sh`.

**Why:** explicit operator directive 2026-05-18 on #187 P3: *"i dont
want a scheduled workflow i want an event driven incantation."*
Consistent with the platform's existing trajectory — the allocator's
launchd cron was already retired 2026-05-17 in favour of an
event-driven gate (CLAUDE.md "event-driven 2026-05-17"). The
application_log bus + supervised-poll daemon IS the canonical
mechanism; a new sibling daemon reusing that infra is NOT a "second
pipeline / rat's nest" — it's the one canonical pattern. A scheduled
workflow or a new linear script step is the anti-pattern here.

**How to apply:** for any "when should X run" wiring decision, first
ask what event on `application_log` should trigger it, and add a
sibling daemon (registered in the existing installer) — before
proposing cron/scheduled-workflow/linear-step. Reuse
`_run_supervised`/`_main_loop`/`TRIGGER_EVENT_TYPES` idioms verbatim;
do not re-author. Pairs with [[feedback_tpcore_reuse]] and the
[[project_three_service_architecture]] bus topology; the future
"event-driven epic" referenced in [[project_engine_sdlc_lifecycle]]
is the same principle generalized.
