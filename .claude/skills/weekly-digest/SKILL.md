---
name: weekly-digest
description: "Slash-only wrapper for the canonical weekly digest — python -m ops.weekly_digest. The non-skippable state-comprehension floor: every cutover/self-heal/near-miss-gate + one adversarial 'most likely silently wrong' + 30s binary ack."
disable-model-invocation: true
---

# Weekly digest

Canonical CLI: `python -m ops.weekly_digest`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs the weekly state-comprehension push (operator-facing). For the past week:

- Every CUTOVER (provider swap) the data-feed roster did automatically
- Every self-heal that fired
- Every near-miss gate (e.g. `prices_daily_completeness` borderline)
- One adversarial "most likely silently wrong" item the system flags
- Every UNDISPOSITIONED entry past the 7-day grace from both Ladders (data lane + engine lane; consolidated defect register read-model — see `/defect-register`)

Requires a 30-second binary acknowledgement from the operator. **≥2 unacked weeks ⇒ `live_clearance` auto-de-escalates live trading** (a real, durable safety mechanism — not just a reminder).

## Usage

```bash
python -m ops.weekly_digest
```

The day-rollover trigger is co-hosted in `engine_service` (DA-3 two-daemon consolidation — see `.claude/rules/daemons.md`). This skill is the on-demand replay/run.

## Invariants

- The digest is **non-skippable** — it's the operator's state-comprehension floor on a live-money platform.
- Unacked-weeks → auto-de-escalation is a real safety contract; the digest is not optional UX.
- UNDISPOSITIONED rows ⇒ a ladder class has no policy (every escalation class must carry a disposition per `tpcore/ladder/` clockwork).

## Adjacent SoT

- `ops/weekly_digest.py`
- `.claude/rules/daemons.md`
- `.claude/rules/data-feed-roster.md`
- `.claude/skills/defect-register/SKILL.md`
- `docs/superpowers/specs/2026-05-19-consolidated-defect-register-design.md`
