---
name: data-adapter
paths:
  - "tpcore/ingestion/**"
  - "scripts/ops.py"
  - "tpcore/templates/adapter_template.py"
description: "Path-scoped rule: data adapters must satisfy the 6-stage canonical contract, the CSV-first sub-protocol, the contract-population sentinel, and the adapter readiness checklist."
---

# Data adapter contract

Canonical SoT: `docs/superpowers/pipelines/data_adapter_pipeline.md` (the 6-stage contract: ingest / test / validate / dashboard / schedule / **self-heal**) + `docs/superpowers/checklists/adapter_readiness.md`.
Authoritative external: <https://code.claude.com/docs/en/extend>.

When touching an ingestion handler, `scripts/ops.py`, or the adapter template:

- **Start from `tpcore/templates/adapter_template.py`** and pass `docs/superpowers/checklists/adapter_readiness.md` before merging. New adapters are 5/5 (now 6/6 incl. self-heal) compliant by construction.
- **CSV-first sub-protocol** for non-trivial pulls: download → validate-at-CSV → load → compress. Pure DB-side INSERT loops are an anti-pattern; the canonical handler validates at the CSV before touching the DB.
- **HTTP retries via `tpcore.outage.with_retry`** — never local `tenacity`, never `asyncio.sleep` loops.
- **Contract-population sentinel** (`tpcore/ingestion/adapter_contract.py::assert_contract_populated`): producer-hard-stop a stage (raise before load) when a declared required adapter-output field is empty across every record of a non-empty pull. Escalate-only; no auto-heal. The frozen `ADAPTER_CONTRACTS` is the SoT.
- **Backfills / re-validations are NEVER a new one-off `scripts/foo.py`.** They run through the canonical stage: `python scripts/ops.py --stage <name> --param KEY=VALUE …`. If a backfill needs a knob the stage lacks, add it to the handler's config contract — do NOT fork a script.
- **Self-heal is generic in `tpcore/selfheal/`, NOT per-source bash.** Register a `HealSpec` (in `tpcore/selfheal/registry.py`); the registry-coverage test reds the build if a new check is missing a HealSpec decision (healable or honest `healable=False`).
- **Data-feed roster (provider) changes** go through the DFCR ONLY — see `data-feed-roster` rule. Never hand-edit `tpcore/providers.py::ProviderBinding`.

Data-layer acceptance gate (13 checks, 100% green or no `DATA_OPERATIONS_COMPLETE`) is the integration contract; `prices_daily_completeness` is the zero-tolerance invariant.
