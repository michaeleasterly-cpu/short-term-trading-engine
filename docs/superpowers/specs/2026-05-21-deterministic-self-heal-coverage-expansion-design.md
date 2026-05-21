# Deterministic Self-Heal Coverage Expansion (design spec)

**Status:** PROPOSED — awaiting operator scope-approval.
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
| D11 | Freshness vendor_late | `latest_published` probe returns ≤ our_latest | Memory-documented selfheal.vendor_late event; partial | **EXTEND:** orchestrator-level recognition + skip-without-failing for known weekly-publish feeds (AAII Thursday, fear_greed daily) |
| D12 | CSV archive substrate dead (Railway) | `write_archive` raises filesystem error | None | **DONE-VIA-#235** R3 substrate is env-pluggable; fails over to S3 backend |
| D13 | Postgres pool exhaustion | asyncpg PoolTimeout | None | **NEW:** circuit-breaker stage timeout that closes idle conns + re-opens pool, then retries the stage |

**Coverage today:** 3/13 (D1, D4, D12 fully; D2/D3/D11 partially). **Coverage after this spec:** 13/13 deterministic; LLM is the long-tail.

## 2. Engine-lane deterministic self-heal — failure mode catalog

Engines (reversion, vector, momentum, sentinel, canary, catalyst, carver) all run through the same scheduler/plug architecture. Failure modes:

| # | Failure shape | Detection signal | Current deterministic recovery | Proposed deterministic recovery |
|---|---|---|---|---|
| E1 | Engine scheduler stage failure | `engine_service` stage retval non-OK | None — log + continue | **NEW:** retry once with same params; if still fails, emit `ENGINE_STAGE_ESCALATED` and skip cycle (don't abort engine_service) |
| E2 | setup_detection panel-load failure | DB-fetch exception | None | **NEW:** transient-DB retry pattern (PR #163 mirror) — 3 attempts with exponential backoff |
| E3 | Order placement failure | Alpaca API error during order submit | RiskGovernor blocks on hard rejects | **EXTEND:** retry ONCE on transient Alpaca network error; on second failure, mark engine_position degraded + emit `ORDER_ESCALATED` |
| E4 | AAR write failure | `aar_logging` plug exception | None | **NEW:** defer AAR to next cycle's deferred-AAR queue (new table `platform.aar_deferred`); don't fail the engine cycle |
| E5 | Capital gate failure | `assert_passed_for_engine` raises | Skip cycle (existing behavior) | **DONE** — current behavior is correct |
| E6 | Drawdown breach | engine PnL vs RiskGovernor breach threshold | RiskGovernor auto-pauses engine (existing) | **DONE** |
| E7 | Credibility drop | post-cycle `write_credibility_score` < threshold | None — Lab/AAR captures it, but no auto-action | **NEW:** if credibility < threshold for N consecutive cycles, auto-emit `ENGINE_CREDIBILITY_DROP` event + RiskGovernor pauses the engine pending operator review |
| E8 | Stale-order accumulation | `stale_order_cancel` finds orders past TTL | Auto-cancel stale orders (existing) | **DONE** |
| E9 | Engine package import error | scheduler raises ImportError | None — daemon crashes | **NEW:** wrap engine-imports in try/except; emit `ENGINE_IMPORT_FAILED` event + skip the engine for the cycle (don't crash engine_service) |
| E10 | Per-trade execution_risk failure | `execution_risk` plug raises mid-cycle | None | **NEW:** cancel any in-flight orders for the trade, emit `EXECUTION_RISK_ESCALATED`, skip the trade (not the whole cycle) |
| E11 | Lifecycle analysis stale | `lifecycle_analysis` reports degradation | None — captured by AAR but no auto-action | **NEW:** if lifecycle score < threshold for N cycles, RiskGovernor pauses the engine |

**Coverage today:** 3/11 (E5, E6, E8 fully; E3 partially). **Coverage after this spec:** 11/11 deterministic.

## 3. What this spec is NOT

- NOT a build PR. No code in this PR.
- NOT a plan PR. No file-by-file implementation sequence.
- NOT a one-PR build — each row (D1-D13, E1-E11) is its own bounded PR or small bundle. Sequenced in §5.
- NOT touching the LLM-side personas — those stay as the long-tail backstop. Deterministic-first is the design; LLM-second is the safety net.

## 4. Operator scope-decision questions

Before plan PRs:

1. **All 24 rows in scope, or a subset?** (Default: all 24; sequenced per §5.)
2. **D5 provider auth — operator-side action (rotate creds) or auto-retry-then-skip-cleanly?** (Recommendation: the latter — daemon stays alive; operator sees the escalation in application_log.)
3. **D7 dedupe rule for monotonicity** — keep latest-inserted, latest event_date, or first-seen? (Recommendation: keep latest event_date with tiebreaker on latest-inserted — the row most likely to be a vendor restatement.)
4. **E4 deferred-AAR substrate** — new `platform.aar_deferred` table or fold into `platform.application_log`? (Recommendation: new table; AAR has structured fields that don't fit a generic event-log.)
5. **E7 + E11 N-consecutive-cycles thresholds** — pick numbers (e.g. N=3 for credibility, N=5 for lifecycle). Operator-tunable later.
6. **Engine-lane v2 LLM-triage persona** — same v2-style pattern catalog as data_recovery_v2.md, separate file. In scope here or separate spec? (Recommendation: separate spec, this one is deterministic-only.)

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
