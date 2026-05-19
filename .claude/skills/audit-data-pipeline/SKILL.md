---
name: audit-data-pipeline
description: "Slash-only wrapper for the canonical data pipeline audit — scripts/run_audit_data_pipeline.sh (or scripts/audit_data_pipeline.py --json). The CLAUDE.md canonical phrase trigger is 'audit data pipeline' / 'audit pipeline' / 'run pipeline audit'."
disable-model-invocation: true
---

# Audit data pipeline

Canonical CLI: `scripts/run_audit_data_pipeline.sh` (human-readable) or `python scripts/audit_data_pipeline.py --json` (machine-readable).
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs the canonical **data** pipeline audit (data service only — NOT engine or AAR services). Four phases × ~33 checks across the data sources: `known_knowns`, `known_unknowns`, `unknown_knowns`, `unknown_unknowns`. Includes guardrail-state checks (`csv_archive_presence`, `shrinkage_detector`) and the adapter-contract sentinel coverage check.

The audit **streams** findings to stdout as each check completes and persists each to `platform.data_quality_log` per-phase (crash-safe) under `source='data_pipeline_audit.<phase>.<check>.<source>'`.

## Usage

```bash
# Default human-readable, streamed:
scripts/run_audit_data_pipeline.sh

# Machine-readable JSON:
python scripts/audit_data_pipeline.py --json

# Single phase:
python scripts/audit_data_pipeline.py --phase known_knowns

# Single source:
python scripts/audit_data_pipeline.py --source <name>
```

## When the operator says "audit data pipeline" / "audit pipeline" / "run pipeline audit"

Run this. Do NOT re-audit manually. The CLAUDE.md phrase trigger is canonical.

## Maintenance contract

When the platform gains a new guardrail or closes a known gap, **add/retire the matching audit check in the same change** — the audit must track current reality, not a frozen snapshot.

## Adjacent SoT

- `scripts/audit_data_pipeline.py` — the audit implementation
- `tpcore/quality/validation/checks/` — the data-acceptance gate (Step 4)
- `.claude/rules/data-feed-roster.md`
- `.claude/rules/selfheal-auditheal.md`
