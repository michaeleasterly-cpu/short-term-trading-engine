# Carver — Systematic Trading (2015) — Lab Emitter Reference Bundle

**Bundle name:** `carver_systematic_trading`
**Status:** seed bundle for SP-G (operator-curated, opt-in via
`/lab-spec-emit --reference-bundle carver_systematic_trading`).
**Sibling bundle:** `chan_algorithmic_trading.md`.

This file is the operator-curated reference excerpt the SP-G emitter
sees when `--reference-bundle carver_systematic_trading` is named on a
`/lab-spec-emit` invocation. It is a single markdown blob; the agent
embeds the text verbatim in the `ReferenceExcerpt.text` field of the
`EmissionContext` it ships to the LLM. The LLM cannot fetch new
references itself — no `tools`, no network beyond the Anthropic SDK
call. This is the operator's hand on the wheel for what reference
material the emitter sees this cycle (spec §3.2; Q3 operator decision).

---

## What Carver's framing brings to a Lab candidate

Carver's *Systematic Trading* (2015) is the design basis the operator
named for the eventual `carver` engine (task #24) and for the
cross-engine improvement toolkit (`ref_carver_systematic_trading`
project memory). For a Lab candidate emitter the most useful Carver
concepts to keep in mind are:

1. **Forecast diversification across uncorrelated rules.** The
   Carver framing emphasises a portfolio of forecast rules whose
   bottlenecked-correlation profile is the dominant driver of
   risk-adjusted return, not any individual rule's edge. For the Lab
   front-half this argues against single-rule "magic indicator" Lab
   candidates and for `fold_existing` candidates that re-tune an
   existing engine's existing rule structure.

2. **Volatility scaling on EVERY position.** Carver positions are
   sized to a target volatility contribution; a Lab candidate proposing
   to change sizing must do so via the engine's existing sizing path
   (per the project's existing risk-management contracts in
   `tpcore.risk`), never by introducing a new bespoke size rule.

3. **Slow rules dominate fast rules at retail capital.** Carver's
   horizon analysis argues a daily-timeframe engine (the project's
   single timeframe) is better served by rules with multi-month
   look-backs than by short-window momentum. A Lab candidate proposing
   a sub-monthly window for a rule that was multi-month is suspect by
   default (the historical record on this is brutal — re-read pp.
   ~140-160).

4. **The "speed limit" — diversification ceiling.** Adding a 21st
   rule that correlates 0.9 with the existing 20 is a multiple-testing
   pollutant, not an edge. The SP-A n_trials ledger encodes this
   structurally: every candidate strictly tightens the cumulative
   gate. The Lab emitter MUST respect that the gate is sacred (spec
   §2.3); it does NOT relax DSR/credibility to "make room" for a new
   rule.

5. **Honest cost modelling — bid/ask, slippage, financing.** A Lab
   candidate that claims an edge that disappears under realistic
   transaction costs is not an edge. The platform's cost model is in
   the existing engine backtest path; the variant changes WHICH names
   are selected/scored, not the trade machinery (Readiness §9 — entry/
   exit mechanics, sizing, crash-guard, cost model **unchanged**).

## How to use this when emitting a Lab candidate

- Prefer `fold_existing` over `promote_new` for the early-cycle
  emissions: the existing engines (reversion, vector, momentum,
  sentinel, catalyst) are the substrate; one well-chosen re-tune is
  worth ten new-engine proposals.
- Frame the hypothesis in Carver's vocabulary where it fits:
  "diversification of forecast rules," "volatility-targeted sizing,"
  "horizon mismatch," "rule correlation ceiling." This keeps the
  rationale ground in operator-known doctrine instead of inventing
  bespoke language.
- The single primary metric MUST be one the engine already declares
  on its `LAB_TARGET.primary_metric` (SP-D). For Sentinel that is
  `MAXDD_REDUCTION`; for Vector/Reversion/Momentum it is `SHARPE`. A
  candidate proposing a metric the engine does NOT declare is a
  category error — STOP and route through SP-D, never silently invent.

## What this bundle is NOT

- This is NOT the book; it is the operator's curated framing of the
  book for the Lab emitter. The full text is not reachable from the
  agent's sandbox.
- This is NOT statistical-toolkit code. SP-G ships the thin emitter
  only (no `statsmodels` / `arch` / `linearmodels` / `scikit-learn`);
  Carver's volatility / correlation maths is deferred to task #25
  (the richer autonomous-quant follow-on epic, per operator decision
  Q1).
- This is NOT a directive to the LLM. It is a reference excerpt the
  LLM may consult to ground its rationale; the deterministic gate
  decides whether the resulting candidate graduates.

---

**Maintenance:** updates to this bundle are operator-curated. A
`PERSONA_VERSION` bump is not required for a bundle edit (the bundle
is data, not the persona); the persona is the LLM's behaviour
contract, the bundle is the operator-staged context.
