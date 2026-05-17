# Audit-Driven Referential Remediation (`tpcore/auditheal`) — Design

**Status:** spec 2026-05-17 (DATA lane). Brainstorm → **spec (this
doc)** → plan → phased build. First sub-agent of the #186 "remaining
deterministic data agents" epic (operator-selected: candidate (5)).

Operator decisions captured (brainstorm, 2026-05-17):
- Placement: *"close the loop in the existing Step 4c audit"* (no new
  daemon; symmetric to Step-4 self-heal).
- Remediable boundary: *"strictly the proven `cross_ref_cleanup`
  class; all else escalate-only"*.

## 1. Problem

Self-heal (#185 + `tpcore/selfheal`) closed the **detect→act** loop on
the *validation* layer: a red `validation.%` row is mapped to a bounded
canonical repair, re-validated, and escalated honestly if unhealable.

The **cross-feed referential** layer has no such loop. The unattended
Step-4c deep audit (`scripts/audit_data_pipeline.py`) *detects*
referential violations (known_knowns, persisted to
`platform.data_quality_log` under
`source='data_pipeline_audit.known_knowns.<check>…'`, `severity=FAIL`)
and **hard-stops** the cycle (alarm + no `DATA_OPERATIONS_COMPLETE`),
but it never *acts* — even when the violation has an already-proven,
bounded, idempotent remediation (`cross_ref_cleanup`: delete expired /
orphan `tradier_options_chains` rows). The operator must hand-run the
cleanup, then the next cycle clears. That is the exact "detect but
don't act" gap #185 removed for validation, still open on the
referential layer.

## 2. Design — close the loop in Step 4c, symmetric to self-heal

A new generic capability `tpcore/auditheal/`, structurally 1:1 with
`tpcore/selfheal/` (proven shape, no new moving parts):

| `tpcore/selfheal` (validation layer) | `tpcore/auditheal` (referential layer) |
|---|---|
| `spec.py` `HealSpec` (pydantic, frozen) | `spec.py` `RemediationSpec` (pydantic, frozen) |
| `registry.py` `HEAL_SPECS` + drift test == `suite.KNOWN_CHECK_NAMES` | `registry.py` `REMEDIATION_SPECS` + drift test == the audit's known_knowns check set |
| `runner.py` `make_canonical_runner` | **reused as-is** (no new runner) |
| `orchestrator.py` `run_self_heal` | `orchestrator.py` `run_audit_heal` |
| `__main__.py` exit `0`=green / `1`=escalate | `__main__.py` same exit contract |
| detector: red `validation.%` rows | detector: `data_pipeline_audit.known_knowns.%` rows with `severity='FAIL'` |
| canonical repair: `HealSpec.stage` | canonical remediation: `RemediationSpec.stage` (= `cross_ref_cleanup`) |
| re-check: `data_validation` stage | re-check: re-run the known_knowns audit (form decided in plan — see §7) |

`run_audit_heal(pool, run_stage, run_audit, *, max_iterations)`:

1. Run the known_knowns audit (`run_audit`) so `data_quality_log` has
   the freshest findings.
2. Read the red set: latest
   `source LIKE 'data_pipeline_audit.known_knowns.%'` rows with
   `severity='FAIL'` (mirrors the orchestrator's `_RED_SQL`).
3. Map each red audit check → its `RemediationSpec` (registry SoT).
   - `remediable=True` → run the bounded canonical stage
     (`run_stage(spec.stage, spec.params)` via the existing canonical
     runner = `ops.py --stage cross_ref_cleanup`).
   - `remediable=False` → **escalate** (honest reason; never act).
   - no spec → **escalate** "unknown referential red".
4. Re-run the audit; bounded retry up to `max_iterations`.
5. Exit `0` iff known_knowns is 100% green (after 0+ remediations);
   else exit `1` (escalate).

The orchestrator is **generic**: all per-check policy lives in the
declarative registry, exactly as `tpcore/selfheal` keeps it in
`HEAL_SPECS`. Deterministic, no LLM.

## 3. The remediable boundary (launch scope)

`REMEDIATION_SPECS` is keyed by the known_knowns audit check identity
the audit emits (the `check_name` component of the
`data_pipeline_audit.known_knowns.<check>` source).

- **Auto-remediable at launch: strictly the `cross_ref_cleanup`
  class.** That stage is additive-only delete rules, idempotent, and
  already proven-safe (its docstring is the contract). The agent runs
  *that exact canonical stage* — it never inlines a bespoke fix.
- **Everything else is escalate-only** with an explicit
  `escalate_reason` (the `healable=False` analog): `row_count`,
  `freshness`, `ingestion_jobs`, `sentinel_basket`,
  `credit_spread_history`, `shrinkage_detector`, and **`validation_status`**.
  `validation_status` is escalate-only *by design* — it is
  `tpcore/selfheal`'s domain; auditheal must not double-act on it.
- **Drift-guarded** (clockwork, the `registry_drift` pattern): a
  registry-coverage test asserts `set(REMEDIATION_SPECS)` ==
  the set of known_knowns check names the audit can emit. A new audit
  check **fails the build** until a remediate-or-escalate decision is
  recorded — the remediation policy can never silently lag the audit.

The exact known_knowns check-name set is enumerated from
`scripts/audit_data_pipeline.py` (`run_known_knowns`) during P1 and
pinned by the drift test; this spec does not freeze a list that would
rot.

## 4. Safety invariants (load-bearing — non-negotiable)

- **Never suppress a red.** The agent can only flip a known_knowns
  red→green by running the *proven* bounded remediation, after which a
  **mandatory re-audit** must independently confirm green. The
  detector and the post-remediation re-check are the *same* audit
  code path — they cannot disagree on "is it clean".
- **The hard-stop floor is strictly preserved.** Still red after
  `max_iterations` (or any escalate-only red) → escalation → wrapper
  does **not** emit `DATA_OPERATIONS_COMPLETE`, engines do not trade —
  *exactly* Step 4c's behaviour today. The agent only removes
  hard-stops that were auto-fixable by an already-proven action; it
  never weakens the gate.
- **Bounded + idempotent.** `max_iterations` cap (default symmetric
  with self-heal); `cross_ref_cleanup` is idempotent by construction
  (same query, deletes shrink to zero next run), so a re-run is safe.
- **Pooler-contention lock.** Runs inside the same
  `${TMPDIR:-/tmp}/ste-data-operations.lock` Step-4/4c already hold
  (it executes via the canonical `ops.py --stage` runner during the
  data-ops cycle) — no concurrent `cross_ref_cleanup`.
- **Deterministic, no LLM.** Like every data agent.
- **No double-act with self-heal.** `validation_status` is
  escalate-only in `REMEDIATION_SPECS`; the two loops have disjoint
  remediation domains.

## 5. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `tpcore/auditheal/`: `RemediationSpec` + `REMEDIATION_SPECS` registry + drift-coverage test (== audit known_knowns set) + `run_audit_heal` orchestrator (generic; `run_stage` + `run_audit` injected) + `__main__` thin caller (exit 0/1 contract). Reuses `tpcore/selfheal/runner.make_canonical_runner`. **Landed dark** (not wired into the cycle). Deterministic fake-pool / fake-runner / fake-audit unit tests, mirroring `test_selfheal.py`. |
| 2 | Wire `run_data_operations.sh` Step 4c to call `python -m tpcore.auditheal` instead of raw `scripts/audit_data_pipeline.py`. The thin caller runs the audit, closes the loop, and exits with the **same** semantics (`1` ⇒ alarm + hard stop + no emit; `0` ⇒ proceed). Net behaviour change: a `cross_ref_cleanup`-class red is now auto-remediated + re-audited instead of always hard-stopping. |
| 3 | Documentation reconciliation: CLAUDE.md Step-4c description (now a detect→remediate→re-audit→escalate loop, symmetric to Step 4), the audit/data-adapter-pipeline docs, TODO.md #186 status. |

## 6. Non-goals

- Not removing or weakening the known_knowns hard-stop gate (it stays
  authoritative; the agent only auto-clears the proven-fixable subset).
- Not building new destructive remediations beyond the existing
  `cross_ref_cleanup` class in this spec (escalate-only otherwise;
  broadening is a future, separately-scoped increment).
- Not an LLM. Not a new daemon/process (loop closes in the existing
  Step 4c).
- Not touching `tpcore/selfheal` (disjoint domain; `validation_status`
  stays its responsibility).
- Operator interaction unchanged: this is internal data-layer
  hardening; the operator's only touchpoints remain the ADD/REMOVE
  Data Feed Change Request + the weekly digest ack. (#186 candidates
  3/4/6 are out of scope — separate specs.)

## 7. Open questions for the plan phase

- Exact `max_iterations` default (lean: match `tpcore/selfheal`'s
  `DEFAULT_MAX_ITERATIONS` for operator-model consistency).
- Whether `run_audit` is invoked in-process (import
  `run_known_knowns`) or via the canonical subprocess
  (`audit_data_pipeline.py --phase known_knowns`); lean toward the
  subprocess path so the re-check is byte-identical to the unattended
  Step-4c audit (no second code path to drift) — decide in the plan
  with the blast-radius lens.
- Confirm the precise `data_quality_log` `source` string format the
  audit writes for known_knowns findings (with/without a trailing
  `.<source>` component) so the detector SQL matches exactly — read
  from `audit_data_pipeline.py`, do not assume.
