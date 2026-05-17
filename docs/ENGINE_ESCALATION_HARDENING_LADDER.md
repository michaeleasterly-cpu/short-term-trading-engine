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
  design.
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
  literally true for the no-hold case). `python -m ops.engine_ladder
  disposition <hold_id> <converted|structural|removed> [note]` records
  an event-sourced `ENGINE_ESCALATION_DISPOSITIONED`.
- **R4 — structural removal levers (existing, no new code):** the
  `removed` disposition is physically realized via
  `RiskGovernor.emergency_kill`/kill-switch, the DSR/credibility
  graduation gate, or `archive/<engine>/EULOGY.md` + the Engine SDLC
  snap-out.
- **R5 — LLM/agentic triage:** OUT of scope (Epic E).

## Operator workflow

`python -m ops.engine_ladder list` → triage the Sprint Dossier / logs
→ apply the fix (or remove the engine) → `python -m ops.engine_ladder
disposition <hold_id> <converted|structural|removed> "<note>"`.

## Disposition vocabulary

`converted` · `structural` · `removed` (no `auto_converted` — the
engine lane has no auto-conversion actor; R3 surfaces, it does not
auto-apply fixes).
