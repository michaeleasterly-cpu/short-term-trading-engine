---
name: identity-path
paths:
  - "tpcore/ingestion/**"
  - "tpcore/quality/validation/**"
  - "tpcore/auditheal/**"
  - "tpcore/selfheal/**"
  - "tpcore/data/**"
  - "platform/migrations/**"
  - "scripts/ops.py"
  - "reversion/**"
  - "vector/**"
  - "momentum/**"
  - "sentinel/**"
  - "canary/**"
  - "catalyst/**"
  - "carver/**"
description: "Path-scoped rule: identity-path discipline. Every prices / fundamentals / lifecycle write must prove `ticker + date → classification_id → CIK`. Engine readers must pass `as_of`. SEC-first for U.S. CIK-backed issuers; FMP fallback only where SEC cannot cover; FMP never overrides SEC identity without divergence handling."
---

# Identity-path (the 2026-06-02 audit's rule)

Canonical SoT: `docs/audits/2026-06-03-claude-code-workflow-controls.md` §13 #10 + `docs/audits/2026-06-03-identity-substrate-data-flow.md` (the case study).
Authoritative external: <https://code.claude.com/docs/en/memory>.

## Why this rule loads

This rule auto-loads when a diff touches any path in the frontmatter `paths:` glob — the lanes that participate in the **ticker + date → classification_id → CIK** identity chain. The 2026-06-02 identity-substrate audit named the read-side bypass (engines calling without `as_of`) and the FMP-overrides-SEC silent attribution as the two failure modes that shipped polluted data. This rule encodes the discipline that prevents both.

Coverage scope:

- **Ingestion + validators + audit-heal + self-heal + data lane** — the writer surface for prices / fundamentals / lifecycle.
- **Migrations** — schema changes that introduce or modify identity-bearing tables.
- **Engines (7 lanes)** — the reader surface; PricesRepo / lifecycle / classification consumers.
- **`scripts/ops.py`** — the stage entrypoint that drives ingestion + backfill.

## The discipline (3 invariants)

### 1. Prove the chain on every write

Any code path that writes to a `ticker`-bearing table (prices_daily, fundamentals_quarterly, ticker_history, ticker_classifications, lifecycle tables, etc.) must prove the **`ticker + date → classification_id → CIK`** path is intact:

- Either a `BEFORE INSERT` trigger auto-assigns `classification_id` (the canonical write-side mechanism — 15 such triggers exist as of 2026-06-02; verify the target table has one).
- Or the writer explicitly resolves identity via `IdentityDispatcher.ticker_to_classification_id(ticker, as_of)` (SCD-2 semantics) and stamps the `classification_id` on the row before insert.

A write that lands a row with `classification_id IS NULL` is a critical defect — surface it as `OPERATOR_DECISION_REQUIRED` per the `discovery-first` rule's CIC gate.

### 2. Engine readers must pass `as_of`

Every engine that reads prices / fundamentals / lifecycle data **must** pass an `as_of` date to the repo layer. The 2026-06-02 audit named PricesRepo bypass (callers omitting `as_of`) as the cause of cross-entity history contamination — old (ticker, date) pairs from delisted-then-reused tickers were read as if they belonged to the current entity.

Canonical caller pattern:

```python
# correct
bars = await prices_repo.get_bars(ticker, start, end, as_of=run_date)

# WRONG — silent cross-entity contamination
bars = await prices_repo.get_bars(ticker, start, end)
```

Any new caller without `as_of` is a critical defect.

### 3. Source-authority order

- **SEC is authoritative** for U.S. CIK-backed issuers. Form 25 / Form 15 lifecycle events, DEI metadata, insider transactions, fundamentals classification — all SEC-canonical.
- **FMP is fallback only** for entities SEC cannot cover (non-U.S., pre-IPO/post-delist gaps where SEC has no filing).
- **FMP must never override SEC identity without explicit divergence handling.** A code path that silently prefers FMP's classification over SEC's is a critical defect. If FMP and SEC disagree, route through `IdentityDispatcher`'s divergence resolution; do not silently merge.

## When the rule blocks (block by composing with discovery-first)

This rule is advisory text. The structural enforcement is the `discovery-first` rule's SWV + CIC gates — both of which auto-load on the overlapping path scope. The CIC gate's `change_type` enum has the relevant entries:

- `ingestion_or_backfill_change`
- `validator_or_gate_change`
- `database_schema_change`
- `database_data_repair`
- `engine_signal_change`

For any of these on an identity-path-scoped diff, the CIC gate must answer question #4 (readers), question #5 (upstream writer), and question #12 (evidence this is the correct layer) **citing the identity chain explicitly** — not generically.

## What this rule does NOT authorize

- Never write a row with `classification_id IS NULL` to a `ticker`-bearing table.
- Never add a new caller to PricesRepo / fundamentals reader without `as_of`.
- Never use FMP classification when SEC has a filing.
- Never edit `ticker_history` or `ticker_classifications` directly without an Alembic migration + ECR/DFCR path.

## Cross-links

- `.claude/rules/discovery-first.md` — the SWV + CIC gates that structurally enforce this discipline.
- `.claude/rules/migrations.md` — the "no new platform table without schema rationale" companion (controls-audit §13 #11).
- `.claude/rules/data-adapter.md` — the 6-stage contract for ingestion handlers.
- `.claude/rules/selfheal-auditheal.md` — the 100%-green-or-don't-trade invariant.
- `docs/audits/2026-06-03-identity-substrate-data-flow.md` — the failure case study + repair order.
- `docs/audits/2026-06-03-claude-code-workflow-controls.md` §13 #10 — the audit recommendation that produced this rule.
