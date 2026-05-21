---
name: ref-chan-algorithmic-trading
description: "Ernest Chan, Algorithmic Trading: Winning Strategies and Their Rationale (Wiley 2013) — standing strategy-design / cross-engine improvement reference (mean-reversion, momentum, factor, overfit cautions)"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 9c826d9e-2d98-48e5-a010-5bcee9667a4d
---

**Ernest P. Chan — *Algorithmic Trading: Winning Strategies and Their
Rationale* (Wiley, 2013).**
PDF: https://asset.quant-wiki.com/pdf/Algorithmic%2520Trading_%2520Winning%2520Strategies%2520and%2520Their%2520Rationale-Wiley%2520%25282013%2529.pdf

Operator-supplied 2026-05-20 as a standing reference (sibling to
[[ref-carver-systematic-trading]]). Covers mean-reversion (pairs /
cointegration / Bollinger), momentum (time-series & cross-sectional),
factor/seasonal strategies, and — critically for this platform — the
**rationale + failure-mode/overfitting** discussion behind each.

How to apply: a toolkit/idea source when (a) building new engines
(e.g. [[ref-carver-systematic-trading]] / systematic_carver, task #24)
and (b) the master-step-4b cross-engine improvement work on
reversion / vector / momentum / sentinel. Every idea still flows
through the Lab → DSR/credibility gate → ECR; every probe counts
against the cumulative n_trials ledger; never bypass the gate
([[project_ml_research_track]], [[project_lab_front_half_epic]]).
Fetch the PDF (WebFetch) at design/spec time and implement against the
book's exact formulation, not from memory ([[feedback_use_official_docs]]).
