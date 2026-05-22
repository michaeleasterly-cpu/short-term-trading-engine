---
name: triage-data-failures
description: "Slash-only wrapper for the OPERATOR-LOCAL LLM data-recovery agent — python -m ops.llm_triage_service (data lane only, one-shot). Reads recent DATA_REPAIR_ESCALATED / DATA_SOURCE_ESCALATED / INGESTION_AUTO_RECOVERY_FAILED rows from platform.application_log and runs ops.llm_data_recovery.run_autonomous_recovery once per outstanding escalation. Runs on the operator's Claude Max account — NOT deployed to Railway."
disable-model-invocation: true
---

# Triage data failures (operator-local LLM data-recovery)

Canonical CLI: `python -m ops.llm_triage_service`
Authoritative external: <https://code.claude.com/docs/en/skills>
Sibling audit: `docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md`

## What this skill does

Runs **one** operator-local LLM data-recovery cycle. Polls
`platform.application_log` for the data-lane escalation set
(`AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES`) and, for each outstanding
escalation, fires `ops.llm_data_recovery.run_autonomous_recovery`:

- the LLM picks ONE stage + params from the frozen whitelist
  (`_AUTONOMOUS_DATA_ACTIONS`);
- the deterministic validator gates it;
- the bounded subprocess runs it credential-starved;
- one terminal event (success or failure) lands back on
  `platform.application_log`.

**NO draft PR. NO human-merge gate. Single-shot per cycle.** Engine-
roster / engine-code mutations are NOT in scope — those stay on the
engine lane's PR-gated path (`/triage-engine-failures`).

## Why this is operator-local

Per operator directive 2026-05-21 ("we wont be deploying the llm data
triage it will run locally with my max account"): the deployed
`lane-service` daemon runs DETERMINISTIC SELF-HEAL ONLY. The LLM
invocation moved to the operator's machine — this skill is the
canonical invocation path. The deterministic cascade in
`scripts/ops.py::_auto_cascade_*` still emits the escalation events;
this skill picks them up.

## Usage

```bash
# Plain one-shot — reads the current escalation set and runs recovery:
python -m ops.llm_triage_service
```

The CLI entry point (`ops/llm_triage_service.py::main`) was the
launchd daemon's entry on the *previous* topology; now the operator
invokes it manually (or via this slash skill) instead of via a launchd
plist. The poll-loop body is unchanged — it's a one-shot in this
flow because the operator runs it on demand, not as a `KeepAlive`
daemon.

## Pre-conditions

- `ANTHROPIC_API_KEY` is available to the operator's local environment
  (operator's Claude Max session credentials).
- `DATABASE_URL` (or `DATABASE_URL_IPV4`) is set so the agent can read
  `platform.application_log`.
- The deterministic cascade (`scripts/ops.py::_auto_cascade_*`) has
  already exhausted and emitted one of:
  - `DATA_REPAIR_ESCALATED`
  - `DATA_SOURCE_ESCALATED`
  - `INGESTION_AUTO_RECOVERY_FAILED`

If there is no outstanding escalation, the agent is a safe no-op.

## After the run

The agent emits exactly one terminal event per recovered escalation.
Inspect the result via:

```bash
psql "$DATABASE_URL" -c "
SELECT recorded_at, event_type, message
FROM platform.application_log
WHERE event_type LIKE 'INGESTION_AUTO_RECOVERED_%%'
   OR event_type LIKE 'AUTONOMOUS_DATA_RECOVERY_%%'
ORDER BY recorded_at DESC LIMIT 10;
"
```

Nothing in this skill touches the deterministic cascade, the
deployed daemon, the engine lane, or the Lab gate. The chain is
deliberately discontinuous at every boundary.

## Adjacent SoT

- Audit: `docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md`
- Memory: `llm-triage-runs-local-on-max`,
  `deterministic-cascade-architecture`
- Persona: `docs/llm_triage_personas/data_recovery_v2.md`
- Sibling skill (engine lane): `/triage-engine-failures`
- Sibling skill (lab emitter): `/lab-spec-emit`
- Deployed daemon (deterministic only): `ops/lane_service.py`
