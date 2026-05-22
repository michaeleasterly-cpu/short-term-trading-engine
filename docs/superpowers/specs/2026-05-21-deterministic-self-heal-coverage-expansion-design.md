# Deterministic Self-Heal Coverage Expansion (design spec)

**Status:** APPROVED (autonomous — Wave 1 scope decisions answered in §4 ANSWERED; standing rule [[ask-expert-then-execute]] + [[stop-over-asking-use-expert]] + [[self-heal-autonomous-no-operator-task]]).
**Trigger:** operator directive 2026-05-21 — *"we wont be deploying the llm data triage it will run locally with my max account... we need to update the self healing with as much use cases as possible for the deterministic agents ... we need to do the same for the engines"*
**Predecessor:** PRs #227 / #231 / #236 (data cascade), PR #239 (LLM data_recovery_v2 with 6 patterns), PR #235 (CSV archive R3), PR #233 (LLM autonomous action), PR #252 (gitleaks gate).
**Memory:** [[llm-triage-runs-local-on-max]] — the LLM-side runs operator-local on Max, not deployed.

## 0. Why now

Today's data-lane incident (2026-05-18 → 21) exposed: the system has self-heal scaffolding (`repair_coverage`, `repair_gaps`, `force_refresh`, `data_repair_service`, `llm_triage_service`) but the deterministic cascade only handled ONE failure shape (`coverage_collapse`). Every other failure mode either escalated to the LLM or did nothing. The operator's directive: **maximize deterministic coverage; the LLM is the long-tail backstop**, not the first line.

The same pattern should apply to engines — deterministic engine-side recovery for as many failure shapes as possible; `ops/engine_llm_triage.py` becomes the long-tail backstop.

## 1. Data-lane deterministic self-heal — failure mode catalog

Each row: failure shape, current deterministic coverage, proposed coverage.

| # | Failure shape | Detection signal | Current deterministic recovery | Proposed deterministic recovery |
|---|---|---|---|---|
| D1 | `daily_bars` coverage_collapse | freshness check tripwire | Cascade probes SIP → IEX feed (PR #231) | **DONE** |
| D2 | `daily_bars` stage timeout | INGESTION_FAILED `reason=timeout` | Chunked force_refresh (PR #236) reduces per-chunk scope | **EXTEND:** cascade should detect timeout on a non-chunked invocation + re-invoke with `force_refresh=true universe=active feed=sip` (chunked path) |
| D3 | Connection drop mid-stage | error contains "connection was closed" | Per-chunk retry (PR #163) for panel-load | **EXTEND:** orchestrator-level re-invoke of the failed stage ONCE before escalating |
| D4 | Alpaca SIP transient 403 | response body "subscription does not permit" | SIP→IEX feed swap (PR #231) | **DONE** |
| D5 | Provider 401/auth | response status 401 | None — escalate to operator | **NEW:** retry once on the assumption of transient creds-cycle; on second 401, emit `PROVIDER_AUTH_ESCALATED` + skip the stage cleanly (no daemon abort) |
| D6 | Validation suite partial failure | `data_validation` returns red on specific checks | None — daemon exits 1 | **NEW:** parse the red-check list, dispatch the canonical refresh stage per check (`fundamentals_quarterly_completeness → fundamentals_refresh`, `liquidity_tiers_completeness → tier_refresh`, etc.) |
| D7 | Monotonicity violation | `*_monotone` check red | None | **NEW:** dedupe stage that finds rogue rows by (ticker, date) + keeps the latest-inserted; re-runs the check |
| D8 | Macro completeness gap (specific indicator) | `macro_indicators_completeness` reports indicator+missing-dates | None — partial PR #247 fix (no longer crashes) | **NEW:** per-indicator targeted re-pull from the FRED adapter for the missing date range |
| D9 | Liquidity-tier ticker missing | `liquidity_tiers_completeness` reports 15 specific tickers | None | **NEW:** `tier_refresh --param universe=<missing>` for the specific tickers |
| D10 | Ticker classification missing | `ticker_classifications_coverage` red | None | **NEW:** `classify_tickers --param force=true --param universe=<missing>` |
| D11 | Freshness vendor_late | `latest_published` probe returns ≤ our_latest | Memory-documented selfheal.vendor_late event; partial | **DONE-VIA-#271** orchestrator-level recognition via `_auto_cascade_vendor_late` + `_VENDOR_LATE_CHECK_MAP` (AAII Thursday, fear_greed daily); emits `INGESTION_VENDOR_LATE_SKIPPED`; freshness check stays red as a classification (not a downgrade) |
| D12 | CSV archive substrate dead (Railway) | `write_archive` raises filesystem error | None | **DONE-VIA-#235** R3 substrate is env-pluggable; fails over to S3 backend |
| D13 | Postgres pool exhaustion | asyncpg PoolTimeout | None | **DONE-VIA-#262** circuit-breaker stage timeout that closes idle conns + re-opens pool, then retries the stage |
| D14 | `data_validation` stage timeout | `ops.stage.timeout` event on data_validation (300s cap exceeded) | None — Wave 1 cascade is keyed on FAILED check_name list which a TIMEOUT does not produce | **DONE-VIA-#271** `_chunk_validation_suite` partitions the 25-check suite into 6 chunks each with a 60s budget; aggregate failed-check list is synthesised into the canonical `"validation suite failed: [<names>]"` shape consumed by `_auto_cascade_validation_failures` (no contract change); emits `INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED` |

**Coverage today:** 14/14 deterministic (D1, D4, D12 pre-existing; D6-D10 Wave 1 via PR #261; D2/D3/D5/D13 Wave 2 via PR #262; D11+D14 via PR #271). **LLM is the long-tail backstop only.**

## 2. Engine-lane deterministic self-heal — failure mode catalog

Engines (reversion, vector, momentum, sentinel, canary, catalyst, carver) all run through the same scheduler/plug architecture. Failure modes:

| # | Failure shape | Detection signal | Current deterministic recovery | Proposed deterministic recovery |
|---|---|---|---|---|
| E1 | Engine scheduler stage failure | `engine_service` stage retval non-OK | **DONE-VIA-#267** — `_invoke_scheduler_with_recovery` in `ops/engine_dispatch.py` retries once + emits `ENGINE_STAGE_ESCALATED` | — |
| E2 | setup_detection panel-load failure | DB-fetch exception | **DONE-VIA-#267 (pilot)** — `tpcore/engine/transient_retry.py` shared helper + opt-in wire on `reversion/plugs/setup_detection.py` | EXTEND to other engines in follow-on PR (per Wave-3 scope decision) |
| E3 | Order placement failure | Alpaca API error during order submit | **DONE-VIA-#267** — `submit_with_transient_retry` in `tpcore/order_management/transient_retry.py` retries once + emits `ORDER_ESCALATED` on second-failure | — |
| E4 | AAR write failure | `aar_logging` plug exception | None | **NEW:** defer AAR to next cycle's deferred-AAR queue (new table `platform.aar_deferred`); don't fail the engine cycle |
| E5 | Capital gate failure | `assert_passed_for_engine` raises | Skip cycle (existing behavior) | **DONE** — current behavior is correct |
| E6 | Drawdown breach | engine PnL vs RiskGovernor breach threshold | RiskGovernor auto-pauses engine (existing) | **DONE** |
| E7 | Credibility drop | post-cycle `write_credibility_score` < threshold | None — Lab/AAR captures it, but no auto-action | **NEW:** if credibility < threshold for N consecutive cycles, auto-emit `ENGINE_CREDIBILITY_DROP` event + RiskGovernor pauses the engine pending operator review |
| E8 | Stale-order accumulation | `stale_order_cancel` finds orders past TTL | Auto-cancel stale orders (existing) | **DONE** |
| E9 | Engine package import error | scheduler raises ImportError | **DONE-VIA-#267** — `_pre_check_engine_import` in `ops/engine_dispatch.py` wraps + emits `ENGINE_IMPORT_FAILED` + skips engine (other engines continue) | — |
| E10 | Per-trade execution_risk failure | `execution_risk` plug raises mid-cycle | None | **NEW:** cancel any in-flight orders for the trade, emit `EXECUTION_RISK_ESCALATED`, skip the trade (not the whole cycle) |
| E11 | Lifecycle analysis stale | `lifecycle_analysis` reports degradation | None — captured by AAR but no auto-action | **NEW:** if lifecycle score < threshold for N cycles, RiskGovernor pauses the engine |

**Coverage today:** 7/11 — E1/E2(pilot)/E3/E9 shipped via PR #267; pre-existing E5/E6/E8. Remaining: E4 (deferred-AAR), E7 (credibility-pause), E10 (execution_risk-skip), E11 (lifecycle-pause) — Wave 4 (pending operator authorization). **Coverage after this spec:** 11/11 deterministic.

## 3. What this spec is NOT

- NOT a build PR. No code in this PR.
- NOT a plan PR. No file-by-file implementation sequence.
- NOT a one-PR build — each row (D1-D13, E1-E11) is its own bounded PR or small bundle. Sequenced in §5.
- NOT touching the LLM-side personas — those stay as the long-tail backstop. Deterministic-first is the design; LLM-second is the safety net.

## 4. ANSWERED — autonomous scope decisions (2026-05-21)

All six scope decisions resolved autonomously per standing rules [[ask-expert-then-execute]] + [[stop-over-asking-use-expert]] + [[self-heal-autonomous-no-operator-task]]. The Wave-1 PR (D6..D10) implements the answers below; Wave-2..Wave-5 decisions stand as ANSWERED for future PRs.

1. **All 24 rows in scope, or a subset?** — **ALL 24 in scope, sequenced per §5.**
   Reasoning: the operator's directive is "maximize deterministic coverage; LLM is the long-tail backstop." Cutting rows leaves the LLM persona owning shapes that have a clean deterministic-recovery path — opposite of the directive. The Wave-1..Wave-5 sequencing already provides the safety stagger; subset would be an unforced tightening.

2. **D5 provider auth — auto-retry-then-skip vs operator-rotate?** — **Auto-retry-then-skip-cleanly.**
   Reasoning: the daemon stays alive (key invariant per [[event-driven-not-scheduled]]). One transient creds-cycle retry is cheap; on a confirmed second 401 we emit `PROVIDER_AUTH_ESCALATED` to `application_log` and skip the stage cleanly. The operator sees the escalation in `application_log` and rotates creds on their own cadence — no operator-blocking task. Matches the spec's own recommendation; aligns with the same "daemon-never-aborts" invariant that protects the engine_service in E9.

3. **D7 monotonicity dedupe rule** — **Latest event_date wins; tiebreaker on latest `recorded_at` (insert time).**
   Reasoning: monotone violations are vendor-restatement events (FMP earnings reclassification, SEC Form 4 amended filings). The latest `event_date` row carries the most recent observation of the underlying event; on equal `event_date` the latest `recorded_at` row carries the most recent vendor write — i.e. the restatement, not the original. Same rule per the spec's own recommendation. First-seen would freeze us on stale data; latest-inserted alone is ambiguous when the same vendor write window contains both the original and a near-simultaneous correction.

4. **E4 deferred-AAR substrate** — **New `platform.aar_deferred` table.** (Deferred to Wave 4 — not in this PR.)
   Reasoning: AAR has structured fields (engine_name, cycle_started_at, credibility_score, lifecycle_metrics, …) that don't fit a JSONB-blob `application_log` schema. A typed table also lets the next-cycle replay query `WHERE replayed_at IS NULL` cheaply with an index — `application_log` would need a JSONB GIN scan. Matches the spec's recommendation; aligns with the per-table-discipline pattern already used for `earnings_events_count_snapshot`, `sec_insider_row_counts_snapshot`, `ticker_classifications_source_count`.

5. **E7 + E11 N-consecutive-cycles thresholds** — **N=3 for credibility (E7), N=5 for lifecycle (E11).** (Deferred to Wave 4 — not in this PR.)
   Reasoning:
   * **Credibility (E7) N=3**: credibility moves fast (Lab-driven, single-cycle DSR/cred re-scoring can flip a credibility value); 3 consecutive cycles is a clear-signal floor before auto-pause. N=1 risks pausing on a single noisy Lab run; N=5 is too slow for a "the engine is bleeding" signal.
   * **Lifecycle (E11) N=5**: lifecycle is a *trend* metric (engine slow-decay over multiple cycles, not a single-cycle shock). 5 cycles ≈ a trading week — clear signal that the engine is structurally degraded, not a single-day blip. Operator-tunable later per the spec.

6. **Engine-lane LLM-triage persona expansion** — **Separate spec, not in scope here.**
   Reasoning: this spec is deterministic-only by design (§3: "NOT touching the LLM-side personas"). Mixing the LLM-persona expansion would conflate the deterministic-first design with the long-tail backstop expansion. The LLM-persona work uses the data_recovery_v2 shape and lives in a sibling spec when it lands. Matches the spec's recommendation.

### Wave-1 implementation scope (this PR)

Per §5 Wave 1: D6, D7, D8, D9, D10. The answers above bear on Wave 1 as follows:
* Q1 (all 24 in scope) — Wave-1 ships 5 rows, the remaining 19 land in subsequent waves per §5.
* Q2 (D5 retry-then-skip) — Wave 2.
* Q3 (D7 dedupe rule) — **applied in this PR** (latest event_date, recorded_at tiebreaker).
* Q4 (E4 substrate) — Wave 4.
* Q5 (E7+E11 thresholds) — Wave 4.
* Q6 (engine LLM persona) — out of scope; future sibling spec.

## 5. Suggested sequencing (each row a PR or small bundle)

**Wave 1 — high-leverage data-lane (1 PR each, sequential):**
- D6 validation-suite-partial-failure cascade (the in-flight subagent `aaccb63575a618b0e` IS working on this — overlaps; this spec captures it but the subagent's PR may land first and partially-fulfill the row)
- D7 monotonicity dedupe stage
- D8 macro per-indicator re-pull
- D9 + D10 tier/classification targeted refresh

**Wave 2 — robustness (bundled PR):**
- D2 timeout-detection re-cascade
- D3 connection-drop re-invoke
- D5 provider-auth retry-then-skip
- D13 pool-exhaustion circuit-breaker

**Wave 3 — engine-lane (bundled PR or 2-3 PRs):**
- E1 scheduler stage retry
- E2 setup_detection panel-load retry
- E3 order placement retry-on-transient
- E9 engine import error wrapping

**Wave 4 — engine-lane behavioral (1 PR each):**
- E4 deferred-AAR substrate + queue
- E7 credibility-drop auto-pause
- E10 per-trade execution_risk skip
- E11 lifecycle-degradation auto-pause

**Wave 5 — observability (1 PR):**
- Sentinel test: every documented failure mode in §1+§2 has a deterministic recovery wired; LLM persona only invoked for shapes NOT in the catalog. Forcing-test that catches drift.

## 6. References

- PR #227 — cascade trigger
- PR #231 — smart-feed cascade (D1, D4)
- PR #233 — autonomous LLM data-recovery (the BACKSTOP layer)
- PR #235 — CSV archive R3 (D12)
- PR #236 — chunked force_refresh (D2 partial) + lane-service consolidation
- PR #239 — data_recovery_v2 (LLM persona — backstop layer)
- PR #163 — transient-DB retry (E2 partial)
- Memory: [[llm-triage-runs-local-on-max]], [[self-heal-autonomous-no-operator-task]], [[autonomous-lab-criteria-replaces-absolute-gate]]
- Catalogs an explicit DETERMINISTIC-FIRST design: every row in §1/§2 must land in the orchestrator BEFORE the LLM is invoked. The LLM only sees shapes that aren't in the catalog.
