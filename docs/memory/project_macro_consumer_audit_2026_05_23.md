---
name: macro-consumer-audit-2026-05-23
description: "Exhaustive grep audit of macro-indicator consumers across the codebase (2026-05-23). Surfaces engines / Lab candidates / scripts / tests / validation checks that the financial expert MISSED when recommending PHCI drop + 'sentinel-only regime gate' treatment. Operator-requested verification per the verify-expert-verdict-in-codebase-first standing rule."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Snapshot 2026-05-23.** Audit ran `grep -rEn "\b(vix|yield_curve|credit_spread|hy_spread|industrial_production|initial_claims|sahm_rule|cfnai_ma3|sos_state_diffusion|macro_indicators|aaii_sentiment|fear_greed)\b"` across the whole repo (excl. `__pycache__`, `.venv`, `.git`, `data/`, `.claude/`). 718 hit-lines across 50+ files. Triggered by financial-expert recommendation to drop PHCI + treat 4 indicators as "sentinel-only regime gate" — verification surfaced HIDDEN consumers in vector, catalyst, lab, indicators, publishing, selfheal, providers, dashboard.

## Engines that consume macro

| Engine | Files |
|---|---|
| sentinel | sentinel/backtest.py, sentinel/models.py, sentinel/plugs/setup_detection.py, sentinel/tests/test_bear_score_byte_identical.py, sentinel/tests/test_macro_stress_gate_byte_identical.py, sentinel/tests/test_sentinel_plugs.py (expected; sentinel is the macro-regime defender) |
| reversion | reversion/backtest.py, reversion/plugs/setup_detection.py, reversion/regime_filter.py, reversion/tests/test_regime_filter.py (EXPERT MISSED — said macro is sentinel-only) |
| vector | vector/plugs/execution_risk.py, vector/plugs/lifecycle_analysis.py, vector/plugs/setup_detection.py, vector/tests/test_vector_plugs.py (EXPERT MISSED) |
| catalyst | catalyst/tests/test_lab_macro_expansion_byte_identical.py — Lab macro-expansion candidate; byte-identical sacred test (EXPERT MISSED — investigate what the Lab candidate consumes) |
| canary / momentum / carver | none |
| sigma (RETIRED) | archive/sigma/backtest.py — historical only |

## Lab / Research consumers

- tpcore/lab/llm_finder/snapshot.py — LLM edge finder reads macro snapshots for context
- tpcore/lab/llm_finder/tests/test_snapshot_assembler.py — sacred test
- scripts/probe_reversion_partial_axis.py — reversion probe uses macro
- scripts/probe_sentinel_macro_stress_gate.py — sentinel macro-stress probe (fear_greed etc.)
- tests/test_validation_failures_auto_cascade_wave1.py — cascade test
- tests/test_validation_vendor_late_and_chunked_cascade.py — cascade test

## Derived indicators (computation that depends on macro)

- tpcore/indicators/fear_greed.py — derived fear_greed indicator
- tpcore/fred/diffusion.py — sos_state_diffusion derived from 50 PHCI series
- publishing/stelib/stelib/indicators/fear_greed.py — published fear_greed indicator
- publishing/stelib/stelib/backtest/overfitting.py — overfitting analysis reads macro
- tpcore/backtest/overfitting.py — same

## Validation / self-heal

- tpcore/quality/validation/checks/macro_indicators_freshness.py
- tpcore/quality/validation/checks/macro_indicators_completeness.py
- tpcore/quality/validation/checks/aaii_sentiment_freshness.py
- tpcore/quality/validation/checks/fear_greed_freshness.py
- tpcore/quality/validation/checks/corporate_actions_completeness.py (SURPRISE — why does CA-completeness reference macro? Investigate.)
- tpcore/selfheal/probes.py, tpcore/selfheal/registry.py, tpcore/selfheal/spec.py

## Feed / provider / dispatch surface

- tpcore/providers.py — DFCR provider bindings for fred, aaii, fear_greed
- tpcore/feeds/profile.py — FeedProfile entries with cadence + freshness per feed
- tpcore/feeds/dispatcher.py, tpcore/feeds/publication.py — schedule dispatch
- dashboard_components/health.py — operator dashboard panels

## Per-indicator consumer map (the 4 the expert called "sentinel-only")

| Indicator | Consumers (engines + tpcore, excl. test/validation/adapter) |
|---|---|
| sahm_rule | sentinel (backtest, setup_detection) + reversion/regime_filter + lab/llm_finder + tpcore/backtest/filter_diagnostics |
| cfnai_ma3 | sentinel (backtest) + reversion/regime_filter + lab/llm_finder |
| industrial_production | sentinel (backtest, models, setup_detection) + tpcore/backtest/filter_diagnostics |
| initial_claims | sentinel (backtest, models, setup_detection) + tpcore/backtest/filter_diagnostics |
| sos_state_diffusion | sentinel bear-score Lab anchor (2 byte-identical tests) |
| vix / yield_curve / credit_spread / hy_spread | sentinel + (likely) vector + indicators — needs per-series breakdown |

## What this means for any future drop recommendation

Per [[verify-expert-verdict-in-codebase-first]]: BEFORE relaying ANY expert-recommended drop of a macro indicator, table, or derived series:

1. Grep with `\b<NAME>\b` across the WHOLE repo (not just tpcore/).
2. Check `*/tests/test_*_byte_identical.py` — sacred; cannot be invalidated without ECR.
3. Check `tpcore/lab/`, `scripts/probe_*`, `catalyst/tests/test_lab_*` for Lab candidates.
4. Check the `regime_filter.py` of every engine, not just sentinel.
5. Check `tpcore/backtest/filter_diagnostics.py` — broadly-shared filter library.
6. Check `tpcore/indicators/` — derived indicators that READ raw macro.

If the candidate-for-drop is referenced anywhere in the above buckets, the drop becomes a multi-PR project (ECR retirement, byte-identical test invalidation, derived-indicator amendment) — NOT a 5-minute cleanup. Surface that scope upfront.

## Investigation TODOs surfaced by this audit

1. catalyst/tests/test_lab_macro_expansion_byte_identical.py — what does the catalyst Lab macro-expansion candidate consume? Is it active or shelved?
2. corporate_actions_completeness.py references macro — why? Cross-validation predicate?
3. vector/plugs/execution_risk.py macro reads — is vector using macro for sizing? Halting? Per-plug breakdown.
4. publishing/stelib — separate project that imports our macro. Anything published from there?

## Related

- [[verify-expert-verdict-in-codebase-first]] — the standing rule that triggered this audit
- [[no-lazy-vendor-blame]] — sibling: don't accept generalized claims without per-item evidence
- [[investigate-dont-hand-wave-findings]] — sibling: query, don't adjective
- [[no-shortcuts-100-pct]] — sibling: this audit IS the 100% verification step
