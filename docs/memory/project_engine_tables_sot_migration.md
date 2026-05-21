---
name: engine-tables-sot-migration
description: ENGINE_TABLES dict was folded into EngineProfile.data_dependencies as the canonical SoT 2026-05-20 (PRs
metadata: 
  node_type: memory
  type: project
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**SoT migration completed 2026-05-20: per-engine data dependencies live on `EngineProfile.data_dependencies`.**

**The migration (3 PRs):**
- **PR #171** introduced `EngineProfile.data_dependencies: frozenset[str]` (Pydantic v2 frozen) + backfilled all 7 engines from the prior hand-curated `capital_gate.py::ENGINE_TABLES` dict. Added `tpcore.engine_profile.engine_data_dependencies(engine)` accessor + a drift forcing-test (`test_dispatchable_engine_declares_data_dependencies`) that reds CI on any PAPER/LIVE engine with empty `data_dependencies`. Back-compat: `ENGINE_TABLES` kept as a PEP-562 module-level `__getattr__` shim deriving from `_PROFILE`.
- **PR #191** threaded `data_dependencies` through the ECR planner for `source: existing_code` ADD (validate-time gate + apply-time line literal rendering). MODIFY threading was deferred to spec §7.x.
- **PR #195** removed the `__getattr__` shim entirely after the 4 external consumers (canary tests, capital_gate tests, engine_lifecycle_consistency tests, engine_profile test) migrated. Drift sentinel `test_engine_tables_shim_removed` reds CI on any future shim re-introduction.
- **PR #210** threaded `data_dependencies` through ECR MODIFY (closing the §7.x deferred follow-up).

**Accuracy audit (PR #206) found 2 real MISSING_DEPENDENCY defects:**
- catalyst gained `earnings_events` read via `_fetch_earnings_events` (event-confirmation variant, PR #178) — declared deps unchanged from PR #171.
- momentum gained `earnings_events` read via `_load_earnings_beats` (vol-managed Lab candidate, PR #180) — declared deps unchanged.

ECR-MODIFY files for catalyst + momentum are staged at repo root (`ecr_catalyst_data_dependencies_2026-05-20.txt`, `ecr_momentum_data_dependencies_2026-05-20.txt`) — applicable since PR #210 landed.

**How to apply (future-session checklist):**
- Reading per-engine data deps: `from tpcore.engine_profile import engine_data_dependencies; engine_data_dependencies("<engine>")` returns the canonical frozenset.
- DO NOT `from tpcore.quality.validation.capital_gate import ENGINE_TABLES` — that import raises AttributeError post PR #195. The drift sentinel test will catch any re-introduction.
- Adding data deps to a new engine: ECR-ADD `data_dependencies: prices_daily, fundamentals_quarterly` (comma-separated). The planner threads it into the rendered `_PROFILE` line.
- Modifying deps on an existing engine: ECR-MODIFY `data_dependencies: ...` (PR #210 enables this).
- Per-engine data gate (`capital_gate.assert_passed_for_engine`) reads deps from `EngineProfile.data_dependencies` directly. The check is fail-closed: unknown engine → all-sources required.

**Why this matters / why future sessions need this:**
- The 2026-05-16 ENGINE_TABLES dict is GONE. Code references to `ENGINE_TABLES` in old specs/plans (`docs/superpowers/plans/2026-05-18-engine-change-request.md`, `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`) are HISTORICAL — they describe the pre-migration shape. Don't try to import or modify the constant; it doesn't exist.
- The autonomous Lab criteria (PR #158) reads engine dependencies through the EngineProfile path, not through ENGINE_TABLES. Future criteria changes touch `ops/engine_sdlc/lab_criteria.py` + `EngineProfile.data_dependencies` only.

**Related:**
- `docs/superpowers/specs/2026-05-20-declarative-engine-profile-data-dependencies.md` — the canonical spec (§7.1-7.4 all shipped)
- `docs/superpowers/audits/2026-05-20-engine-data-dependencies-accuracy.md` — the audit + catalyst/momentum defect findings
- [[feedback-tpcore-reuse]] — the operator's reuse discipline applies; EngineProfile is the SoT
