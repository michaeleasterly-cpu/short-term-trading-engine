# {{ENGINE}} — Eulogy (archived {{DATE}})

{{ENGINE}} was retired via the Engine Change Request (SP3). This is the
auto-generated provenance artifact — the structure mirrors the Sigma
eulogy (the worked example, untouched); the content is engine-specific.

## Cause of death

{{REASON}}

Last on-record gate: {{GATE_RECORD}}

Operator notes:

{{EULOGY_NOTES}}

## What it leaves behind (still in tpcore — not archived)

Nothing engine-specific is left in `tpcore`: the strategy code is
relocated to `archive/{{ENGINE}}/`. Shared tpcore facilities the engine
used (risk, AAR, quality, backtest) are untouched and remain available
to the live engines.

## Retirement checklist (all done {{DATE}})

- [x] `tpcore.engine_profile._PROFILE["{{ENGINE}}"].lifecycle_state` →
      `RETIRED`, `allocator_eligible=False` (SoT flip — auto-delists
      from roster / allocator / check_imports by derivation).
- [x] `{{ENGINE}}/` moved to `archive/{{ENGINE}}/`.
- [x] By-name wrapper scripts moved alongside (if any).
- [x] `scripts/run_smoke_test.sh` step-3 loop purged of `{{ENGINE}}`.
- [x] `pyproject.toml` testpaths + `packages.find.include` purged.
- [x] `test_dispatch_order_invariant_is_the_frozen_literal` updated iff
      the roster changed (same staged diff — never a hand-edit).
- [x] `ENGINE_TABLES` entry removed if present.
- [x] `test_engine_lifecycle_consistency.py` archive-leg clockwork
      green (EULOGY content floor + shadow purge + no-orphan + not
      importable).
