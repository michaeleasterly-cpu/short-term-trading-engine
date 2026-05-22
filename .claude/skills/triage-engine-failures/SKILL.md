---
name: triage-engine-failures
description: "Slash-only wrapper for the OPERATOR-LOCAL LLM engine-triage agent — python -m ops.engine_llm_triage (engine lane, advisory + draft PR + human-merge). Reads recent ENGINE_ESCALATED rows from platform.application_log and runs ops.engine_llm_triage.run_triage once per open escalation. Runs on the operator's Claude Max account — NOT deployed to Railway."
disable-model-invocation: true
---

# Triage engine failures (operator-local LLM engine-triage)

Canonical CLI: `python -m ops.engine_llm_triage`
Authoritative external: <https://code.claude.com/docs/en/skills>
Sibling audit: `docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md`

## What this skill does

Runs **one** operator-local LLM engine-triage cycle. Polls
`platform.application_log` for `ENGINE_ESCALATED` events, intersects
with `engine_ladder.list_undispositioned()` (open + past-grace +
undispositioned), and fires `ops.engine_llm_triage.run_triage` per
novel escalation:

- runs in a credential-starved ephemeral `git worktree`;
- calls Anthropic `messages.create` with NO `tools` param
  (advisory text only);
- emits a non-authoritative `ENGINE_LLM_TRIAGE_PROPOSAL` event;
- produces a DRAFT, HUMAN-MERGE-ONLY PR (additive `RemediationSpec`
  binding to an existing verb — never a mechanism change).

**Engine lane stays PR-gated** — unlike the data lane, the engine
lane does NOT execute autonomously. The operator (and a second human
reviewer) must merge the draft PR for the proposal to take effect.

## Why this is operator-local

Per operator directive 2026-05-21 ("we wont be deploying the llm data
triage it will run locally with my max account"): the deployed
`lane-service` daemon runs DETERMINISTIC SELF-HEAL ONLY. The LLM
invocation moved to the operator's machine.

## Usage

```bash
# Plain one-shot — reads the current undispositioned set and runs triage:
python -m ops.engine_llm_triage
```

## Pre-conditions

- `ANTHROPIC_API_KEY` is available locally (operator's Max session).
- `DATABASE_URL` (or `DATABASE_URL_IPV4`) is set.
- An `ENGINE_ESCALATED` event exists in `platform.application_log`
  AND the engine Ladder has it open + undispositioned + past grace.
- `gh` is on PATH (the agent opens a draft PR).

If there is no novel escalation, the agent is a safe no-op (the
agent re-checks the Ladder open set itself — same-cycle deterministic
resolution makes the pass a no-op).

## After the run

The agent opens (at most) one draft PR per novel escalation. The
operator reviews, hardens, and (if appropriate) merges. The Ladder
NEVER mutates from the LLM's output — only via the deterministic
disposition path.

## Bright lines (the advisory contract)

- Advisory only. The LLM is not the detector.
- Event-driven, NOT scheduled.
- Draft PR + human-merge only.
- No `tools` param to the Anthropic SDK.
- Gated by the deterministic `engine-llm-triage-fence` CI check.

See `.claude/rules/llm-triage.md` for the full set.

## Adjacent SoT

- Audit: `docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md`
- Memory: `llm-triage-runs-local-on-max`
- Persona: `docs/engine_llm_triage_persona.md`
- Sibling skill (data lane): `/triage-data-failures`
- Sibling skill (lab emitter): `/lab-spec-emit`
- Deployed daemon (deterministic only): `ops/lane_service.py`
- Rule: `.claude/rules/llm-triage.md`
