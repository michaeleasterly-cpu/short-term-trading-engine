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

**⚑ Path B autonomous scale + ML-discipline preservation (operator
decision 2026-05-21, surfaced by Task #25 Path B reversal).** The
Task #25 Path B v1 spec
(`docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`)
flips the LLM edge-finder from human-gated (each PR / each ECR / each
retire) to autonomous (auto-undraft, auto-merge, auto-ECR ADD/MODIFY/
RETIRE, auto-retire on bleed/outcome violation). The ML-discipline-
at-scale verdict here (n_trials inflation is the binding constraint;
DSR deflation is the structural defense) is preserved at autonomous
scale through THREE NEW structural fences, NOT through operator-
gating:
1. **Regime-aware n_trials ledger.** Same hypothesis re-fired in a
   different market regime is a fresh trial against the regime axis;
   `cumulative_n_trials_by_regime` deflates the DSR gate WITHIN
   regime — defense against autonomous DSR-laundering by "novel
   regime" hypothesis-relabeling.
2. **Bleed-budget per finder-emitted PAPER engine.** $5,000
   structural max-bleed over the 30-session outcome window auto-
   retires the engine the moment it's hit — capital-destruction
   defense at autonomous scale.
3. **Provenance audit lane.** Every autonomous action writes a
   `LAB_FINDER_ACTION` row; the operator audits OUTCOMES via the
   `dashboard_components/finder_audit.py` dashboard, not each step.

The defensible uses 1 (meta-labeling) and 2 (cross-engine combiner)
remain v2.5 / v2.0 deferred in the Path B roadmap; this verdict's
low-DOF discipline is what binds the Path B toolkit
(`statsmodels` + `scipy.stats` only; no `arch` / `sklearn` /
`linearmodels` in v1; HAC-default OLS removes a footgun the Path A
spec accidentally invited).
