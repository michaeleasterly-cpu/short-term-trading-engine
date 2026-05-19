---
name: run-data-ops
description: "Slash-only wrapper for the canonical daily data operations — scripts/run_data_operations.sh (single button: 14-stage update → audit → validation → compress → emit DATA_OPERATIONS_COMPLETE → forensics scan; macOS notify on failure)."
disable-model-invocation: true
---

# Run data operations

Canonical CLI: `scripts/run_data_operations.sh`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs the canonical daily data-operations cycle. Single button:

1. 14-stage update (the canonical adapter stages)
2. Audit (`scripts/audit_data_pipeline.py` — see `/audit-data-pipeline`)
3. Validation suite (`tpcore.quality.validation`; 13 checks; 100% green required)
4. Compress (CSV archive)
5. Emit `DATA_OPERATIONS_COMPLETE` ON `platform.application_log` — **only if self-heal returns 100% green** (sacred invariant)
6. Forensics scan (`tpcore/forensics/`)
7. macOS notification on any failure

The `engine-service` daemon picks up the `DATA_OPERATIONS_COMPLETE` event and fires the engine sweep (event-driven, not scheduled — `daemons` rule).

## Usage

```bash
scripts/run_data_operations.sh
```

For a full historical refresh: `scripts/run_full_backfill.sh`.

For parameterised single-stage backfills: `python scripts/ops.py --stage <name> --param KEY=VALUE …` — NEVER a one-off `scripts/foo.py` (`data-adapter` rule).

## Invariants (do NOT bypass)

- `DATA_OPERATIONS_COMPLETE` is NEVER emitted unless self-heal returns 100% green ("100% data or don't trade", structural).
- The mkdir-atomic self-exclusion lock prevents a long run overlapping the next scheduled fire; it does NOT guard an ad-hoc concurrent `ops.py --stage daily_bars` from a separate process.
- Step 4c runs `audit_data_pipeline.py` unattended every cycle (known_knowns 🔴 → alarm + hard stop).

## Adjacent SoT

- `.claude/rules/daemons.md` — event-driven topology
- `.claude/rules/selfheal-auditheal.md` — the 100%-green invariant
- `.claude/skills/audit-data-pipeline/SKILL.md`
