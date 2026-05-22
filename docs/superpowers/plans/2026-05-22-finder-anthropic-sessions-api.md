# Plan — Wire LLM finder to Anthropic Sessions API + memstore

**Created 2026-05-22 by subagent.** Replaces `ops/llm_edge_finder_sdk.py`'s stateless `messages.create` path with the Sessions API + the finder memstore for cross-run memory. Spec context: Task #25 §3.2 LLM seam + operator 2026-05-22 memstore handoff.

## Goal

The finder LLM gets stateful cross-run memory via memstore `memstore_01MzLun3AfRf2viPmDqJvsWi`:
- Reads `/agent-context/`, `/prior-emissions/`, `/outcomes/`, `/lessons/`, `/cross-agent/dev-to-finder/` on startup.
- Writes `/sessions/<run_id>.md` + (conditionally) `/lessons/<theme>.md` on completion.
- Does this WITHOUT changing the application's `LLMCallable` protocol or the outer loop in `ops/llm_edge_finder.py`.

## Architecture decision

**One Anthropic session per finder run, many `_call` invocations.** The wrapper is stateful: first `_call` creates the session; subsequent `_call`s send only the new turn's tool_results as the next `user.message` event.

The Agent's `system` field on the Anthropic side holds the persona text (we register it server-side via `agents.create`). The application's `system_prompt` arg becomes redundant; we assert it matches our pinned `PERSONA_SHA256` as defense-in-depth.

**Memstore mounted as a directory** (Anthropic Sessions API contract — `resources[{type: memory_store, ...}]` mounts at `/mnt/memory/<store-name>/`). Agent reads/writes via standard `agent_toolset_20260401` tools (`read`, `glob`, `grep`, `write`, `edit`). NO custom memory tool; the file tools are sufficient.

**Application-side tool sandbox unchanged.** The agent EMITS a JSON envelope (text content of `agent.message`) carrying `tool_calls` (e.g. `OLS_HAC_NW`). The outer loop runs them via the existing `tool_sandbox.dispatch()`. Results go back as the next `user.message`. This keeps the existing tool whitelist + the persona's advisory-only contract intact.

## Parts

### A — One-time setup script
`scripts/anthropic_agent_provision.py` (NEW). Idempotent: looks for an existing agent named `lab-edge-finder` first; only creates if missing. Captures the IDs in:
`ops/llm_finder_anthropic_ids.py` (NEW). Constants module.

### B — SDK refactor
`ops/llm_edge_finder_sdk.py` (REWRITE). Replace `messages.create` with `sessions.create` + `events.send` + `events.stream`. Keep `LLMCallable` protocol shape. Add stateful wrapper class `_SessionLLMState`. Preserve `AuthSkip` semantics + the structured-error retry shape. Old `messages.create` path retained behind a feature flag `use_sessions_api=True` default ON.

### C — Persona §11
Add `## §11 Memory store discipline` to `docs/lab_finder_persona.md`. Teach the LLM the memstore conventions (read/write paths, curation rules). Bump `PERSONA_VERSION` v2.2 → v2.3 + new SHA256.

### D — Curation policy memory entry
Seed `/agent-context/curation-policy.md` in the finder memstore via `memory_stores.memories.create()` (one-time, idempotent).

### E — Tests
`ops/tests/test_llm_edge_finder_sdk.py` (UPDATE). Mock `sessions.create` + `events.send` + `events.stream`. Preserve coverage: AuthSkip on auth error, JSON-decode failure → synthetic AnalysisRequest, transcript→events shape, no `tools` param in messages (vacuous now), persona on server-side.
Update the 4 persona-version-pin tests: `test_persona_versioned.py`, `test_run_writer.py`, `test_models_frozen.py`, `test_llm_edge_finder_to_outcome_proven.py`.

### F — Live pilot
`.venv/bin/python -m ops.llm_edge_finder --trigger operator_command --target catalyst`
Verify: session created, memstore reads observed in events, no re-emission of prior candidates, `/sessions/<run_id>.md` write observed.

## Risks / safeguards

- **No git stash.** Standing rule.
- **Heavy-lane discipline.** `ops/llm_*` is heavy-lane → whole-suite + reverse-order pytest before push.
- **Subagent worktree.** Already in `.claude/worktrees/reversion-probe-wiring/` per parent's dispatch (isolation=worktree).
- **Cost cap.** Pilot = ONE run, target $0.05-0.20. Don't loop the pilot.
- **In-source state restore.** No monkeypatching outside test fixtures; tests use finally-block restore.
- **529 self-heal.** Per `feedback_anthropic_529_self_heal` — existing backoff (15s/300s) is preserved.

## Non-goals

- NOT migrating Epic-E `llm_triage_*` modules (they remain raw `messages.create`).
- NOT exposing the agent via the Sessions API to operator-driven coding (that's Claude Code's separate role).
- NOT building a session-events tail for the operator dashboard (deferred to a follow-up).
