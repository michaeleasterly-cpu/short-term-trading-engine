---
name: ref-carver-systematic-trading
description: "Robert Carver, Systematic Trading (Harriman House 2015) — design basis for the systematic_carver engine AND the cross-engine improvement reference operator wants applied when hardening reversion/vector/momentum/sentinel"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 9c826d9e-2d98-48e5-a010-5bcee9667a4d
---

**Robert Carver — *Systematic Trading: A unique new method for
designing trading and investing systems* (Harriman House, 2015).**
PDF: https://asset.quant-wiki.com/pdf/Robert%20Carver%20-%20Systematic%20Trading_%20A%20unique%20new%20method%20for%20designing%20trading%20and%20investing%20systems-Harriman%20House%20(2015).pdf

Engine name: **`carver`** (operator 2026-05-20: "now just the carver
engine" — NOT `systematic_carver`/`systematic-carver`; ECR file
`ecr_carver.txt`).

Two uses (operator 2026-05-20):
1. **Design basis for the new `carver` engine** (task #24,
   master step 4b): multiple simple forecasts (trend / value /
   mean-reversion) → equal-weight combine × diversification multiplier
   → volatility-targeted (Half-Kelly) position sizing → turnover speed
   limit. Carver's framing is explicitly an **anti-overfitting**
   answer: humble rules, realistic Sharpe expectations, strict cost
   control — aligns with [[project_ml_research_track]] (the DSR/
   n_trials overfit verdict) and the sacred-gate doctrine.
2. **Standing cross-engine improvement reference**: when revisiting
   reversion / vector / momentum / sentinel for improvements (master
   step 4b (i) — Lab candidates, never hand-tuned past the gate), use
   Carver's methods (forecast scaling to capped Sharpe units,
   diversification multiplier, vol targeting, speed limit, forecast
   combination) as the toolkit/idea source. Improvements still flow
   through the Lab → DSR/credibility gate → ECR (never bypass; every
   probe counts against the cumulative n_trials ledger).

**Operator framing of the reference set (2026-05-20)** — the
recommended books (this + [[ref-chan-algorithmic-trading]] + future
adds) focus on TWO things: (1) **explaining the trading environment**
— market structure / micro-structure and how everything
interconnects; (2) **a repeatable workflow** for collecting data →
analysis → finding trade ideas to automate. That autonomous workflow
**is what the LLM edge finder will do** — explicit **future roadmap**
(reinforces the SP-G/#242 autonomous-quant ambition in
[[research-llm-edge-discovery]]; surface at the SP-G design point).

Fetch the PDF (WebFetch) at design/spec time for the exact formulas
(forecast scaling constant, diversification-multiplier derivation,
volatility-target → position-size math, speed-limit enforcement) —
implement against the book, not from memory ([[feedback_use_official_docs]]).
