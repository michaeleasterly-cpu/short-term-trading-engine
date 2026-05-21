# Passover — Data Validation Failures (2026-05-21 15:14 UTC)

**From:** session 869ca3ee (Task #25 run-everything cadence — was investigating)
**To:** the active data-validation session
**Status:** stopping investigation — handing off the findings

Surfaced via `python scripts/ops.py --check --pretty` during the run-everything-to-find-bugs cadence.

## §1 The 9 RED data_validation checks (gates DATA_OPERATIONS_COMPLETE)

From the last daemon-progress run at 2026-05-20 21:30 UTC (= 2026-05-21 05:30 Manila local):

1. `fundamentals_quarterly_completeness`
2. `corporate_actions_completeness`
3. `earnings_events_monotone`
4. `sec_insider_monotone`
5. `liquidity_tiers_completeness`
6. `ticker_classifications_coverage`
7. `macro_indicators_completeness`
8. `fear_greed_freshness`
9. `aaii_sentiment_freshness`

**Per the master-program memory:** liquidity_tiers + ticker_classifications are honestly **non-healable** (derived/recomputed shape; per `project_master_remaining_program.md` SIDE-EPIC P1 follow-ons). The other 7 are healable HealSpecs.

## §2 `consolidated_daemon_topology` VIOLATION — 5 unexpected launchd labels

Beyond the closed whitelist (engine-service + lane-service + data-operations):

- `com.michael.trading.allocator`
- `com.michael.trading.allocator-heartbeat`
- `com.michael.trading.llm-triage-service` *(should have been consolidated into lane-service per DA-3)*
- `com.michael.trading.momentum-oneshot-2026-05-27` *(one-shot leftover?)*
- `com.michael.trading.pipeline-smoke-test` *(test leftover?)*

These are likely launchd entries from past sessions that didn't unload cleanly. `launchctl unload` + delete the .plist files. The `consolidated_daemon_topology --check` probe will go GREEN once removed.

## §3 Last `--update` daemon run (2026-05-20 21:30 UTC)

| Stage | Status | Duration |
|---|---|---|
| daily_bars | **FAILED** | 521s (possible timeout — 120s is the hard cap; this hit 521s = stages timeout) |
| reconcile | OK | 2.8s |
| greeks_max_pain | **FAILED** | 4s (probably auth/api) |
| data_validation | **FAILED** | 53s (the 9 reds above) |
| forensics | OK | 2.8s |

**`workflow_complete=False`** → `DATA_OPERATIONS_COMPLETE` correctly NOT emitted (the structural 100%-green invariant held — no fake green). Self-heal didn't recover the failures.

## §4 16 critical errors in `recent_errors`

Sample:
- `ops.INGESTION_FAILED`: `"data_validation failed: validation suite failed: [the 9 checks]"` × 3 occurrences (14:19, 13:04, etc.)
- `ops.SHUTDOWN` exit_code=1 × multiple
- (per the operator's earlier note: this session itself produced one of these via the failed gate-pilot run at 15:14 UTC; that's the SAME ops shutdown error in the list — not a separate bug)

## §5 What I did NOT do

I did NOT proceed past the `--check` discovery. Operator interrupted — said the other session is handling data-validation work. So I'm NOT:
- Running the full validation suite directly
- Investigating per-check root causes
- Filing defect_register entries
- Issuing HealSpec invocations

That's all the receiving session's call.

## §6 Other-session next-action menu (not prescriptive)

- Run `await run_suite(pool)` from `tpcore.quality.validation.suite` to get per-check `reason` + `details`
- Cross-reference each red against `tpcore.selfheal.registry.HEAL_SPECS` to see what auto-heal can attempt
- For the 2 honestly-non-healable (liquidity_tiers + ticker_classifications): operator-intervention path
- Clean up the 5 daemon-topology violations via `launchctl unload` + plist delete
- Diagnose daily_bars 521s timeout — probably hitting Postgres statement_timeout on a wide query

## §7 Where session 869ca3ee is moving next

Picking a NON-overlapping component from the run-everything backlog (TODO L499). Likely candidates: `ops.engine_service` (DA-3 daemon, no data-overlap) or `run_all_engines.sh` (engine path, market-hours-sensitive so possibly deferred).
