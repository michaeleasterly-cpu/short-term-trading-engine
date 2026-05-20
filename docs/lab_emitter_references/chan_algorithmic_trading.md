# Chan — Algorithmic Trading (2013) — Lab Emitter Reference Bundle

**Bundle name:** `chan_algorithmic_trading`
**Status:** seed bundle for SP-G (operator-curated, opt-in via
`/lab-spec-emit --reference-bundle chan_algorithmic_trading`).
**Sibling bundle:** `carver_systematic_trading.md`.

This file is the operator-curated reference excerpt the SP-G emitter
sees when `--reference-bundle chan_algorithmic_trading` is named on a
`/lab-spec-emit` invocation. The agent embeds the text verbatim in the
`ReferenceExcerpt.text` field; no network fetch, no tools call, no
out-of-band lookup. Operator-staged by design — the LLM cannot pull
new references in (spec §3.2; Q3 operator decision).

---

## What Chan's framing brings to a Lab candidate

Chan's *Algorithmic Trading: Winning Strategies and Their Rationale*
(2013) is the standing strategy-design / cross-engine improvement
reference (`ref_chan_algorithmic_trading` project memory; sibling of
the Carver ref). For a Lab candidate emitter the most useful Chan
concepts are:

1. **Mean reversion vs momentum — regime conditioning.** Chan's
   stationarity / cointegration framing argues mean-reversion rules
   perform very differently in trending vs ranging regimes, and a
   regime filter often dominates parameter tuning. For an engine
   already in PAPER (reversion, vector), a Lab candidate exploring a
   regime filter is a higher-leverage proposal than a parameter sweep.

2. **Statistical-arbitrage pairs / cointegrated baskets.** A
   single-stock mean-reversion edge is fragile; a cointegrated basket
   (Engle-Granger, Johansen) often survives where a per-name rule
   does not. Note: the math (cointegration tests, vector error-
   correction models) lives in `statsmodels` / `linearmodels` — and
   per Q1, SP-G does NOT ship those (task #25 territory). The LLM
   can NAME these concepts in a rationale; it cannot RUN them.

3. **Kelly criterion / fractional-Kelly sizing.** Chan re-derives the
   Kelly fraction for return distributions with finite variance. The
   platform's risk path (`tpcore.risk`) owns sizing; a Lab candidate
   does NOT introduce a new Kelly sizing rule — Readiness §9 entry/
   exit / sizing / crash-guard / cost model are **unchanged**.

4. **Realistic backtest discipline — survivorship, look-ahead, data
   snooping.** Chan's chapter 3 is the canonical statement of why
   most "edges" in published research are artefacts. The platform
   takes this seriously: the survivorship-free `prices_daily`
   substrate (CLAUDE.md universal invariant), the held-back DSR with
   cumulative n_trials deflation (SP-A), and the lookahead-honest
   strictly-backward window discipline (Readiness §9) are all direct
   responses. The Lab emitter MUST acknowledge this — every emission
   spends ledger budget, and the gate is sacred.

5. **Out-of-sample testing — train/holdout, walk-forward.** Chan
   pushes walk-forward as the discipline. The Lab framework already
   uses walk-forward (`WalkWindowRecord` in `tpcore.lab.models`); the
   emitter does NOT propose a new evaluation scheme, it proposes a
   hypothesis that the existing walk-forward gate is the right
   evaluator of.

## How to use this when emitting a Lab candidate

- Where Chan's framing fits, use his vocabulary in the rationale:
  "regime conditioning," "stationarity / mean-reversion half-life,"
  "fractional Kelly," "walk-forward holdout discipline." Ground the
  rationale in operator-known doctrine.
- The Reversion engine is the natural target for Chan-flavoured
  candidates (mean reversion is Chan's home turf). The Vector engine
  is a natural fit for regime-conditioning hypotheses (Vector
  composite signals already do cross-sectional ranking; a regime
  filter on top is a one-toggle `fold_existing` candidate).
- The Catalyst engine is **not** a Chan-mean-reversion target —
  Catalyst is an event-driven engine (different mathematical
  regime). A Chan-framed mean-reversion candidate against Catalyst
  is suspect by default.

## What this bundle is NOT

- This is NOT the book; it is the operator's curated framing of the
  book for the Lab emitter. The full text is not reachable from the
  agent's sandbox.
- This is NOT a statistical-toolkit invocation. Chan's chapters that
  USE `statsmodels` (cointegration tests, Johansen) are knowledge the
  LLM can reference but NOT execute — SP-G ships the thin emitter
  only (per operator Q1 decision).
- This is NOT a directive to graduate at a relaxed bar. The
  cumulative DSR/credibility gate is sacred and is never relaxed for
  a candidate that invokes Chan's framing — see spec §2.3 + Readiness
  §6.

---

**Maintenance:** updates to this bundle are operator-curated. A
`PERSONA_VERSION` bump is not required for a bundle edit (the bundle
is data, not the persona); the persona is the LLM's behaviour
contract, the bundle is the operator-staged context.
