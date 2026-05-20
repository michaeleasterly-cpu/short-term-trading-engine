# Engine Change Request — the operator's single structured touchpoint

This is **the** way to add, remove, or re-tune an engine. You do **not**
hand-edit `tpcore.engine_profile._PROFILE`, the smoke loop, `pyproject`,
the dispatch-order frozen literal, or an EULOGY — that is exactly how the
system gets broken (the Sigma 22-site drift, PR #170, proved it). You
fill in the block below and feed it in; the system parses it, routes it
through the deterministic lifecycle gates, **prepares and validates the
exact diff**, and hands you back either a binary **APPROVE? (y/n)** on a
proven-consistent diff (ADD / REMOVE), or — for the automated gated
operations (MODIFY / LAB→PAPER promote) — a done-receipt with the
`ENGINE_CHANGE_REQUEST` audit reference.

> **Operator-interaction policy (authoritative — spec §6).** You approve
> **only** two things: **ADD** an engine (new scaffold or Lab-graduated)
> and **REMOVE** one (retire/archive). Everything reversible and
> gate-verified — a MODIFY (re-tuned params that already passed
> DSR≥0.95 ∧ credibility≥60) and a LAB→PAPER promotion the capital gate
> already cleared — is **automated, deterministic, no operator approval**.
> A request that cannot produce a consistent diff is **rejected with the
> exact reason — never handed to you to force**.

## The request block (copy, fill, feed in)

```
ECR
action:        ADD | REMOVE | MODIFY        # exactly one
engine:        <engine name>                # _PROFILE key vocabulary
# ── ADD only (onboard / graduate) ─────────────────────────────────
source:        new_scaffold | lab_candidate | existing_code
                                            # new_scaffold: copy from tpcore/templates/engine_template/
                                            # lab_candidate: Lab-graduated (dossier-gated)
                                            # existing_code: register engine code shipped via a
                                            #   separate PR — the SP-F → catalyst pattern.
                                            #   Engine dir MUST already exist on disk.
lab_dossier:   <path under docs/lab/…>      # required iff source=lab_candidate
cadence:       daily | weekly_first_trading_day | monthly_first_trading_day
allocator:     true | false                 # allocator_eligible
dispatch_order: <int>                        # unique among non-RETIRED
gate_dsr:      <float ≥ 0.95>               # ONLY for source=lab_candidate;
                                            #   re-verified from the dossier.
                                            #   FORBIDDEN for new_scaffold/existing_code.
gate_cred:     <int ≥ 60>                   # same scoping as gate_dsr.
need:          <one line: the edge / why this engine exists>
data_dependencies: <comma-separated platform.<table> names>
                                            # the per-engine data gate's SoT — threaded
                                            # into EngineProfile.data_dependencies on
                                            # the new _PROFILE row. Source-kind-aware:
                                            #   - existing_code: REQUIRED (non-empty).
                                            #     The operator-shipped engine code already
                                            #     reads from specific platform.<table>s;
                                            #     declare them up-front or the planner
                                            #     hard-rejects (fail-closed).
                                            #   - new_scaffold: OPTIONAL. A fresh scaffold
                                            #     may have no data wiring yet; extend it
                                            #     via a later MODIFY once the engine is
                                            #     wired to data.
                                            #   - lab_candidate: INHERITABLE. Today's
                                            #     LabResult schema does not yet carry
                                            #     data_dependencies; ECR-provided value
                                            #     wins when present, empty otherwise.
                                            # Vocabulary: HealSpec source names (see
                                            # tpcore.selfheal.registry.HEAL_SPECS.source).
# ── REMOVE only (retire / archive) ────────────────────────────────
reason:        <one line: cause of death>
eulogy_notes:  <free text → seeds the EULOGY template>
# ── MODIFY only (re-tuned params on an existing engine) ───────────
lab_dossier:   <path under docs/lab/…>      # the SURVIVED fold_existing dossier
param_change:  <key>=<value>[, <key>=<value> …]
gate_dsr:      <float ≥ 0.95>
gate_cred:     <int ≥ 60>
```

`action` selects exactly one block; any field outside the selected
block is **rejected** (not ignored). All numeric gate evidence is
**re-verified by the planner against the cited Lab dossier's JSON
sidecar — never trusted from this text** (spec §5.4 / H-S3-6).

### Pre-conditions for `data_dependencies` (spec 2026-05-20 §7.1)

Each `source` kind imposes a distinct posture on the `data_dependencies`
key. The planner enforces these at ECR parse time (fail-closed —
`existing_code` without `data_dependencies` is a hard reject, never
inferred or coerced from later context):

- **`source: existing_code` → REQUIRED, non-empty.** The operator-shipped
  engine code already reads from specific `platform.<table>` rows
  (verified by the per-engine data gate). Declaring them in the ECR is
  the only way the gate can run on first dispatch; omitting them is a
  silent un-gated half-state and the planner refuses.
- **`source: new_scaffold` → OPTIONAL.** A fresh scaffold may have no
  data wiring yet. The empty default is the SoT for "no declared reads
  yet"; the operator extends it via a later MODIFY once the engine reads
  real data.
- **`source: lab_candidate` → INHERITABLE.** Today's `LabResult` schema
  does not yet carry `data_dependencies`; the ECR's value wins when
  present, otherwise the empty default applies. A future `LabResult`
  extension may carry the inherited value; the ECR override remains.

Run it: `python -m ops.engine_sdlc --ecr <path-to-this-filled-file>`
