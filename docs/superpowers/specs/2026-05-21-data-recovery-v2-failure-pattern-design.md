# Triage v2 — Failure-Pattern Mapping (design spec, both lanes)

**Naming:** this spec covers data-lane v2 (`llm_data_recovery_v2`). The engine-lane parallel work (`llm_engine_triage_v2`) needs a sibling spec — see §6 below for the operator scope-decision.

---

# Data Recovery v2 — Failure-Pattern Mapping

**Status:** PROPOSED — awaiting operator scope-approval.
**Predecessor:** v1 shipped via PR #233 (commit `c79cde9`) — `ops/llm_data_recovery.py` + `docs/llm_triage_personas/data_recovery_v1.md`. v1 has a whitelist of stages the LLM can invoke but no explicit (failure-shape → recovery-action) mappings — the LLM picks "general reasoning" against whatever escalation it sees.
**Operator directive (2026-05-21):** "make sure you can self heal each of these issues you found... why aren't you making the triage more smart while you wait for data to update?" + "are you even following the development process?" — hence spec-first, build-later.

## 0. Why v2

v1 escalation-to-action is generic. v2 makes the LLM's action selection EVIDENCE-DRIVEN: each failure shape we have empirical evidence for gets an explicit mapped action documented in the persona, so the LLM pattern-matches first and falls back to general reasoning only when no pattern matches.

The empirical evidence is the 2026-05-21 incident itself — six concrete failure shapes hit the data lane this session. Each is a data point for what v2 must handle.

## 1. The 6 failure patterns + proposed recovery actions

### Pattern 1 — `daily_bars` stage 3600s timeout
- **Log shape:** `daily_bars timed out after 3600.0s` (severity=ERROR, event_type=INGESTION_FAILED, data.reason="timeout")
- **Status post-#236:** the chunked `force_refresh` is supposed to never time out at the stage level. If it STILL escalates, that means a single chunk ran >3600s — different failure mode.
- **Proposed action:** `daily_bars(force_refresh=true, repair_coverage=true)` — narrower scope, force only the coverage_collapse gap.
- **Confidence:** medium. Could also be `daily_bars(force_refresh=true, universe=tier_1_2)` to scope by tier instead.

### Pattern 2 — Supabase pooler connection drop mid-fetch
- **Log shape:** `daily_bars failed: connection was closed in the middle of operation` (severity=ERROR, INGESTION_FAILED)
- **Status:** PR #163 retry-on-transient-DB exists for per-chunk panel-loads. Whole-stage drop isn't auto-recovered.
- **Proposed action:** re-invoke `daily_bars(force_refresh=true, universe=active, feed=sip, end_offset_days=1)` — the chunked path retries each chunk individually; one drop costs one chunk, not the whole run.
- **Confidence:** high.

### Pattern 3 — Alpaca SIP transient 403 ("subscription does not permit querying recent SIP data")
- **Log shape:** 403 response with body containing `"subscription does not permit"`
- **Status post-#231:** orchestrator cascade probes SIP before falling to IEX. LLM only sees this if the cascade itself failed (rare).
- **Proposed action:** re-probe SIP after 60-120s delay; if still 403, switch to `feed=iex` and emit `INGESTION_AUTO_RECOVERY_DEGRADED`.
- **Confidence:** medium. The retry-delay is a guess; could just be "fail over to iex immediately" instead.

### Pattern 4 — `greeks_max_pain` 401 (different provider, auth failure)
- **Log shape:** `greeks_max_pain failed: greeks_pro /api/analytics/maxpain returned 401`
- **Status:** NOT in LLM whitelist. Different provider entirely. Auth issue is operator-credential.
- **Proposed action:** skip-with-warning. LLM cannot resolve a third-party API auth. Emit `DATA_RECOVERY_ACTION_SKIPPED` with `reason=provider_auth_failure`, `provider=greeks_pro`. Operator rotates creds.
- **DO NOT add `greeks_max_pain` to invokable whitelist** — there's no LLM-runnable recovery.
- **Confidence:** high. Skip is correct.

### Pattern 5 — `fundamentals_quarterly_completeness` validation failure
- **Log shape:** `data_validation failed: validation suite failed: ['fundamentals_quarterly_complete...]'`
- **Status:** NOT in v1 whitelist. The validation suite found a real defect.
- **Proposed action:** trigger `fundamentals_refresh` stage (add to whitelist if not present). If still red after refresh, escalate with `reason=validation_failure_persists`.
- **Confidence:** medium. Need to verify `fundamentals_refresh` is the right repair stage.

### Pattern 6 — `repair_gaps` blindness to coverage_collapse
- **Log shape:** stage returns `skipped: no_gaps_or_not_bars_fixable` despite obvious gaps
- **Status:** known design flaw — completeness check threshold blind to partial sessions. Orchestrator cascade (#231) routes around this.
- **Proposed action:** persona EXPLICITLY tells the LLM "do NOT pick `repair_gaps` for coverage_collapse-shaped failures; pick `force_refresh` with appropriate feed and universe instead." Negative pattern.
- **Confidence:** high.

### Pattern 7 — Daemon not installed (data-repair / llm-triage / lane-service)
- **Status:** NOT LLM-recoverable. The LLM RUNS IN one of those daemons; if the daemon isn't installed, the LLM never gets the event.
- **Out of scope for this spec.** Documenting separately as a pre-flight startup check (`engine-service` emits `DAEMON_PEER_MISSING` event if peer plist absent).

## 2. v2 shape

**Persona changes** (`docs/llm_triage_personas/data_recovery_v2.md` — new file, NOT overwriting v1):
- New "PATTERN MATCHING" section at the top
- 6 patterns documented with log shape (regex or exact string) + mapped action + confidence reasoning
- Persona explicitly: "match against documented patterns FIRST; fall back to general whitelist reasoning only if no pattern matches"
- Operator-curatable (markdown, no code)

**Code changes** (`ops/llm_data_recovery.py`):
- Add `fundamentals_refresh` to `_AUTONOMOUS_DATA_ACTIONS` if not present
- Add `_SKIP_WITH_WARNING_ACTIONS` set for failures where skip is the correct answer (Pattern 4)
- Add `_NEGATIVE_PATTERNS` — failures where a specific stage MUST NOT be picked (Pattern 6: don't pick `repair_gaps` on coverage_collapse)
- Switch persona load to v2 (or version-select based on env)

**Tests** (`tests/test_llm_triage_autonomous_data_recovery.py`):
- One test per pattern: mock LLM returns the documented action → assert correct dispatch
- One test for the negative pattern: mock LLM tries `repair_gaps` on coverage_collapse → assert rejection + escalation
- Tests assert pattern-match HAPPENS (not just that an action runs)

## 3. Out of scope (defer to v3)

- Engine-lane failures (still PR-gated)
- Live-trading recovery (paper-only mandate stays binding)
- Pattern 7 daemon-missing detection (separate spec: pre-flight startup check)
- Cost optimization (telling the LLM to prefer cheaper actions first)
- Persona SHA-pinning enforcement (assumed inherited from SP-G fence)
- Retry-cap-per-pattern (each escalation = ONE LLM-driven action; multi-step recovery is v3)

## 4. Operator scope-approval questions

Before plan-PR:

1. **All 6 patterns in scope, or subset?** (Default: all 6.)
2. **Pattern 1 action — `repair_coverage=true` vs `universe=tier_1_2`?** (My recommendation: `repair_coverage` because it targets the actual gap, not a tier-narrowed full run.)
3. **Pattern 3 action — delay-and-retry SIP vs immediate IEX failover?** (My recommendation: immediate IEX failover; the SIP transient is operator-time-sensitive to investigate, not auto-resolvable.)
4. **Pattern 5 — verify `fundamentals_refresh` is the right repair stage?** (Need code-confirm; will check during plan PR.)
5. **v2 persona — new file or replace v1?** (My recommendation: new file `data_recovery_v2.md`; v1 stays for rollback.)

## 6. Engine-triage parallel — operator scope decision

The architecture has **two distinct LLMs per job** by design:

| LLM | Authority | Lane | Source files |
|---|---|---|---|
| `llm_data_recovery` (v1, PR #233) | **Autonomous-action** — invokes whitelisted stages directly, no human gate | Data recovery | `ops/llm_data_recovery.py` + `docs/llm_triage_personas/data_recovery_v1.md` |
| `llm_engine_triage` / `llm_lab_emitter` (SP-G) | **Advisory PR-gated** — opens draft PRs, never auto-merges, never auto-invokes engine code changes | Engine / Lab / roster | `ops/llm_lab_emitter.py` + `tpcore/engine_llm_triage/` |

Operator confirmed 2026-05-21: "two different llms per job.. one job data one job engine" — that's already the design. v2 pattern-matching applies to BOTH but they need SEPARATE specs because:

- **Action models differ:** data-lane outputs a stage+params dict; engine-lane outputs a draft ECR/PR proposal.
- **Whitelists differ:** data-lane = data-stage names; engine-lane = ECR action types (ADD / MODIFY / REMOVE / RETIRE) + the engine roster.
- **Escalation events differ:** data-lane consumes `DATA_REPAIR_ESCALATED` / `INGESTION_AUTO_RECOVERY_FAILED`; engine-lane consumes engine-side events (Lab dossier verdicts, AAR aggregation, ECR drift).
- **Personas differ:** data-lane is operationally-focused (stage invocation, feed selection, retry timing); engine-lane is design-focused (signal-presence reasoning, autonomous Lab criteria adjudication, scope of proposed change).
- **Failure-pattern catalogs differ:** data-lane patterns are infrastructure shapes (timeouts, auth, coverage gaps); engine-lane patterns are signal shapes (drawdown breach, AAR-flagged degradation, dossier-failed-the-gate).

**Scope-decision (operator must answer before plan PR):**

A. **One v2 spec covering both lanes** — this document expands to include `llm_engine_triage_v2` patterns. Single PR pair (spec → plan → build) covers both.

B. **Two parallel v2 specs** — this stays "Data Recovery v2"; a sibling `2026-05-21-engine-triage-v2-failure-pattern-design.md` ships independently. Two PR pairs, can land in either order.

C. **Data-lane v2 only for now** — engine-lane v2 deferred. Operator addresses engine-triage v2 separately later.

**Recommendation:** **B** — two parallel specs. Justification:
- Action models genuinely differ → conflating them risks one-size-fits-all decisions
- Engine-lane changes hit `tpcore/engine_profile.py` (roster SoT) which is ECR-only — totally different review pattern
- Parallel work allows the Carver session to take engine-lane while I take data-lane (or vice versa)
- Each spec stays sized for fresh-context review (no 800-line monsters)

## 5. What this spec is NOT

- NOT a build PR. No code in this PR.
- NOT a plan PR. No file-by-file implementation sequence.
- NOT a process bypass. This IS the process: spec → operator-review → plan → build.

## References

- PR #233 (v1 ship): `c79cde9`
- PR #231 (orchestrator cascade): `90cf4d3`
- PR #236 (chunked force_refresh + lane-service): `e66c4aa`
- PR #163 (transient-DB retry per chunk)
- Operator memory: `feedback_self_heal_autonomous_no_operator_task.md`, `feedback_stop_over_asking_use_expert.md`
- 2026-05-21 incident application_log: query at session-time for verbatim error strings (preserved for the persona's pattern regexes)
