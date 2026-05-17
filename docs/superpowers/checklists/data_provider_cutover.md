# Data Provider CUTOVER Runbook (snap-in / snap-out)

Stage 4 of the Data Provider Lifecycle (spec `…/specs/2026-05-17-data-
provider-lifecycle-design.md`; plan Phase 5). The operator procedure
for swapping the live provider behind a feed.

**CUTOVER is operator-confirmed and structural — never autonomous.**
The `ProviderBinding` registry is a frozen code SoT (like
`engine_profile` / `HealSpec`); the flip is a *reviewed PR editing
`_BINDINGS`*, exactly like the Sigma engine archival was a PR. The
deterministic guard (`tpcore.providers.plan_cutover`) validates the
transition is legal; you apply it. Audit trail = the PR.

## When to cut over

- A feed's ACTIVE provider degraded (parity/freshness red) **and** a
  `FALLBACK` (parity-verified) exists → promote the fallback.
- A better provider was onboarded + passed EVALUATE → it became
  `FALLBACK`; promote it to make it primary.

## Procedure

1. **Confirm eligibility (the guard).** Run the transition-guard:
   ```python
   from tpcore.providers import plan_cutover
   plan_cutover("<feed>", "<new_provider>")            # demote incumbent → FALLBACK
   plan_cutover("<feed>", "<new_provider>", retire_incumbent=True)
   ```
   - `allowed=True` → it prints the exact `_BINDINGS` status changes to
     apply. `allowed=False` → fix the `block_reason` first; **do not
     hand-edit around a block** (a `CANDIDATE` blocked here means it
     skipped EVALUATE — run `data_provider_evaluate.md`, not a manual
     flip; that bypass is the silent-degradation class this lifecycle
     exists to prevent).
2. **Apply the plan in a PR.** Edit `tpcore/providers.py` `_BINDINGS`:
   set the new provider's `status=ACTIVE`; demote the incumbent to
   `FALLBACK` (reversible — keep it parity-verified as a standby) **or**
   `RETIRED` (then the §3 `data_provider_retire.md` 3-way-atomic rule
   applies: archive history + retire FeedProfile/HealSpec/audit in the
   **same** PR). Update both bindings' `evidence` with the cutover
   reason + date.
3. **Re-validate.** The branch must pass:
   - `test_providers.py` (exactly-one-ACTIVE, drift, frozen),
   - `test_provider_lifecycle_consistency.py` (3-way; a RETIRED
     incumbent must be fully offboarded),
   - the full suite + the per-feed validation for the swapped feed
     (the new ACTIVE provider's data is green by the canonical check).
4. **Confirm post-cutover, against reality.** After merge + the next
   data-ops cycle, verify the feed's `validation.<check>` is green and
   the `audit_data_pipeline` referential layer is clean — *check the
   DB/row counts, never just the exit code* (the recurring lesson).

## Reversibility

A `FALLBACK`-demoted incumbent stays parity-verified, so a bad cutover
is reversed by the inverse `plan_cutover` + PR. A `RETIRED` incumbent
is **not** reversible without re-onboarding — only retire the incumbent
when you are certain (it triggers the full RETIRE gate).

## Non-goals

- No autonomous swapper / no daemon flips providers (a provider change
  is structural, like engine archival — operator-confirmed always).
- The guard never mutates the SoT and never trades; it validates and
  hands you the exact, legal change.
