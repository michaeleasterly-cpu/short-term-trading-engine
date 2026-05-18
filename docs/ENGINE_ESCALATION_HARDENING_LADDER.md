# Engine-Lane Escalation & Hardening Ladder

**Principle:** every engine escalation terminates in exactly one of
`converted` (a new bounded deterministic capability — e.g. a new DA-1
detector/healer or DA-2 trigger class), `structural` (a structural fix
to DA-1/DA-2/engine_profile logic or config), or `removed` (the engine
de-escalated from live capital — archived / kill-switched /
graduation-gated out). **Never silent best-effort; never an indefinite
hold without a recorded disposition.** Engine-native; symmetry-
references the data-lane ladder (`docs/ESCALATION_HARDENING_LADDER.md`)
— same shape, NOT a clone, lane-separate.

## The rungs

- **R1 — fail-closed (exists):** DA-1 (`ops/engine_supervisor`) and
  DA-2 (`ops/aar_autotune`) emit `ENGINE_ESCALATED` (+`ENGINE_HELD`
  for held-class) when a bounded agent can't resolve a failure; a held
  engine is gated off by `tpcore.engine_profile.should_fire`. DA-2
  *escalate-only* (noise: outlier_loss / short loss_cluster) emits
  `ENGINE_ESCALATED` with NO hold — the engine keeps trading by
  design. **Engine-daemon co-hosted platform-service failures (Epic E
  Phase-0):** the consolidated engine daemon (`ops/engine_service.py`)
  now also emits *escalate-only* `ENGINE_ESCALATED` for two frozen
  classes — `engine_service_task_crashloop` (a co-hosted
  `_run_supervised` task crash-looping past a 3-in-600s budget; the
  log+5s-backoff restart is unchanged, the escalation is advisory) and
  `engine_service_digest_failed` (a swallowed weekly-digest spawn error
  / non-zero rc). Deterministic `hold_id` (`engsvc-<sha256[:16]>` of
  `class|task`), `engine="engine_service:<task>"`, NO `ENGINE_HELD`
  (advisory — the daemon keeps running). Surfaced + dispositioned via
  R3 like any other escalate-only class.
- **R2 — clockwork forcing-function:** `ops/engine_ladder.py`
  `DISPOSITION_POLICIES` covers every class in
  `KNOWN_ESCALATION_CLASSES` (derived from
  `engine_supervisor.INFRA_FAILURE_CLASSES |
  {aar_autotune._BEHAVIORAL}`, with `_classify` pinned to the
  constant). `escalation_drift()` ⇒ a new DA-1/DA-2 class **fails the
  build** until a policy is recorded.
- **R3 — surface + disposition:** `python -m ops.engine_ladder list`
  shows undispositioned instances past a 7-day grace
  (`ENGINE_LADDER_GRACE_DAYS`). Held-class closes on `ENGINE_CLEARED`
  or a disposition; **escalate-only** closes on a disposition OR all
  its payload `triggers` fingerprints resolved/absent from
  `forensics_triggers` (so the "every escalation terminates" claim is
  literally true for the no-hold case — except an escalate-only row
  with no recorded fingerprints, which cannot auto-close and requires
  a manual disposition). `python -m ops.engine_ladder
  disposition <hold_id> <converted|structural|removed> [note]` records
  an event-sourced `ENGINE_ESCALATION_DISPOSITIONED`.
- **R4 — structural removal levers (existing, no new code):** the
  `removed` disposition is physically realized via
  `RiskGovernor.emergency_kill`/kill-switch, the DSR/credibility
  graduation gate, or `archive/<engine>/EULOGY.md` + the Engine SDLC
  snap-out.
- **R5 — LLM/agentic triage: BUILT 2026-05-18 (Epic E).**
  `ops/engine_llm_triage.py` is an **advisory + human-gated** triage
  analyst for genuinely-novel engine escalations — the engine-native
  symmetric mirror of the shipped data-lane #187 (symmetry-of-approach,
  NOT a clone). It triggers on `ENGINE_ESCALATED`, selects only
  `hold_id`s in `engine_ladder.list_undispositioned()` (open +
  undispositioned + past grace; the corrected §7 predicate — NOT an
  "unknown class", structurally impossible) minus prior-proposal dedup,
  reads the repo in a credential-starved ephemeral `git worktree`,
  calls official Anthropic `messages.create` with **no `tools`**, emits
  a non-authoritative `ENGINE_LLM_TRIAGE_PROPOSAL`, and produces only a
  **draft, human-merge-only PR** (an additive, mechanism-free
  `DISPOSITION_POLICIES` binding pointing an **existing**
  `EngineEscalationDisposition` verb at the novel pattern + a dossier).
  Bright lines: it **never** feeds the Ladder, never mutates, never
  trades, never disposes, never edits the ladder/supervisor mechanism;
  it is NOT a safety boundary (the fence is). Fenced deterministically
  by the credential-starved label-gated `engine-llm-triage-fence` CI
  job (provenance + hard-denied paths reused verbatim from the #187
  pure `tpcore/llm_data_triage/{fence,canary}` — one fence object, no
  twin) + two-human review + inert-until-merged + post-merge
  canary/shadow. The LLM is **never** the detector: Phase 0 closed the
  engine-daemon platform-service blind spot **deterministically** (a
  small `ops/engine_service` emitter for co-hosted-task crash-loop /
  swallowed-digest failure, see R1) so the LLM only triages what the
  deterministic layer already escalated — "deterministic agents stay
  deterministic", the LLM sits strictly atop the fail-closed Ladder.
  Placement is **B1**: a second crash-isolated `_run_supervised`
  co-task inside the existing process-isolated advisory daemon
  `ops/llm_triage_service.py` (NOT in the live-trading `engine_service`,
  NOT a 5th daemon — the installer/launchd label/4-token whitelist are
  unchanged, the two-daemon topology invariant holds by construction).
  **#243 Phase 1 (BUILT 2026-05-18)** extended R1 with two additional
  engine-daemon *silent-absence* escalate-only classes that escalate
  deterministically from `engine_service._main_loop` (the long-lived
  60s poll — the correct home; `engine_supervisor.supervise()` only runs
  inside the sweep subprocess and would be dead code for a
  "sweep never ran" detector): `engine_service_sweep_silent` (a
  qualifying trigger landed but no sweep ran within `SWEEP_SILENT_SEC`)
  and `engine_service_digest_stalled` (ISO-week rollover passed by more
  than `DIGEST_STALE_SEC` with no successful digest completion). Both
  are `STRUCTURAL`, escalate-only (no `ENGINE_HELD`), added to
  `PLATFORM_SERVICE_FAILURE_CLASSES` + `DISPOSITION_POLICIES` in the
  same Phase-1 PR (R2 clockwork enforced). Trade-monitor-silent
  (the third originally-deferred case) is **resolved-by-existing-coverage**
  — not a new detector (the `tpcore/trade_monitor` `daemon_heartbeats`
  substrate + dashboard `trade_monitor_heartbeat` probe +
  `engine_service_task_crashloop` already provide the coverage).
  Spec:
  `docs/superpowers/specs/2026-05-18-engine-silent-absence-detectors-design.md`.
  Spec:
  `docs/superpowers/specs/2026-05-18-engine-llm-triage-advisory-layer-design.md`;
  persona `docs/engine_llm_triage_persona.md`; settings runbook
  `docs/llm_data_triage_operator_runbook.md` (shared, both lanes).

## Operator workflow

`python -m ops.engine_ladder list` → triage the Sprint Dossier / logs
→ apply the fix (or remove the engine) → `python -m ops.engine_ladder
disposition <hold_id> <converted|structural|removed> "<note>"`.

## Disposition vocabulary

`converted` · `structural` · `removed` (no `auto_converted` — the
engine lane has no auto-conversion actor; R3 surfaces, it does not
auto-apply fixes).
