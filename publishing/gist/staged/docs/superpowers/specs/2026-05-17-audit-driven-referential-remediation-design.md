# Audit-Driven Referential Remediation (`tpcore/auditheal`) — Design

**Status:** BUILT 2026-05-17 (DATA lane). Brainstorm → spec (this
doc, v2 — retargeted) → plan → **phased build complete** (P1–P4). First
sub-agent of the #186 "remaining deterministic data agents" epic
(operator-selected: candidate (5)).

**Build record:**
- P1 (PR #26): `tpcore/audit/cross_table.py` — structured cross-table
  audit with `CROSS_TABLE_CHECKS` SoT, `data_quality_log` persistence
  (`cross_table_audit.*` rows), 🟢/🔴 stdout roll-up preserved.
- P2 (PR #28): `tpcore/auditheal/` generic loop — `RemediationSpec`,
  `REMEDIATION_SPECS` registry (drift-guarded), `run_audit_heal`
  orchestrator. Landed dark (Step 3 not yet wired).
- P3 (PR #29): wire Step 3 + enforce gate — `run_data_operations.sh`
  Step 3 calls `python -m tpcore.auditheal`; exit 1 on any
  unremediated/escalate-only red hard-stops the cycle (no
  `DATA_OPERATIONS_COMPLETE`). Previously always exit 0 (theatre).
- P4 (this doc update): CLAUDE.md / TODO.md / spec reconciled to
  shipped reality.

**v2 correction (2026-05-17):** v1 targeted Step-4c
(`audit_data_pipeline.py` known_knowns) — but its 11 known_knowns
checks contain **no** cross-ref-remediable violation; the
`cross_ref_cleanup`-class violations live in the **Step-3** cross-table
audit (`scripts/audit_all_tables.py`). Verified, not assumed. Operator
decision: *retarget to the Step-3 cross-table audit*. v2 also folds in
a defect that verification surfaced (see §1).

Operator decisions captured (brainstorm + correction, 2026-05-17):
- Close the detect→act loop in the **existing cross-table audit**
  (no new daemon; symmetric to Step-4 self-heal).
- Remediable boundary: *strictly the proven `cross_ref_cleanup`
  class; all else escalate-only*.

## 1. Problem

Self-heal (#185 + `tpcore/selfheal`) closed the **detect→act** loop on
the *validation* layer. The **cross-table referential** layer has two
gaps, both verified in `scripts/audit_all_tables.py` /
`scripts/run_audit_all_tables.sh` / `run_data_operations.sh` Step 3:

1. **Detection theatre.** `audit_all_tables.py` runs a COUNT per check
   and prints `🟢 (n==0)` / `🔴 (n>0)`, but it (a) **never persists**
   to `platform.data_quality_log`, and (b) **`main()` unconditionally
   `return 0`** — so Step 3's `AUDIT_RC -ne 0` guard only catches a
   crash / missing DSN, **never an actual violation**. The
   CLAUDE.md/data-gate claim *"Cross-table audit must return 0
   violations across every dependent table"* is therefore **currently
   unenforced** — a 🔴 prints and the cycle proceeds.
2. **No detect→act.** Even the subset with an already-proven, bounded,
   idempotent remediation (`tradier_options_chains` expired / orphan →
   the `cross_ref_cleanup` stage) is never acted on; the operator must
   hand-run cleanup.

This spec closes BOTH: makes the cross-table audit structured +
persisted + (at P3) gate-honest, and closes the detect→act loop on the
proven-remediable subset — exactly the pattern #185 established for
validation.

## 2. Design — structured cross-table audit + an auditheal loop

A new generic capability `tpcore/auditheal/`, structurally 1:1 with
`tpcore/selfheal/` (proven shape, no new moving parts), fed by a
**structured** cross-table audit:

| `tpcore/selfheal` (validation layer) | `tpcore/auditheal` (referential layer) |
|---|---|
| detector: red `validation.%` rows | detector: red `cross_table_audit.%` rows (NEW persistence) |
| `spec.py` `HealSpec` (pydantic, frozen) | `spec.py` `RemediationSpec` (pydantic, frozen) |
| `registry.py` `HEAL_SPECS` + drift test == `suite.KNOWN_CHECK_NAMES` | `registry.py` `REMEDIATION_SPECS` + drift test == the cross-table check SoT |
| `runner.py` `make_canonical_runner` | **reused as-is** (no new runner) |
| `orchestrator.py` `run_self_heal` | `orchestrator.py` `run_audit_heal` |
| `__main__.py` exit `0`=green / `1`=escalate | `__main__.py` same exit contract |
| re-check: `data_validation` stage | re-check: re-run the structured cross-table audit |
| canonical repair: `HealSpec.stage` | canonical remediation: `RemediationSpec.stage` (= `cross_ref_cleanup`) |

**Prerequisite — make the cross-table audit structured (P1).**
Refactor `audit_all_tables.py` from inline `print`-only `q()` calls
into a declared single-source-of-truth list `CROSS_TABLE_CHECKS`: each
entry is `(table, check_name, sql, kind)` where `kind ∈
{violation_count, dump}`. `violation_count` checks run the COUNT and
**persist a row** to `platform.data_quality_log` under a stable source
key (see §7), reusing `audit_data_pipeline._persist`'s exact
convention: `n==0 → severity OK, stale=False, confidence=1.000`;
`n>0 → severity FAIL, stale=True, confidence=0.000`. The stdout
🟢/🔴 roll-up is preserved (operator-visible, unchanged). `dump`
sections (risk_state / open_orders / ingestion_jobs — informational,
not pass/fail) are NOT persisted as checks.

`run_audit_heal(pool, run_stage, run_audit, *, max_iterations)`:

1. `run_audit` (the structured cross-table audit) → fresh
   `cross_table_audit.%` rows.
2. Read the red set: latest `source LIKE 'cross_table_audit.%'`
   rows with `confidence = 0` (FAIL) — mirrors the selfheal
   orchestrator's `_RED_SQL` predicate.
3. Map each red `(table, check_name)` → its `RemediationSpec`:
   - `remediable=True` → `run_stage(spec.stage, spec.params)` via the
     existing canonical runner (`ops.py --stage cross_ref_cleanup`).
   - `remediable=False` → **escalate** (honest reason; never act).
   - no spec → **escalate** "unknown cross-table red".
4. Re-run the audit; bounded retry up to `max_iterations`.
5. Exit `0` iff cross-table is 100% green (after 0+ remediations);
   else `1` (escalate). All per-check policy lives in the declarative
   registry. Deterministic, no LLM.

## 3. The remediable boundary (launch scope)

`REMEDIATION_SPECS` is keyed by the structured audit's
`(table, check_name)` identity.

- **Auto-remediable at launch: strictly the `cross_ref_cleanup`
  class** — exactly the two `tradier_options_chains` checks that stage
  proves-safe to delete:
  - `tradier_options_chains / expiration_in_past`
  - `tradier_options_chains / orphan_no_prices`
  Both map to `RemediationSpec(stage="cross_ref_cleanup", params={})`.
  The agent runs *that exact canonical stage* — it never inlines a
  bespoke fix. (`cross_ref_cleanup` deletes precisely these two row
  classes; idempotent.)
- **Everything else is escalate-only** with an explicit
  `escalate_reason` (the `healable=False` analog): the other-table
  orphan checks (`earnings_events`, `liquidity_tiers`,
  `universe_candidates`, `corporate_actions`,
  `fundamentals_quarterly` "ticker not in prices_daily") and all
  integrity checks (NULL ticker, negative spreads, future dates,
  etc.). Deleting those is NOT proven-safe — e.g. an `earnings_events`
  row for a ticker transiently absent from `prices_daily` must not be
  auto-deleted. Honest escalation, not a silent gap.
- **Drift-guarded** (clockwork, the `registry_drift` pattern): a
  registry-coverage test asserts `set(REMEDIATION_SPECS)` == the set
  of `violation_count` `(table, check_name)` keys in
  `CROSS_TABLE_CHECKS`. A new cross-table check **fails the build**
  until a remediate-or-escalate decision is recorded — the remediation
  policy can never silently lag the audit.

## 4. Safety invariants (load-bearing — non-negotiable)

- **Never suppress a red.** The agent flips a cross-table red→green
  ONLY by running the *proven* `cross_ref_cleanup` stage, after which
  a **mandatory re-audit** (same audit code path) must independently
  confirm green.
- **Strengthens, never weakens, the gate.** Today the cross-table
  audit is non-enforcing (always exit 0). After P3 the thin caller's
  exit code is honest: still-red after `max_iterations` (or any
  escalate-only red) → exit `1` → Step 3 hard-stops (its wrapper
  already `exit $AUDIT_RC` on non-zero) → no `DATA_OPERATIONS_COMPLETE`
  downstream. This is a net *tightening* of the data gate (a real
  latent bug fixed), plus auto-clearing of the proven-fixable subset.
- **Bounded + idempotent.** `max_iterations` cap (default symmetric
  with self-heal); `cross_ref_cleanup` is idempotent by construction.
- **Pooler-contention lock.** Runs inside the same
  `${TMPDIR:-/tmp}/ste-data-operations.lock` the data-ops cycle holds
  (executes via the canonical `ops.py --stage` runner during Step 3).
- **Deterministic, no LLM.**
- **No double-act with self-heal.** Disjoint detector namespaces:
  `cross_table_audit.%` (auditheal) vs `validation.%` (selfheal). No
  cross-table check overlaps a validation check.

## 5. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | Refactor `scripts/audit_all_tables.py`: declare `CROSS_TABLE_CHECKS` SoT (`(table, check_name, sql, kind)`); `violation_count` checks **persist** to `data_quality_log` via the `audit_data_pipeline._persist` convention (stdout roll-up preserved; `main()` still `return 0` — Step-3 wrapper behaviour unchanged this phase, so the change is isolated). Tests: each declared check persists the right `stale`/`confidence`; the SoT is internally consistent (every `q()` call site is now a declared entry). |
| 2 | `tpcore/auditheal/`: `RemediationSpec` + `REMEDIATION_SPECS` + drift-coverage test (== `CROSS_TABLE_CHECKS` violation_count keys) + `run_audit_heal` orchestrator (generic; `run_stage` + `run_audit` injected) + `__main__` thin caller (exit 0/1). Reuses `tpcore/selfheal/runner.make_canonical_runner`. **Landed dark** (not wired). Deterministic fake-pool / fake-runner / fake-audit unit tests, mirroring `test_selfheal.py`. |
| 3 | Wire `run_data_operations.sh` Step 3 to call `python -m tpcore.auditheal` instead of `scripts/run_audit_all_tables.sh`. The thin caller runs the structured audit, closes the loop, and exits honestly (`1` ⇒ hard stop, as Step 3 already does on non-zero; `0` ⇒ proceed). **Net behaviour change:** the `cross_ref_cleanup` class is now auto-remediated + re-audited; real cross-table violations now actually fail the cycle (theatre fixed). |
| 4 | Doc reconciliation: CLAUDE.md Step-3 description + the "cross-table audit must return 0 violations" claim (now genuinely enforced); the audit/data-adapter-pipeline docs; TODO.md #186 status. |

## 6. Non-goals

- Not building new destructive remediations beyond the existing
  `cross_ref_cleanup` class (escalate-only otherwise; broadening is a
  future, separately-scoped increment).
- Not an LLM. Not a new daemon/process (loop closes in the existing
  Step 3).
- Not touching `tpcore/selfheal` or Step-4c
  (`audit_data_pipeline.py`) — disjoint domains.
- Not changing the `dump` sections' behaviour (risk_state /
  open_orders / ingestion_jobs stay informational stdout).
- Operator interaction unchanged: internal data-layer hardening; the
  operator's only touchpoints remain the ADD/REMOVE Data Feed Change
  Request + the weekly digest ack. (#186 candidates 3/4/6 are out of
  scope — separate specs.)

## 7. Open questions for the plan phase

- **`data_quality_log` source-key format** for cross-table findings:
  lean `cross_table_audit.<table>.<check_name>` (no trailing
  `.<source>` component — `(table, check_name)` is already unique, and
  the `(source, timestamp)` constraint is satisfied since one row per
  check per run). Confirm against `data_quality_log`'s schema +
  `_persist`'s ON CONFLICT in the plan.
- **P1 keeps `main()` exit 0** (gate flip deferred to P3) so the
  enforcement change is isolated and independently reviewable — lean
  yes; confirm no other caller depends on Step-3 staying non-enforcing
  in the interim.
- **`max_iterations` default**: match `tpcore/selfheal`'s
  `DEFAULT_MAX_ITERATIONS` for operator-model consistency.
- **In-process vs subprocess re-audit**: lean — invoke the structured
  audit in-process (import the refactored entrypoint) so the detector
  and the post-remediation re-check are the *same* code path (cannot
  drift); decide in the plan with the blast-radius lens.
- **Convergence / predicate-parity (load-bearing).** The audit's
  `tradier_options_chains / orphan_no_prices` check joins
  `prices_daily` (`SELECT DISTINCT ticker`), while the
  `cross_ref_cleanup` stage deletes orphans `WHERE NOT EXISTS … FROM
  platform.prices_daily_tickers`. If those two predicates are not
  equivalent, the remediation will not clear the audit's red →
  bounded retry exhausts → escalate (a false hard-stop, not a silent
  pass — safe, but wrong). The plan MUST verify the audit check's
  predicate exactly matches what `cross_ref_cleanup` deletes (align
  the audit SQL to the stage, or vice-versa) so the loop provably
  converges. Same parity check for `expiration_in_past` vs the stage's
  `expiration_date < CURRENT_DATE` delete (these already match —
  confirm in the plan).
