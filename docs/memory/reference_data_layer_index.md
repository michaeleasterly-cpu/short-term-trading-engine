---
name: reference_data_layer_index
description: On ANY data-layer task read docs/DATABASE_AND_DATAFLOW.md §0 FIRST — the START-HERE index mapping every canonical data source.
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

The data-layer START-HERE index lives at `docs/DATABASE_AND_DATAFLOW.md` **§0** (added 2026-06-03). On any database / ingest / validation / identity / backfill / repair / audit task, read it FIRST — do not rediscover sources piecemeal.

It maps: schema SoT (§2) + live truth (`platform/migrations/**`), the identity model (`ticker+date→classification_id→CIK`, SCD-2 `ticker_history`), the audits + 7 moratoria (`docs/audits/2026-06-03-identity-substrate-data-flow.md`), the discovery-first gates (SWV+CIC), the source roster, ingest entrypoints (`scripts/ops.py --stage`), the 32-check validation suite, the readers (PricesRepo/IdentityDispatcher — pass `as_of`), DB access (pooler `:6543` vs session `:5432` for DDL/COPY), daemons to stop, and the four memory tiers.

Built because the agent kept coming into data work cold and having to be told where each doc/audit lived. See [[project_data_layer_rebuild_arc]], [[project_tradier_closed_no_options]], [[reference_anthropic_memstores]].
