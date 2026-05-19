---
name: engine-readiness
description: "Use when building a new engine (anything matching the <engine>/ 5-plug scaffold) or modifying any plug, scheduler, backtest, or engine_template — loads the canonical 10-section non-optional readiness checklist that the SDLC planner machine-checks via planner._check_readiness."
---

# Engine readiness

Canonical text: `docs/superpowers/checklists/engine_readiness.md` (the SoT — read it; this skill loads the doc, it is NOT a paraphrase).
Authoritative external: <https://code.claude.com/docs/en/skills>.

## When this applies

You're starting or modifying any of:

- A new engine package (anything matching `<engine>/` with the 5-plug structure: `setup_detection`, `lifecycle_analysis`, `execution_risk`, `aar_logging`, `capital_gate`)
- The engine template (`tpcore/templates/engine_template/`)
- Any plug, scheduler, backtest, or model under an existing engine package

The path-scoped rule `.claude/rules/engine-build.md` auto-applies when those paths are edited; this skill is the **complementary on-demand reference** that loads the full 10-section checklist for an author or reviewer.

## What to read

Open `docs/superpowers/checklists/engine_readiness.md` end-to-end before authoring. The 10 sections are non-optional and Section 10 in particular enumerates the six compliance verifications surfaced by the 2026-05-15 Sentinel audit: BaseEnginePlug on every plug, FilterDiagnostics on signals, `write_credibility_score` call, trading-day gate, `classify_exit_reason`, stale-order cancel.

## Adjacent SoT

- `tpcore/templates/engine_template/` — copy-paste-start scaffold.
- `.claude/rules/engine-build.md` — the path-scoped invariant (the always-on copy).
- `docs/superpowers/checklists/engine_change_request.md` — the ECR after the engine clears readiness.
- `docs/DEV_PIPELINE_STANDARD.md` §0 — a new engine is a **heavy lane** trigger; full §1 pipeline mandatory.
