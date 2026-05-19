---
name: adapter-readiness
description: "Use when building a new data adapter (anything under tpcore/ingestion/handlers.py or scripts/ops.py stages) or modifying the adapter template — loads the canonical 6-stage data-adapter contract checklist (ingest / test / validate / dashboard / schedule / self-heal)."
---

# Adapter readiness

Canonical text: `docs/superpowers/checklists/adapter_readiness.md` (the SoT). Pipeline spec: `docs/superpowers/pipelines/data_adapter_pipeline.md`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## When this applies

You're starting or modifying any of:

- A new data adapter under `tpcore/ingestion/handlers.py` or as a new `scripts/ops.py` stage
- The adapter template `tpcore/templates/adapter_template.py`
- A new feed/derived-datum that needs to be wired through `tpcore/providers.py::ProviderBinding`

The path-scoped rule `.claude/rules/data-adapter.md` auto-applies on those paths; this skill is the complementary on-demand reference.

## What to read

`docs/superpowers/checklists/adapter_readiness.md` end-to-end. The 6-stage contract (ingest / test / validate / dashboard / schedule / **self-heal**) is the gate. A feed isn't shipped until all 6 stages exist. CSV-first sub-protocol; `tpcore.outage.with_retry`; `assert_contract_populated` sentinel.

## Adjacent SoT

- `tpcore/templates/adapter_template.py` — copy-paste-start scaffold.
- `.claude/rules/data-adapter.md` — the path-scoped invariant.
- `.claude/rules/data-feed-roster.md` — the DFCR + ProviderBinding lifecycle.
- `docs/superpowers/checklists/data_feed_change_request.md` — the DFCR after the adapter clears readiness.
- `docs/DEV_PIPELINE_STANDARD.md` §0 — a new adapter is a **heavy lane** trigger; full §1 pipeline mandatory.
