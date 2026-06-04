---
name: utc-everything-operator-currently-sf
description: "Operator currently in San Francisco (PT, UTC-7/-8). ALL time reasoning in code is UTC + tpcore.calendar (XNYS) regardless. Was in Manila through 2026-05-30."
metadata: 
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

The operator's machine "could be anywhere" — they travel — so the
system is deliberately UTC-anchored and trading-day logic MUST go
through `tpcore.calendar` (XNYS), never local time.

**Current location:** San Francisco (PT) as of 2026-05-31.
**Prior location:** Manila (Asia/Manila, UTC+8, no DST) through 2026-05-30.

**Why:** market is US/Eastern but the operator travels; hardcoding or
assuming any local timezone (US-Pacific OR Manila) silently corrupts
every schedule and session calculation. The operator called this out
forcefully when I assumed US-Pacific while they were in Manila —
same lesson applies the other direction now that they're in SF.

**How to apply:**
- Any "today"/"now" in new code → `datetime.now(UTC)`. Any "is this a
  trading day / what sessions" → `tpcore.calendar` (XNYS). Never local.
- Operator wall-clock messages: when the operator says a time without
  a TZ suffix (e.g. "I'll check back at 5pm"), interpret as **Pacific**
  while they're in SF; was **Manila** previously. When in doubt, ask
  — don't silently assume.
- launchd `StartCalendarInterval` fires on the machine's LOCAL time.
  **Schedules were calibrated for Manila** (`05:30 local = 21:30 UTC`,
  `Mon 21:00 local = Mon 13:00 UTC`). If the operator's laptop has
  moved to Pacific:
    * On laptop reboot/timezone change, launchd will fire at the **SAME
      LOCAL CLOCK TIME** under the new timezone — i.e. 05:30 PT instead
      of 05:30 Manila. PT 05:30 = 13:30 UTC (vs the calibrated 21:30
      UTC). That's an **8-hour drift** of every scheduled daemon.
    * The safety net is `ops.py --update` enforcing market-closed via
      `tpcore.calendar`, so schedule drift can't make daemons run
      mid-session — but the cadence is wrong.
    * **Do NOT change schedules to "fix" this without explicit
      operator authorization.** High-impact, touches live trade timing.
      If the operator wants schedules re-tuned for Pacific:
        - 05:30 PT → re-derive UTC equivalent (13:30 UTC summer, 14:30
          UTC winter due to PDT/PST) and update the plists.
        - But ASK FIRST; the operator may be temporarily in SF and
          want schedules to keep firing on Manila time.
- See [[feedback_research_builder_persona]] (DESTRUCTIVE ACTION stop
  rule) — schedule changes are destructive.

**Historical context:** The Manila / UTC-anchored design predates
the SF relocation. The DURABLE principle is "UTC-anchored in code,
XNYS for sessions, never assume a US timezone." The TRANSIENT fact
is whichever city the laptop's currently in.
