# Engine-Lane Escalation & Hardening Ladder — Design

**Status:** approved 2026-05-18 (operator: engine-native surface,
build-break+surfacing teeth, Approach A; design settled — expert
hardening pass replaces the per-section approval gate per the
operator's standing "stop over-asking, use the expert" directive).
Next sub-project of the Deterministic Agents epic after canary (#46),
before DA-3. Engine-lane ONLY; symmetry-references the data side's
shipped ladder (`tpcore/ladder/disposition.py`,
`docs/ESCALATION_HARDENING_LADDER.md`, `ops/weekly_digest`
undispositioned section) — NOT a clone, lane-separate.

## 1. Problem

DA-1 (`ops/engine_supervisor._emit_escalated`) and DA-2
(`ops/aar_autotune._emit_escalated`) emit `ENGINE_ESCALATED` when a
bounded deterministic agent cannot resolve a failure. **Grep confirms
ENGINE_ESCALATED has ZERO consumers** — not surfaced, not
dispositioned, not tracked to closure. That is exactly the "silent
best-effort" the hardening-ladder principle forbids: an escalation
must terminate in a recorded disposition (a new bounded capability, a
structural fix, or removal), never vanish into the log.

## 2. Mandate

Close the gap, engine-native, with two teeth (operator decision —
nothing more; the escalated engine is ALREADY stood down by
`ENGINE_HELD`+`should_fire`, R1, pre-existing):
- **R2 build-break:** every engine escalation *class* must carry a
  recorded disposition policy; a new class fails CI until one is
  recorded (the clockwork forcing-function, symmetry with
  `selfheal.registry_drift`).
- **R3 loud surfacing:** every undispositioned *instance* past a grace
  window surfaces in an engine-native digest with a disposition CLI.
No extra automatic trade consequence (the engine is already held;
adding platform-wide live-clear withholding would over-reach on a
paper-only, already-gated engine).

## 3. R2 — disposition registry + clockwork drift

New `ops/engine_ladder.py` (ops-layer: may read DA-1/DA-2 ops-side
constants + tpcore — no `tpcore→ops` violation; the data ladder's
registry is `tpcore/ladder` only because its classes are tpcore-side;
engine escalation classes are ops-side, so the registry is ops-side —
this is the engine-native adaptation, not a deviation).

- `EngineEscalationDisposition(StrEnum)`: `CONVERTED` ("a new bounded
  deterministic capability — e.g. a new DA-1 detector/healer pair or
  DA-2 trigger class"), `STRUCTURAL` ("a structural fix to DA-1/DA-2/
  engine_profile logic or config"), `REMOVED` ("engine de-escalated
  from live capital — archived / kill-switched / graduation-gated
  out"). No `AUTO_CONVERTED`: the engine lane has no auto-conversion
  actor (R3 is a forcing function, not an actor — no code
  auto-applies a fix).
- `DispositionPolicy` (pydantic v2, `extra="forbid", frozen=True`):
  `class_name: str`, `default: EngineEscalationDisposition`,
  `rationale: str`.
- `INFRA_FAILURE_CLASSES` — the DA-1 touch (expert-hardened): a
  behavior-preserving extract in `ops/engine_supervisor.py` that makes
  it the **single enforced SoT**, not just one of two parallel
  literals. Today the seam-guard tuple (`engine_supervisor.py:152-154`)
  and `_classify`'s five inline `return "…"` strings are TWO
  independent literals that merely happen to agree — a new DA-1 class
  is naturally added via a new `_detect_*`+`_classify` return, and the
  seam-guard tuple (hence any constant extracted only from it) would
  NOT change → the clockwork test would stay green → R2 defeated for
  the most likely way a class is added. Therefore:
  - Introduce `INFRA_FAILURE_CLASSES: frozenset[str] =
    frozenset({"crashed_startup","scheduler_crash",
    "data_request_timeout","data_repair_escalated","missed_cycle"})`.
  - `_auto_clear`'s guard references the constant instead of its inline
    literal — **byte-identical behavior** (the DA-1 supervisor suite
    is the equivalence oracle, exactly like C-T1/CAN-T2 extracts).
  - Pin `_classify`'s emittable set to the constant: EITHER refactor
    `_classify` to iterate a `(detector, class_name)` table whose
    `class_name`s are exactly `INFRA_FAILURE_CLASSES` (preferred —
    one SoT by construction), OR add a clockwork assertion that the
    set of every string `_classify` can `return` ⊆/==
    `INFRA_FAILURE_CLASSES`. The plan picks the table refactor if it
    is behavior-preserving and small; else the assertion. Either way
    a new `_classify` class without a constant+policy update **fails
    the build**.
- `KNOWN_ESCALATION_CLASSES: frozenset[str]` =
  `engine_supervisor.INFRA_FAILURE_CLASSES | {aar_autotune._BEHAVIORAL}`
  (the real, current emitted-class union — derived, not hardcoded).
- `DISPOSITION_POLICIES: dict[str, DispositionPolicy]` — one entry per
  known class. Initial policies (the expert hardening pass may refine
  the defaults/rationales; the SHAPE is fixed):
  - `crashed_startup → STRUCTURAL` "DA-1 bounded re-invoke exhausted;
    persistent ⇒ a structural scheduler/runtime fix."
  - `scheduler_crash → STRUCTURAL` "non-zero scheduler exit survived
    self-heal ⇒ a code/runtime defect to fix structurally."
  - `data_request_timeout → STRUCTURAL` "data lane never answered the
    request in-window ⇒ the structural fix is typically in the DATA
    LANE's request fulfillment/timeout, NOT this engine; disposition
    records the operator confirmed cross-lane ownership."
  - `data_repair_escalated → STRUCTURAL` (expert-corrected from
    REMOVED — `REMOVED` here means archive/kill/graduation-gate-out,
    which is actively wrong for a transient cross-lane data outage)
    "the DATA-LANE escalation owns the fix; this engine is HELD (not
    removed) and AUTO-CLEARS on DATA_REPAIR_COMPLETE green via DA-1
    `_auto_clear`'s `_repair_complete_green_after` path; disposition
    records the operator confirmed cross-lane ownership; escalate to
    `REMOVED` ONLY if the data source is permanently retired."
  - `missed_cycle → STRUCTURAL` "engine silently failed to start over
    N cycles ⇒ a structural scheduling/dispatch fix."
  - `behavioral → STRUCTURAL` "DA-2 loss_cluster≥5 / drawdown ⇒
    edge-decay; a structural strategy review, or `REMOVED` if the
    edge is gone (snap-out via the Engine SDLC)."
- `escalation_drift() -> tuple[set[str], set[str]]`: takes **no
  argument** and derives `KNOWN_ESCALATION_CLASSES` internally from
  the live constants (identical idiom to the data ladder's
  `tpcore.ladder.disposition.disposition_drift()` — the symmetry
  oracle). Returns `(missing, extra)` = (`KNOWN` not in
  `DISPOSITION_POLICIES`, `DISPOSITION_POLICIES` keys not in `KNOWN`).
  `policy_for(class_name) -> DispositionPolicy | None`.
- Clockwork test (`scripts/tests/test_engine_ladder.py`): asserts
  `escalation_drift() == (set(), set())`. **No tautological
  `KNOWN == INFRA_FAILURE_CLASSES | {_BEHAVIORAL}` identity assertion**
  (that is `X == X`, asserts nothing, and falsely reads as a tooth —
  expert-removed). The non-tautology proof (mirroring the data
  ladder's drift test): a test that, with a synthetic class present
  in the derived `KNOWN` but absent from `DISPOSITION_POLICIES`,
  asserts `escalation_drift()` returns non-empty `missing` — proving
  the build genuinely breaks. Because `KNOWN` is DERIVED from the
  pinned constants (incl. `_classify` per the SoT pinning above),
  adding a real DA-1/DA-2 class grows `KNOWN` → `missing` non-empty →
  **build breaks until a `DISPOSITION_POLICIES` entry is added**.
  This — not the deleted identity assert — is the R2 tooth.

## 4. R3 — undispositioned-instance surface + disposition verb

`ops/engine_ladder.py` CLI (`python -m ops.engine_ladder ...`),
event-sourced over `platform.application_log` (mirrors the
`current_hold` / `_open_request_state` read idiom; mirrors DA-1/DA-2
`_emit` for writes):

**Two escalation shapes (expert-hardened — the make-or-break
correctness point):** every `ENGINE_ESCALATED` carries a `hold_id`
(uuid4), but there are TWO shapes with DIFFERENT terminals:
  - **held-class** (all DA-1 classes — DA-1 always escalates+holds;
    and DA-2 hold-eligible: `loss_cluster≥5`/`drawdown`): the SAME
    `hold_id` is on a paired `ENGINE_HELD`; terminal = a later
    `ENGINE_CLEARED` for that `hold_id` OR an
    `ENGINE_ESCALATION_DISPOSITIONED` for it. The engine is gated off
    by `should_fire` while held (R1).
  - **escalate-only** (DA-2 noise: `outlier_loss` / short
    `loss_cluster`, `aar_autotune.py:156` — emits `ENGINE_ESCALATED`
    with a fresh uuid4 `hold_id`, **NO `ENGINE_HELD`, and NOTHING
    ever emits `ENGINE_CLEARED` for it**; the engine KEEPS TRADING by
    design — it's noise, not decay). Without special handling this
    would surface forever (no possible `ENGINE_CLEARED`) AND never be
    tracked-to-closure. Terminal for escalate-only = an
    `ENGINE_ESCALATION_DISPOSITIONED` for its `hold_id` **OR** every
    `triggers` fingerprint in its payload is no longer open in
    `platform.forensics_triggers` (i.e. all operator-resolved /
    absent from DA-2's `_open_triggers` view — the honest "the noise
    cleared" auto-close, mirroring DA-2's own `_maybe_clear_behavioral`
    re-evaluation idiom). An escalate-only escalation IS dispositionable
    and IS tracked; it is NOT a hold.

- `list` (default) — the undispositioned digest. An instance
  (keyed by `hold_id`) is **OPEN-UNDISPOSITIONED** iff: an
  `ENGINE_ESCALATED` with that `hold_id` exists, `recorded_at` older
  than `_GRACE_DAYS` (default 7, `ENGINE_LADDER_GRACE_DAYS`
  env-overridable — data-ladder symmetry), NO
  `ENGINE_ESCALATION_DISPOSITIONED` for that `hold_id`, AND **either**
  (held-class) no later `ENGINE_CLEARED` for that `hold_id`, **or**
  (escalate-only: that `hold_id` has no paired `ENGINE_HELD`) at least
  one of its payload `triggers` fingerprints is still open in
  `forensics_triggers`. Prints per instance: engine, shape
  (held / escalate-only), failure_class, reason, age, and the class's
  `policy_for(...)` default+rationale. Header states the rung-3
  principle ("each MUST be converted | structural | removed"),
  symmetry with the data digest's section title.
- `disposition <hold_id> <converted|structural|removed> [note]` —
  validates the verb is a valid `EngineEscalationDisposition`
  (case-insensitive accepted, stored lowercase); validates the
  `hold_id` corresponds to a real `ENGINE_ESCALATED` that is still
  OPEN-UNDISPOSITIONED per the predicate above — **for either shape**
  (it must accept an escalate-only `hold_id`, i.e. one with NO paired
  `ENGINE_HELD`; validity MUST NOT be gated on `current_hold`).
  Unknown `hold_id` / not-open / unknown verb → nonzero exit +
  message, NO write. On success emits event-sourced
  `ENGINE_ESCALATION_DISPOSITIONED {schema:1, hold_id, disposition,
  note}` to `platform.application_log` via the locked INSERT
  (`(engine, run_id, event_type, severity, message, data)` —
  `engine` = the escalated engine, `severity="INFO"`). Re-dispositioning
  emits a new event; the digest excludes any `hold_id` with ≥1
  disposition event (presence-excludes; latest-wins not needed).
- No daemon; no auto-trade action. The escalated engine is already
  `ENGINE_HELD` (R1). Dashboard panel = explicit out-of-scope
  follow-up (YAGNI; CLI/digest is the deliverable).
- `__main__` entrypoint (argparse: subcommand `list` | `disposition`;
  `--grace-days` optional) + `def main()` + `if __name__ ==
  "__main__":` — so `python -m ops.engine_ladder` is genuinely
  invocable (the canary `__main__`-no-op lesson: a CLI that can't be
  run as `-m` is a silent no-op; a test asserts the `__main__` guard
  exists and `list` runs DB-lessly to a clean exit/empty result).

## 5. R4 — canonical doc (doc-only, no code)

`docs/ENGINE_ESCALATION_HARDENING_LADDER.md` — the engine-lane
parallel to `docs/ESCALATION_HARDENING_LADDER.md` (read that for
symmetry-of-shape; do NOT clone its data specifics). Sections: the
principle (every engine escalation terminates in
converted|structural|removed — never silent best-effort, never
indefinite hold without recorded disposition); the rungs (R1 = the
existing `ENGINE_ESCALATED`+`ENGINE_HELD`+`should_fire` fail-closed
gate; R2 = the clockwork drift forcing-function; R3 = the
`ops.engine_ladder` digest + disposition verb; R4 = the EXISTING
removal levers — `RiskGovernor` kill-switch, the DSR/credibility
graduation gate, `archive/<engine>/EULOGY.md` + Engine SDLC snap-out —
enumerated as how the `removed` disposition is physically realized,
NO new code; R5 = LLM/agentic triage, OUT, Epic E); the disposition
vocabulary; the operator workflow (`python -m ops.engine_ladder list`
→ triage the dossier/logs → `disposition <hold_id> <verb> [note]`);
one sentence stating escalate-only (no-hold) escalations close on
EITHER disposition OR resolution of all their trigger fingerprints
(so the doc's "every escalation terminates" claim is literally true
for the no-hold case); and an explicit "symmetry-references the
data-lane ladder; engine-native; lane-separate; not a clone" note. Plus a one-line CLAUDE.md
engine-lane-escalation-contract bullet, parallel to the existing
data-lane-escalation-contract bullet — staged with `git diff`/`git
add -p` guarding ONLY the new bullet (the data session edits CLAUDE.md
concurrently; never stage their hunks).

## 6. Composition / lane discipline

Consumes DA-1/DA-2 escalation EVENTS read-only. The ONLY DA-1/DA-2
source touch is the §3 behavior-preserving `INFRA_FAILURE_CLASSES`
constant extract in `engine_supervisor.py` (inline tuple → named
frozenset constant; `_auto_clear` guard + `_classify` behavior
byte-identical; the DA-1 supervisor suite is the equivalence oracle,
exactly like prior C-T1/CAN-T2 extracts). Does NOT touch:
`tpcore/ladder/`, `ops/weekly_digest.py`, `ops/data_repair_service.py`,
`ops/cutover_agent.py`, the data lane, DA-1/DA-2 detection/clear
logic, or alpha engines. `ops/engine_ladder.py` is ops-layer; the
clockwork + CLI tests live in `scripts/tests/test_engine_ladder.py`
with the ops-name-collision guard header identical to
`scripts/tests/test_engine_supervisor.py`. Adds NO daemon (consistent
with the DA-3 two-daemon target).

## 7. Error handling / determinism

Deterministic + bounded: grace-windowed event-sourced reads (no
unbounded scans), exactly one event emit per `disposition` call, no
loops/retries/daemon. The CLI exits non-zero with a clear message on
bad input (unknown verb / unknown-or-not-open hold_id) and performs
NO partial write. Re-running `list` is read-only and idempotent.

## 8. Testing

Unit (fake pool, no DB; ops-name-collision guard like sibling
scripts/tests):
- `escalation_drift()` (no args; derives KNOWN internally) ==
  `(set(), set())` in lockstep; returns `missing` non-empty when a
  known class lacks a policy.
- Non-tautology proof (NO `KNOWN == INFRA|{_BEHAVIORAL}` identity
  assert — deleted as vacuous): with a synthetic class present in the
  derived KNOWN but absent from `DISPOSITION_POLICIES`,
  `escalation_drift()` returns non-empty `missing` (build would
  break) — symmetric with the data ladder's `disposition_drift` test
  (name it as the oracle).
- `_classify` SoT pin: a test asserting the set of every string
  `_classify` can `return` ⊆ `engine_supervisor.INFRA_FAILURE_CLASSES`
  (so a new `_classify` class without a constant+policy update fails
  CI) — OR, if the `(detector, class_name)`-table refactor is used,
  a test that the table's `class_name`s == `INFRA_FAILURE_CLASSES`.
- `policy_for` returns the right policy / None for unknown.
- `list` (held-class): includes a past-grace open undispositioned
  held escalation; EXCLUDES within-grace; EXCLUDES one with a later
  `ENGINE_CLEARED`; EXCLUDES one with an
  `ENGINE_ESCALATION_DISPOSITIONED`.
- `list` (escalate-only, no `ENGINE_HELD`): INCLUDES a past-grace one
  whose trigger fingerprints are STILL open; EXCLUDES one whose
  fingerprints are ALL resolved/absent from `forensics_triggers`
  EVEN BEFORE grace expiry (the auto-close disjunct, symmetric with
  the data digest's resolving-terminal exclusion); EXCLUDES one with
  an `ENGINE_ESCALATION_DISPOSITIONED`.
- `disposition`: emits the locked `ENGINE_ESCALATION_DISPOSITIONED`
  payload (schema:1, hold_id, disposition lowercased, note) for a
  valid verb on BOTH a held-class AND an escalate-only `hold_id`
  (validity NOT gated on `current_hold`); nonzero exit + NO write for
  an unknown verb and for an unknown/not-open hold_id.
- `__main__`/entrypoint present and `list` reaches a clean DB-less
  exit (canary `-m`-no-op regression guard).
- Constant-extract behavior-preserving: full
  `scripts/tests/test_engine_supervisor.py` green (DA-1 oracle).
- Full suite + CI-exact `ruff` (incl. `ops/`) + `check_imports` green;
  data-lane/`weekly_digest`/`tpcore/ladder`/DA-1-2-logic untouched
  (asserted via `git diff` scope in the finish task).

## 9. Scope boundary

Delivers: `ops/engine_ladder.py` (enum + `DispositionPolicy` +
`DISPOSITION_POLICIES` + `escalation_drift`/`policy_for` +
`list`/`disposition` CLI + `__main__`), the `INFRA_FAILURE_CLASSES`
extract in `ops/engine_supervisor.py`,
`docs/ENGINE_ESCALATION_HARDENING_LADDER.md`, the CLAUDE.md bullet,
`scripts/tests/test_engine_ladder.py` (incl. the clockwork drift
test). Does NOT: add teeth beyond build-break + surfacing; touch
data-lane / `weekly_digest` / `tpcore/ladder`; change DA-1/DA-2
detection/clear logic (only the constant extract); build a dashboard
panel; do DA-3. Acceptance: a new DA-1/DA-2 escalation class fails CI
until a policy is recorded; undispositioned instances past grace
surface via `python -m ops.engine_ladder list`; `disposition` records
an event-sourced terminal; the doc + CLAUDE.md bullet exist; full
suite + ruff + check_imports green; lane discipline asserted.

## 10. Decisions log

- **D-EL-1** Engine-native surface (`ops/engine_ladder.py` CLI/digest)
  — `weekly_digest.py` / `tpcore/ladder/` NOT touched (operator).
- **D-EL-2** Teeth = R2 build-break drift + R3 loud surfacing; NO
  extra automatic trade consequence (engine already `ENGINE_HELD`;
  paper-only) (operator).
- **D-EL-3** Approach A — all-in `ops/engine_ladder.py`; one
  behavior-preserving `INFRA_FAILURE_CLASSES` constant extract in
  `engine_supervisor.py`; layering-safe (ops reads ops+tpcore;
  tpcore unchanged) (operator).
- **D-EL-4** Disposition verbs = `converted|structural|removed`; no
  `AUTO_CONVERTED` (no engine auto-conversion actor).
- **D-EL-5** Event-sourced `ENGINE_ESCALATION_DISPOSITIONED` on
  `platform.application_log`; `hold_id` correlation; 7-day grace
  (`ENGINE_LADDER_GRACE_DAYS` overridable) — symmetry with data.
- **D-EL-6** Symmetry-references the data ladder shape; engine-native;
  lane-separate; not a clone. DA-3 is the next sub-project after.
- **D-EL-7** A `__main__`/`-m`-invocable entrypoint with a test
  (canary `__main__`-no-op regression lesson).
- **D-EL-8** (expert hardening, 2026-05-18) DA-2 **escalate-only**
  escalations (no `ENGINE_HELD`, no possible `ENGINE_CLEARED`, engine
  keeps trading) are explicitly in-scope: a separate digest shape;
  terminal = DISPOSITIONED OR all payload `triggers` fingerprints
  resolved/absent from `forensics_triggers`; `disposition` accepts
  their `hold_id` (NOT gated on `current_hold`). Without this they'd
  surface forever / slip untracked.
- **D-EL-9** (expert hardening) `INFRA_FAILURE_CLASSES` must be the
  ENFORCED SoT — `_classify`'s emittable set is pinned to it (table
  refactor or a coverage assertion), not just the `_auto_clear`
  seam-guard literal, else R2 is vacuous for the common
  add-a-class path. The tautological `KNOWN == INFRA|{_BEHAVIORAL}`
  assertion is deleted. `data_repair_escalated` default disposition
  corrected `REMOVED → STRUCTURAL` (cross-lane owned; engine
  auto-clears on data-green).
