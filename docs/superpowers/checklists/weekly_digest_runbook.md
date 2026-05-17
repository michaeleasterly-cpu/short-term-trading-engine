# Weekly Digest — Operator Runbook

The non-skippable state-comprehension floor. Rationale: minimizing
*operator interaction* is the wrong objective — minimize *opportunity
for irreversible harm* while keeping the operator's mental model warm
for the rare crisis. A fully-autonomous data layer nobody looks at is
one whose state you cannot model when something is silently wrong; and
"config-reversible" ≠ "consequence-reversible" (money moved on quietly-
degraded data is already irreversible). This is the periodic adversarial
pressure-test of the gates themselves (verify-the-verifier).

## What happens automatically

- Once per ISO week the system PUSHES a one-page digest to
  `platform.application_log` (`WEEKLY_DIGEST`, severity WARNING so it
  surfaces in dashboards) + a best-effort local notification. Emitted
  by the `com.michael.trading.weekly-digest` LaunchAgent
  (`run_weekly_digest.sh` → `python -m ops.weekly_digest emit`);
  idempotent per ISO week (safe to fire daily — no-ops until a new
  week).
- The digest contains: every provider cutover, every self-heal that
  fired **and what it changed**, every gate that **passed within 5% of
  failing**, and ONE adversarially-surfaced *"most likely silently
  wrong right now"* item.

## The one operator action (30 seconds, binary, zero fat-finger)

Read the digest, then:

```
python -m ops.weekly_digest ack
```

That's it. Read-then-ack — no structural edit, nothing to mis-type.
`status` shows the current clearance:

```
python -m ops.weekly_digest status      # live_cleared=<bool> — <reason>
```

## Auto-de-escalation (the teeth — this is not theatre)

`ops.weekly_digest.live_clearance(pool)` counts consecutive most-recent
weekly digests with no matching ack:

- **0 unacked** → cleared (`weekly digest current`).
- **1 unacked** → cleared, but warned ("one more miss auto-de-escalates").
- **≥ 2 unacked** → **NOT cleared**: live trading is auto-de-escalated
  (the data layer withholds the live-clear). A single `ack` restores it
  immediately (acks the latest week).

## Integration handshake (documented, not wired across the lane here)

`live_clearance` is the **DATA-lane signal**. The data-ops all-clear /
engine dispatch consults it as an additional precondition for
*live* trading — same family as "100% data or don't trade". This
runbook defines the contract; the consult is wired at the
data-ops/engine boundary (not edited across the lane boundary in the
PR that introduced this module). Until wired, `status` exit code 2
already exposes the de-escalation programmatically.

## Why you cannot safely skip it

Skipping isn't "less interaction" — it's the system trading on a data
layer whose health no human has pressure-tested, with the gates
unsupervised. The ack is the cheapest possible insurance against
silent semantic drift; the de-escalation guarantees that insurance is
not optional.
