---
name: feedback-coverage-gate-denominator-matches-engine-universe
description: "Coverage-gate denominators must equal the universe the engines actually trade (strategy-eligible), not the ingest firehose or an instrument-type taxonomy. Gate on tradability, not on instrument type."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-25, two-PR sequence #370 + #374):**
when building or fixing a data-completeness coverage gate, the
**denominator must equal the universe the engines actually trade** —
the same predicate the per-ticker completeness invariant already
enforces. Do not invent a parallel universe definition; do not gate
on instrument-type taxonomy; do not measure coverage over the ingest
firehose.

For `platform.prices_daily` today, the canonical strategy-eligible
predicate is:

  `tc.asset_class = 'stock' AND lt.tier <= TRADEABLE_TIER_MAX AND
   (tc.lifetime_end IS NULL OR tc.lifetime_end > pd.date)`

(matches `prices_daily_completeness._LIQUID_UNIVERSE_SQL`). Asymmetric
engine universes (Sentinel's defensive-ETF basket) get their OWN
critical-ticker check at 100%, not a unified gate.

**Why (failure mode this prevents):**

  - **Wrong axis.** I tried (in spec `006_strategy_eligible_...json`)
    to gate on a 10-class instrument-type taxonomy (SPAC_common /
    SPAC_unit / SPAC_warrant / ADR / foreign_ordinary / closed_end_fund
    / warrant / right / unit / preferred / common_stock). Two experts
    (db-architect + finance) converged on REJECT: coverage gates gate
    on *tradability* (universe + liquidity), not *instrument type*.
    Instrument type is a one-time universe-construction decision made
    at strategy design, not a per-cycle gate parameter.

  - **Quant-finance precedent.** Russell/S&P/CRSP survivorship-bias-
    free academic datasets exclude pre-merger SPACs (trust-money
    proxies bounded ≈ $10 + T-bill carry — including them manufactures
    a fake mean-reversion edge that vaporizes post-de-SPAC). The
    `asset_class='stock'` filter handles this cleanly without a
    bespoke SPAC sub-typing column.

  - **`operator_decision_required` buckets are incompatible with
    daily-cycling gates.** A gate that runs every data-ops cycle
    cannot pause for human input — universe-construction decisions
    must be pre-resolved.

  - **Country-as-exclusion is forbidden** (operator hard rule).
    Cayman SPACs and China-VIE ADRs trade on US exchanges in USD
    under Reg NMS — no tradability difference. Regulatory-risk
    concerns belong at position-sizing, not data-coverage layer.
    Universe-membership decisions (which Level II/III ADRs to
    include) happen at the `liquidity_tiers` curation layer.

**How to apply:**

  - Any new completeness/coverage check for a `platform.*_daily`-class
    table: start by asking "what's the strategy-eligible universe for
    the engines that consume this table?" — then look up the predicate
    that's already in the corresponding `_completeness.py` check.
    Reuse the constant (e.g., `TRADEABLE_TIER_MAX`) rather than
    redefining.

  - Asymmetric engine universes (an engine that trades a non-stock
    asset class): give that engine its own critical-ticker check at
    its own threshold; do NOT mix into the global gate.

  - Reject any spec that introduces `operator_decision_required`,
    multi-class instrument taxonomy, or country/incorporation filtering
    in a coverage gate. Push back BEFORE implementing.

  - Live-DB validate before pushing: run the new query against the
    actual data and confirm the latest-vs-trailing ratio reflects the
    intended semantic. PR #374 caught its own bug-class this way:
    PR #370's "active-universe" denominator failed 2026-05-22 at
    97.68% (177-ticker firehose gap), but PR #374's strategy-eligible
    denominator passed at 99.80% (1,488 / 1,491) — the gap was almost
    entirely in non-tradeable names the engines don't touch.

**Related:**
- [[feedback_ask_expert_then_execute]] — expert verdict precedent
- [[feedback_authorization_via_expert_keep_moving]] — gate authority
- [[feedback_apply_my_own_documented_constraints]] — meta-rule that
  this entry must be read BEFORE the next coverage-gate touch
