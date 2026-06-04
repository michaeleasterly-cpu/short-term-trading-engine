---
name: ml-research-track
description: "Expert verdict on adding ML to the platform — ML attacks the wrong constraint (inflates DSR n_trials); only meta-labeling / cross-engine combiner defensible. Research-track epic, not now."
metadata:
  node_type: memory
  type: project
  originSessionId: e4b282f8-c3bf-497d-9609-6eed7b7ec5cf
---

Operator asked (2026-05-17) "can we add ML and how would it benefit
us? ask an expert." Senior-quant expert verdict:

**Central tension:** the binding constraint is DSR ≥ 0.95 /
credibility ≥ 60 — a *multiple-testing-deflated* bar. ML is a
degrees-of-freedom multiplier; its hyperparameter/feature/architecture
choices inflate `n_trials` in `_expected_max_sharpe_under_null`
(`tpcore/backtest/overfitting.py`), which *raises* the bar all 4
engines already fail. ML can't add information not in daily OHLCV +
quarterly fundamentals — it fits noise tighter (what DSR/PBO catch).

**The only defensible uses (narrow, low-DOF, advisory):**
1. **Meta-labeling** (López de Prado AFML ch.3) in `lifecycle_analysis`
   / `execution_risk` — keep the rule as the side signal, a fixed-
   hyperparam shallow classifier predicts P(win) to gate/size. Does
   NOT inflate the host engine's `n_trials`. Highest-EV ML option.
2. **Honest cross-engine portfolio combiner** in the allocator
   (NNLS / risk-parity on the 4 weakly-correlated edge streams) —
   cleanest math path to a portfolio that clears the bar no single
   engine does. Barely "ML".
3. Regime classification (sentinel) — small, interpretable.

Deep learning: negative ROI on single-Mac infra, don't. Keep ML
advisory UPSTREAM of `RiskGovernor.check_trade()`, never replacing it.

**Honest null (expert's actual recommendation):** skip ML; spend the
effort on a genuinely NEW orthogonal signal (SEC-filing text features,
accruals from existing tables) or the cross-engine combiner —
information, not model flexibility, is the missing ingredient.

**Gating if pursued:** count FULL hyperparameter grid (incl. abandoned
runs) into `n_trials`; CPCV not single OOS; purge+embargo ≥ holding
period; meta-model trial budget tracked separately from host engine;
no live graduation without rolling-refit paper walk-forward.

**Status:** research-track epic, its own brainstorm→spec→plan cycle.
NOT folded into the event-driven engine epic (A/B done, C executing,
D pending). Relates to [[engine-sdlc-lifecycle]] (a meta-label/combiner
would be a new lifecycle-managed component) and the DSR/credibility
gate that is the platform's binding constraint.
