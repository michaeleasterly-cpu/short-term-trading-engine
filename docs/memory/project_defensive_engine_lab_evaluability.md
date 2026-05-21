---
name: defensive-engine-lab-evaluability
description: "Sentinel + future defensive engines are structurally Lab-unevaluable in non-recessionary holdouts — the sacred `DSR≥0.95 ∧ n_trades≥3` gate is built for active engines. Open strategic question with 3 honest paths; pending operator decision before any further Sentinel Lab spend."
metadata: 
  node_type: memory
  type: project
  originSessionId: 869ca3ee-c182-4698-af5f-67c6a0479e21
---

**The structural problem.** The sacred Lab gate (`DSR ≥ 0.95 ∧ credibility ≥ 60 ∧ n_trades ≥ 3`) is calibrated for *active* engines that produce many trades over a 2-year holdout. Defensive engines (Sentinel today; future engines like a tail-hedge sleeve) are designed to **fire rarely but decisively** — literature-anchored thresholds (Sahm rule ≥0.50, CFNAI-MA3 ≤−0.70, SOS state-diffusion ≥0.20) intentionally don't trigger outside genuine recessions. In a clean post-2020 holdout (no recession), the correct engine behaviour is `n_trades = 0` (hold cash / no defensive basket entry). This **correctly-dormant** state crashes every Lab metric: DSR collapses to 0, MAXDD_REDUCTION is unmeasurable (no drawdowns to reduce), credibility = 40.

**Empirically verified 2026-05-21:** the `sentinel_bear_score` candidate FAILED. Offline distribution probe (PR #220) confirmed OOS p95 = 0.237 on the 2024-2025 window — composite never reaches the 0.45 LIGHT floor; 100% DORMANT across 872 holdout days. Across the FULL 2018-2025 window (which includes 2020 COVID + 2022 inversion), the gate fires only 3.8% of the time. Indicator coverage is healthy (NOT a data-availability defect). **The composite design is correct; the Lab evaluation methodology is wrong for this engine class.**

**Three honest paths forward (operator decision pending):**

1. **Validation-by-construction.** Declare defensive engines exempt from the Lab gate. Their threshold anchors come from external literature (Sahm 2019, Chicago Fed CFNAI, Crone-Clayton-Matthews 2005, Estrella-Mishkin 1998); the design is published-academic-method, not curve-fit. **No further `lab_trial_ledger.sentinel` spend.** Document the exemption + audit trail.

2. **SP-D pluggable-metric extension.** Add a second gate path for defensive engines: `MAX(DSR_with_trades_normal_path, equity_protection_score_dormant_path)`. The dormant path scores "did the engine correctly stay out when there was no signal" by comparing the live-path equity curve to a forced-active counterfactual. Requires SP-D extension + a per-engine-class gate-routing field on `EngineProfile`. Heavy-lane.

3. **Recessionary holdout window override.** Allow `--train-start` / `--holdout-start` overrides on `python -m ops.lab` to explicitly test against 2007-2010 (Great Recession) or 2020 Q1-Q2. The gate has signal to evaluate. Trade-off: holdout-window cherry-picking is itself an n_trials laundering risk if used carelessly — needs a single pre-registered window per candidate, not a sweep.

**`lab_trial_ledger.sentinel` standing.** First-ever production Lab run was today's `sentinel_bear_score` (40 trials). Every subsequent Sentinel probe faces a strictly harder DSR-deflated gate. So a second probe (e.g. the sibling `sentinel_maxdd` candidate that exists but hasn't been probed) is **NOT cheap** even if the candidate is design-sound — the cumulative deflation makes the gate strictly harder, and the same zero-trade issue likely recurs.

**Defect ref:** `[defect_ref: SENTINEL-ACTIVATION-DORMANT-2026-05-21]` — REVIEW_DEFECT_LOGGED + matching TODO.md tag. Resolution is the strategic decision above, not an engine-code fix.

**Related:** [[master-remaining-program]] §SIDE-EPIC 2026-05-21 for the full deep-research sweep outcome (4/5 candidates FAILED honestly). [[no-shortcuts-100-pct]] for the "red is red" discipline that produced the honest verdict instead of weight-tweaking.
