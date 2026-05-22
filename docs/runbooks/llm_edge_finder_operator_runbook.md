# LLM Edge-Finder — Operator Runbook (Task #25 Path B v1.0)

**Slash skill:** `/lab-edge-find`
**Canonical CLI:** `python -m ops.llm_edge_finder --trigger operator_command [--target <engine>] [--reference-bundle <name>]`
**Spec:** `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`

This runbook is your procedural counterpart to the §12 audit dashboard. It covers the operator surfaces the autonomous loop creates and the manual interventions you'll occasionally need.

---

## §1 What the finder is + isn't (cheat sheet)

The finder is the **autonomous edge-finder**. It runs Phase A (snapshot + references) + Phase B (bounded LLM↔tool-sandbox loop) + Phase C (up to 3 `ProposedSpec` emissions). Phase D-F (auto-promote, live-paper monitoring, auto-retire) ship in follow-up PRs.

**Binding outcome (operator-pinned 2026-05-21):** edges that trade and make money. NOT "candidate reaches PAPER" (necessary, not sufficient). The §12 audit dashboard surfaces `LiveOutcome` for finder-emitted PAPER engines; **you mark `LAB_FINDER_OUTCOME_VERDICT(verdict='success'|'failure')`** on your own cadence ("I know it when I see it"). The autonomous loop reads your marker + the mechanical $5k bleed-cap to drive auto-retire.

What the finder **does NOT** do:
- Bypass the SP-A DSR/credibility gate (sacred per spec §2.3).
- Bypass the per-regime + aggregate `n_trials` ledger fences (constraints 14 + 17).
- Modify the engine roster directly (every change is via ECR).
- Modify provider bindings (DFCR-only).
- Self-merge PRs at v1 (Phase D ships in a follow-up).
- Run live-paper monitoring at v1 (Phase E ships in a follow-up).

---

## §2 Running the finder manually

```bash
# Default — finder picks target from snapshot.roster
/lab-edge-find

# Target a specific engine
/lab-edge-find --target reversion

# Add caller-named reference bundles
/lab-edge-find --reference-bundle carver_systematic_trading
```

Without arguments, `/lab-edge-find` runs one cycle on today's session_date with the 3 mandatory bundles loaded. Output is one `LAB_FINDER_RUN` row in `application_log` + up to 3 `ProposedSpec` emissions visible in the next run's transcript or via the §12 dashboard.

**Pre-conditions checked at startup:**
- `ANTHROPIC_API_KEY` set (otherwise smoke-mode: provenance row still lands, zero emissions).
- `DATABASE_URL_IPV4` reachable.
- 3 mandatory bundles non-empty + non-stub on disk.
- Persona file SHA matches `PERSONA_SHA256` in `tpcore/lab/llm_finder/__init__.py`.

---

## §3 Reading the §12 audit dashboard

The dashboard (planned: `dashboard_components/finder_audit.py` — ships in a follow-up PR) surfaces 5 panels:

1. **Recent finder runs (last 7 UTC days).** `FinderRun` rows ordered by `started_ts` desc. Each shows trigger / regime_tuple_id / proposed_spec_count / auto_merged_pr_urls / auto_issued_ecr_refs / rejection_reason.
2. **Active finder-emitted PAPER engines** (`outcome_proven=False`). Per-engine `LiveOutcome` table — rolling P&L, descriptive Sharpe HAC, drawdown, bleed-budget usage (% of $5k cap), trade count, days-in-PAPER, current `operator_verdict` (`none|success|failure`). **Per row: a `Post Verdict` action button** that emits the `LAB_FINDER_OUTCOME_VERDICT` event for the autonomous loop to read.
3. **Outcome-proven engines.** Archived list with final `LiveOutcome` at the moment of your success-verdict.
4. **Auto-retired engines.** Archived list with retire-reason (`bleed_cap|operator_failure|inactivity_timeout|global_bleed_cap`) + auto-ECR-RETIRE PR URL.
5. **`LAB_FINDER_ACTION` audit feed.** Time-ordered log of every autonomous action with `triggered_by` + linked PR URL + `human_override` (always `'none'` in v1).

**Your daily-ish workflow on this dashboard:**
- Eyeball Panel 2 (active PAPER engines). For each: does it look like it's making money? If yes → click `Post Verdict: success`. If no → click `Post Verdict: failure`.
- Skim Panel 5 (audit feed) for surprise auto-actions. v1 v1: no surprises expected since Phase D-F not yet shipped.

---

## §4 Operator actions

### §4.1 Post a Tier-2 verdict via the dashboard

The §12 dashboard surfaces a `Post Verdict` action per active finder-emitted PAPER engine. Clicking emits:

```sql
INSERT INTO platform.application_log
  (engine, event_type, ts, payload)
VALUES
  ('llm_edge_finder', 'LAB_FINDER_OUTCOME_VERDICT', NOW() AT TIME ZONE 'UTC',
   '{"engine": "<engine>", "verdict": "success|failure", "operator_note": "<optional>"}'::jsonb);
```

The autonomous loop reads this on the next Phase E monitor tick + transitions to Phase F1 (success → `outcome_proven=True`) or F2 (failure → auto-ECR-RETIRE).

### §4.2 Pause the finder co-task

If outcomes look systematically wrong (e.g. the loop is bleeding capital faster than expected, or surfacing many junk emissions), pause the co-task:

```sql
INSERT INTO platform.application_log
  (engine, event_type, ts, payload)
VALUES
  ('llm_edge_finder', 'EDGE_FINDER_DISABLED', NOW() AT TIME ZONE 'UTC',
   '{"operator_reason": "<short reason>"}'::jsonb);
```

The co-task (`_edge_finder_loop` in `ops.llm_triage_service`) checks for this event on every poll + sleeps the lane until you re-enable. Other lanes (data, engine, lab_emitter) continue.

To re-enable: emit `EDGE_FINDER_ENABLED` with the operator reason.

### §4.3 Promote `outcome_proven=True` engine to LIVE

LIVE graduation is operator-only per the paper-only mandate. Once a finder-emitted engine has `outcome_proven=True` (via your `verdict='success'` marker):

```bash
/ecr  # follow the ADD/MODIFY/REMOVE template
```

Use the standard ECR-MODIFY path with `target_state: LIVE`. The Engine SDLC checklist runs as usual.

### §4.4 Roll back an auto-action

If the autonomous loop did something you disagree with (e.g. auto-retired an engine you wanted to keep), issue a counter-ECR by hand:

```bash
/ecr  # action=ADD with source=existing_code, restore the engine to PAPER
```

The finder writes the original `LAB_FINDER_ACTION(action='ecr_retire', triggered_by=...)` for provenance; your counter-ECR is a separate ledger entry. The §12 dashboard shows both side by side.

### §4.5 Edit the persona

Persona changes are operator-staged (the LLM cannot edit via diff-scope fence):

1. Edit `docs/personas/lab_finder_persona.md`.
2. Bump `PERSONA_VERSION` in `tpcore/lab/llm_finder/__init__.py` (e.g. `v2.0` → `v2.1`).
3. Update `PERSONA_SHA256` to the new file SHA: `sha256sum docs/personas/lab_finder_persona.md | cut -c1-64`.
4. The `test_persona_versioned` sentinel reds on drift — both values must match before CI passes.

### §4.6 Edit reference bundles

Same as persona — operator-staged via the standard PR flow. Mandatory bundles (`dsr_ntrials_discipline`, `regime_aware_trading`, `market_structure_primer`) MUST stay non-empty and non-stub or the `reference_loader` fails loud.

---

## §5 Failure modes + fixes

| Symptom | Cause | Fix |
|---|---|---|
| `AuthSkip` in logs | `ANTHROPIC_API_KEY` unset | Set the env var; finder runs in smoke mode otherwise (provenance lands, no emissions) |
| `ReferenceNotFoundError` | Mandatory bundle missing | Restore the bundle file under `docs/lab_emitter_references/` |
| `ReferenceEmptyError` | Bundle file 0 bytes | Replace with non-empty content |
| `ReferenceStubError` | Bundle contains `[operator-pending content]` | Author the bundle content + remove the stub marker |
| `SnapshotOverflowError` | Snapshot > 512 KiB | Reduce universe (sp500 → smaller subset) OR shorten price window. NEVER silently truncate |
| `AgentError: kind=...` | LLM emitted malformed envelope | Single-run failure; FinderRun.rejection_reason carries detail; no auto-retry |
| `llm_json_decode_failed` (in transcript) | LLM returned non-JSON text | Loop continues with synthetic `AnalysisRequest`; persistent occurrences → check persona for output-contract clarity |
| Bleed-cap auto-retire fires | Engine bled >$5k cumulative | Mechanical (correct behavior); engine retires; you audit the EULOGY for design lessons |
| Inactivity-timeout auto-retire | Engine 60 sessions PAPER + <30 trades + no verdict | Mechanical (capital-slot scarcity); free the slot for another candidate |
| Global bleed-cap pause | Aggregate finder-engine bleed > $12k (80% of $15k) | Finder co-task auto-pauses; existing engines continue; pause lifts when aggregate drops < $7.5k via auto-retires or success-verdicts |

---

## §6 Defect register

If you spot a finder emission that looks like a genuine bug (not just an unconvincing candidate):

```bash
python -m ops.defect_register log \
    --ref FINDER-<short-tag>-$(date +%Y-%m-%d) \
    --summary "<one-sentence finder defect>" \
    --lane engine
```

Pair with a TODO.md entry tagged `[defect_ref: FINDER-...]` per the CI forcing-test convention.

---

## §7 Related runbooks + skills

- `/lab-spec-emit` — SP-G single-emission emitter (the layer Task #25's `emit_once_with_auto_promote` will call once Phase D ships).
- `/lab-target-run` — runs an actual Lab probe + dossier.
- `/ecr` — Engine Change Request (the touchpoint a finder-emitted spec eventually flows through).
- `/audit-data-pipeline` — full data-lane audit (the snapshot reads the same substrate the audit validates).
- `docs/runbooks/lab-spec-emit-orphaned-spend.md` — recovery for SP-G orphaned-spend (the finder inherits SP-G's fence stack via `emit_once_with_auto_promote`).

---

## §8 The bottom line

**Your role on this loop is judgement, not gate-keeping.** The autonomous fence stack catches statistical fraud (DSR, PBO, per-regime ledger, HAC defaults, cost-net Sharpe). The mechanical bleed-cap catches capital destruction. You catch "this isn't actually making money in PAPER" — the part that's irreducibly operator-judgement at runtime ("I know it when I see it").

Run the dashboard at whatever cadence works for you. Daily is fine. Weekly is fine. If something looks wrong, post a verdict + the audit trail explains why.
