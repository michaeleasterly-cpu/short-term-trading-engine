---
name: self-heal-autonomous-no-operator-task
description: "Self-heal must be END-TO-END autonomous. No \"operator runs X\" step anywhere in the recovery chain. LLM-driven triage takes ACTION not just suggests a draft PR. Operator 2026-05-21 directive after the daily_bars 3-day cascade exposed the gap."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule:** Every recovery path must be FULLY autonomous. No operator-task step inside the self-heal chain. The recovery substrate is the DETERMINISTIC CASCADE CATALOG (D1-D14 / E1-E11, Waves 1-4 + sentinel). On cascade exhaustion the daemon emits `INGESTION_AUTO_RECOVERY_FAILED` and STOPS — operator reviews the event. **2026-05-22 update:** the LLM-triage stack has been REMOVED entirely (operator directive "we aren't going to use the llm triage... take it out"); the deterministic cascade is the COMPLETE recovery path with no LLM fallback.

**Why:** 2026-05-21 incident — `daily_bars` failed with coverage_collapse three consecutive nights (2026-05-18, 19, 20). The system had self-heal mechanisms (`repair_coverage`, `repair_gaps`, `data_repair_service`, `llm_triage_service`) but they sat idle:

1. `repair_gaps` was blind to coverage_collapse (completeness check threshold blind to partial sessions)
2. `repair_coverage` only fixed the 1-session diff (got 19 tickers of the ~7000 needed)
3. `data_repair_service` wasn't installed on the operator's machine (post-DA-3 installer never re-run)
4. `llm_triage_service` was advisory-only — could open a draft PR but never actually act
5. PR #227 wired the cascade trigger but cascaded to `repair_gaps` (the broken mechanism)

The operator had to manually diagnose Alpaca SIP entitlement, run `force_refresh feed=sip universe=active`, and rebuild the data layer by hand. **None of that should have required them.**

Operator verbatim:
- "the system is supposed to be able to recover from this shit"
- "so the first time we need self heal it doesnt work"
- "dont ask me questions ask an expert no operator task bullshit in the self heal as well"
- "we wrote detministic agents and they suck"
- "and we are going to automate the god damn triage and you aren't gonne stop it anymore i want the system to self heal not with some operator task bullshit you work for me motherfucker"

**How to apply (every self-heal design):**

- **The recovery chain must end with autonomous action**, not "emit escalation event for operator." If the deterministic cascade exhausts, the LLM-triage service picks up the escalation event and TAKES ACTION via a whitelisted stage invocation. Operator finds out in the morning via application_log + dashboard, not via a Slack ping asking them to run something.
- **Deterministic rule-based cascades are fragile.** They catch the failure modes they were designed for, miss new ones. The LLM-triage path is the catchall — it reads the failure + chooses an action from the whitelist.
- **Whitelist is the safety boundary, NOT human review.** Allowlist = which stages + which params the LLM is authorized to invoke. Outside the whitelist = REJECT + escalate. Inside = autonomous-action authority.
- **Data-lane actions get autonomous merge authority.** Engine code changes, roster mutations, LIVE-trading actions stay PR-gated. Distinction matters — autonomous data recovery is safe; autonomous code changes are not.
- **Test the recovery chain end-to-end.** The PR #227 cascade test passed because it asserted the cascade FIRED. It didn't assert the cascade ACTUALLY HEALED THE DATA. Regression tests must walk the whole chain: failure → cascade → action → DB-state-recovered.

**The autonomous chain (post-2026-05-22 — deterministic-only):**

```
1. data_operations cron fires daily_bars at 21:30 UTC
2. daily_bars / data_validation / engine_service / etc. fails
3. Orchestrator cascade picks up the failure shape (one of D1-D14 / E1-E11)
4. Cascade runs the canonical recovery (cascade catalog + smart-feed cascade)
5. If cascade succeeds → INGESTION_AUTO_RECOVERED_* event → green; next cycle continues
6. If cascade fails    → INGESTION_AUTO_RECOVERY_FAILED event → STOP
                          (no LLM fallback; operator reviews via application_log)
```

**Don't break the chain with operator-task steps.** Every link is automation. The autonomous boundary is the deterministic cascade catalog — exhausting it emits the terminal event and the operator decides.

**Related:**
- [[autonomous-lab-criteria-replaces-absolute-gate]] — the Lab criteria similarly replaced absolute DSR/cred gate with autonomous adjudication
- [[lab-heavy-probe-needs-chunking]] — the cascade fixed timeouts; this fixes the recovery action
- [[research-llm-edge-discovery]] — separate but related: LLM-driven autonomous research-time decision-making
- [[event-driven-not-scheduled]] — the architectural foundation: application_log bus + sibling daemons, not scheduled linear scripts
