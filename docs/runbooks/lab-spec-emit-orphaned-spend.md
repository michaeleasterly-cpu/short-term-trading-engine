# Runbook — SP-G `lab-spec-emit` orphaned-spend recovery

**Lane:** Lab / SP-G (the thin advisory LLM spec-emitter).
**Trigger:** the operator runs `/lab-spec-emit ...`, the SP-A ledger
row is written, but the draft PR is NOT visible on GitHub (the
`gh pr create --draft` step failed, or the operator's CI/network was
flaky after step 5 but before step 6 completed).
**Authoritative SoT:** spec §3.4 step 5/6 — *"the ledger row is
written **before** the draft PR is opened. If step 5 succeeds but
step 6 fails (gh CLI flake), the ledger row stands — by design: the
LLM consumed budget the moment it received the response, even if the
operator never sees the draft PR."*

---

## Why orphaned spend can happen

The SP-G emission sequence is strictly ordered (spec §3.4):

1. `ledger_gate.check_budget(target)` — pre-emission rate-limit fence
2. Build `EmissionContext`
3. Invoke the Anthropic SDK
4. Validate the response against `EmittedSpec`
5. **`record_trial_spend(target, trials, source="llm_emitter:<persona_sha>")`**
   — the SP-A ledger row is written **before** the draft PR is opened
6. Render the markdown spec + `gh pr create --draft`

**Step 5 → 6 is a non-transactional boundary.** If step 5 succeeds and
step 6 fails (gh CLI flake, transient network outage, label not
authorised, branch already exists, diff-scope fence trips on a
malformed renderer output), the ledger row stands. The LLM consumed
budget the moment it received the response; the operator never sees
the draft PR.

This is **by design**, not a bug — under-counting the ledger is a
multiple-testing pollutant (spec §2.1, the SP-A H-LL-1 contract). The
ledger is fail-safe toward **over-count**, never under-count.

## What to do when orphaned spend happens

### 1. Confirm the spend actually orphaned

Symptoms: `/lab-spec-emit` printed a `LLM_LAB_EMITTED_SPEC` advisory
event but no draft PR was opened; `gh pr list --draft --label
lab-spec-emit` shows no row for the emission's candidate name.

Check the cumulative ledger:

```bash
psql "$DATABASE_URL" -c "
  SELECT timestamp, notes::jsonb
  FROM platform.data_quality_log
  WHERE source = 'lab_trial_ledger.<target_engine>'
    AND timestamp > now() - interval '1 hour'
    AND notes::jsonb->>'candidate' = '<candidate_name>'
  ORDER BY timestamp DESC LIMIT 5;
"
```

If a row exists with the emission's candidate name and no draft PR
exists for it: this is an orphaned spend.

### 2. The recovery: re-render and re-open the draft PR

The ledger row stands; the operator does NOT spend budget again. The
agent persists the validated `EmittedSpec` JSON sidecar to a stable
path under `docs/lab/<date>-<candidate>-emitted-spec.json` (spec §4.4
allow-list slot 2) — this is the machine-readable record of what was
emitted.

To recover, the operator re-renders the spec from the persisted JSON
sidecar and opens the draft PR by hand:

```bash
# 1. Find the persisted sidecar (the agent writes it to the working
#    tree before attempting gh; if the working tree was torn down by
#    a worktree-cleanup, fall back to step 2b).
ls docs/lab/*-<candidate_name>-emitted-spec.json

# 2a. Re-render the markdown spec from the sidecar:
python -m ops.llm_lab_emitter --replay docs/lab/<sidecar>.json

# 2b. If the working-tree sidecar is gone, reconstruct from the
#     LLM_LAB_EMITTED_SPEC application_log event:
psql "$DATABASE_URL" -c "
  SELECT data::jsonb->>'emitted_spec_json'
  FROM platform.application_log
  WHERE event_type = 'LLM_LAB_EMITTED_SPEC'
    AND data::jsonb->>'candidate_name' = '<candidate_name>'
  ORDER BY recorded_at DESC LIMIT 1;
" -t -A > /tmp/<candidate_name>-emitted-spec.json
python -m ops.llm_lab_emitter --replay /tmp/<candidate_name>-emitted-spec.json
```

The replay path:

- Re-validates the JSON against `EmittedSpec` (a corrupted sidecar
  rejects).
- Re-renders the markdown spec.
- Re-applies `validate_no_gate_override`.
- Re-applies `enforce_diff_scope` against the would-be diff.
- Calls `gh pr create --draft` again — does NOT call
  `record_trial_spend` (the ledger row already stands; idempotency on
  the `(source, timestamp)` PK would no-op the duplicate insert
  anyway, but the replay path explicitly skips step 5).

### 3. When NOT to recover

- If the emission was genuinely defective (the rationale is nonsense,
  the hypothesis is malformed, the falsification criterion is empty
  prose): do NOT recover. Mark the orphaned ledger row with an
  audit-trail event (`LLM_LAB_EMIT_ABANDONED` on the
  `application_log`), and the cumulative DSR-deflation will absorb
  the spent trials on the target's next legitimate Lab run. The
  monotone-harder fence is the structural defense.
- If the operator's review concludes the emission would have produced
  a draft PR that violated the diff-scope fence (the rendered spec
  named a forbidden path): do NOT recover. The fence reds the build;
  the operator marks the orphan and moves on.

## What this runbook does NOT cover

- A failure BEFORE step 5 (`check_budget` rejected; the Anthropic SDK
  returned a malformed response; the agent crashed before
  `record_trial_spend`): no ledger spend occurred, no recovery
  needed.
- A failure AFTER step 6 (the draft PR was opened but the operator
  closed it without merging): the spend already counted, the gate's
  monotone-harder fence handles this without operator action.
- Cumulative ledger reset / quota raise: these are NOT operator
  actions covered by this runbook. The default
  `EMISSION_QUOTA_PER_TARGET = 20` (per operator Q2 decision) is the
  pinned default; an env override is the explicit operator action
  (spec §4.1).

## Cross-references

- Spec: `docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md`
  §3.4 (the strict ordering), §4.1 (the quota fence).
- SP-A ledger: `tpcore/lab/ledger.py` (the substrate); the source-
  prefix audit query is the same shape every operator audit uses.
- The diff-scope fence: `tpcore/lab/llm_emitter/diff_fence.py` +
  `tpcore/lab/llm_emitter/tests/test_diff_fence.py` (load-bearing).
