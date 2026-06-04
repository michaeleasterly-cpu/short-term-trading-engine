---
name: db-is-substrate-not-engine-inputs
description: "The database is research substrate first, engine inputs second — completeness > current-usage. Don't retire a feed just because no engine reads it today. Removing TRUE redundancies (same source, multiple wrappers) is fine."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

⚑ **Standing rule (operator 2026-05-25):** the database is for more than just the engines. The operator wants research-level completeness in the substrate, not minimalism limited to current engine consumption.

**Why:** Research lives in the substrate. A FRED series, an AAII sentiment column, a Tradier options chain, a fear/greed index — these are valuable IN THE TABLE regardless of which engine reads them today. A future engine, a Lab probe, or an autonomous edge-finder can read what's already there. Retiring a feed because "no current engine consumes it" is a research-impoverishment move.

**The carve-out:** TRUE redundancies are fine to remove. Two API paths writing the SAME source data to the SAME-or-equivalent table is redundant. That's what P0_3 was — FMP `/stable/insider-trading/search` was a wrapper around SEC EDGAR Form 4; the underlying source was the same. SEC bulk-Form-4 → `insider_transactions` → `insider_mspr_daily` covers the same data more directly. The retirement removed an API wrapper, not a data source.

**How to apply:**

- **Before proposing any RETIRE / DROP of a feed, ProviderBinding, or table**: confirm it's a TRUE redundancy (same source, alternative covered). If it's the ONLY path for that data — even if no engine consumes it — preserve.
- **Macro indicators specifically**: `platform.macro_data` carries 134K rows across 1985–2026, 14+ series_id values. Many are not currently consumed by engines. They stay. ([[macro-consumer-audit-2026-05-23]] for the consumer survey.)
- **Examples of substrate-preserve targets** (NOT engine-driven): FRED macro series with no current consumer, AAII sentiment columns, fear/greed, Tradier options chains, ApeWisdom social sentiment, FINRA short interest, iborrowdesk borrow rates, all of `corporate_actions`, all of `tradier_options_chains`.
- **Examples of legitimate redundancy retirement** (P0_3 model): same upstream source, two ingest paths, one substrate. Drop the redundant ingest path; the source isn't lost.
- **When unsure**, ask the operator before retiring. The cost of an unnecessary retirement is higher than the cost of a one-line clarifying question.

Related:
- [[macro-consumer-audit-2026-05-23]] — the 718-hit-line audit showing macro is consumed broadly, but the substrate carries more than the audit surface.
- [[apply-my-own-documented-constraints]] — the meta rule.
- [[publishing-intent-stelib]] — operator-intended-outbound research artifacts; same substrate-philosophy applies.
