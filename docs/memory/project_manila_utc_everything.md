---
name: manila-utc-everything
description: "Operator's laptop is in Manila; ALL time reasoning must be UTC + tpcore.calendar (XNYS), never assume a US timezone"
metadata: 
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

The operator's machine runs in Manila (Asia/Manila, UTC+8, no DST) but
the laptop "could be anywhere" — so the system is deliberately
UTC-anchored and trading-day logic MUST go through `tpcore.calendar`
(XNYS), never local time.

**Why:** market is US/Eastern but the operator travels; hardcoding or
assuming a local/US timezone silently corrupts every schedule and
session calculation. The operator called this out forcefully after I
assumed US-Pacific.

**How to apply:**
- Any "today"/"now" in new code → `datetime.now(UTC)`. Any "is this a
  trading day / what sessions" → `tpcore.calendar` (XNYS). Never local.
- launchd `StartCalendarInterval` fires on the machine's LOCAL time.
  In Manila: `05:30 local = 21:30 UTC` (prior day), `Mon 21:00 local
  = Mon 13:00 UTC`. So the plists and the CLAUDE.md "21:30 UTC / Mon
  13:00 UTC" comments are CONSISTENT — do not "fix" an apparent
  mismatch by assuming Pacific/Eastern. Convert with UTC+8 before
  concluding anything about daemon timing.
- Caveat the operator is aware of: launchd has no native UTC mode, so
  a timezone change shifts daemon firing vs UTC. The safety net is
  `ops.py --update` enforcing market-closed via `tpcore.calendar`, so
  schedule drift can't make it run mid-session. Don't change schedules
  to "fix" this without explicit authorization (high-impact, touches
  live trade timing). See [[research-builder-persona]] (DESTRUCTIVE ACTION stop rule).
