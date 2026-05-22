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
- **R5 — REMOVED 2026-05-22.** Operator directive ("we aren't going to
  use the llm triage... take it out") deleted the entire LLM-triage
  stack — `ops/engine_llm_triage.py`, `ops/llm_data_triage.py`,
  `ops/llm_data_recovery.py`, `tpcore/engine_llm_triage/`,
  `tpcore/llm_data_triage/`, the two triage personas, the CI fence
  script + label guard, the two triage slash skills. The deterministic
  Ladder (R1-R4) is the COMPLETE escalation surface; there is NO LLM
  backstop. An open + undispositioned + past-grace engine escalation
  STAYS open for the operator to disposition manually — no LLM proposal,
  no draft PR. **#243 Phase 1 (BUILT 2026-05-18)** extended R1 with two additional
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
  Specs:
  `docs/superpowers/specs/2026-05-18-engine-silent-absence-detectors-design.md`
  (#243 detectors). The Epic E LLM-triage spec is now historical only —
  the entire stack was removed 2026-05-22.

## Operator workflow

`python -m ops.engine_ladder list` → triage the Sprint Dossier / logs
→ apply the fix (or remove the engine) → `python -m ops.engine_ladder
disposition <hold_id> <converted|structural|removed> "<note>"`.

## Disposition vocabulary

`converted` · `structural` · `removed` (no `auto_converted` — the
engine lane has no auto-conversion actor; R3 surfaces, it does not
auto-apply fixes).
