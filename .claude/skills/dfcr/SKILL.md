---
name: dfcr
description: "Slash-only wrapper for the canonical Data Feed Change Request — the single structured touchpoint for ADD / REMOVE / CUTOVER of a feed/provider; never hand-edit tpcore.providers.ProviderBinding."
disable-model-invocation: true
---

# Data Feed Change Request (DFCR)

Structured touchpoint: `docs/superpowers/checklists/data_feed_change_request.md` (the SoT).
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Routes a data-feed roster change through the deterministic data-lane SDLC. The operator approves **only two operations**: **ADD (ONBOARD)** a feed/derived datum and **REMOVE (RETIRE)** one — binary `APPROVE? (y/n)` on a system-prepared+validated diff. **CUTOVER** (provider swap for an existing feed), **EVALUATE** (parity gate via `tpcore/parity/data_parity.py`), and **self-heal** are automated, deterministic, no approval.

## Usage

1. Fill the DFCR block from `docs/superpowers/checklists/data_feed_change_request.md` into a file.
2. Submit it via the canonical pipeline (the data-lane planner reads the structured form; see the checklist for the exact submit command — do NOT hand-edit `tpcore/providers.py`).
3. Approve (`y`) the diff for ADD/REMOVE.

## Pre-conditions

- For ADD: the adapter has passed `docs/superpowers/checklists/adapter_readiness.md` (the 6-stage contract); a `HealSpec` decision is recorded (healable or honest `healable=False`).
- For CUTOVER: the new provider is parity-verified (`tpcore/parity/data_parity.py`).
- `ProviderBinding` status enum: `CANDIDATE / ACTIVE / FALLBACK / DEPRECATED / RETIRED` — exactly one ACTIVE per feed.

## Invariants

- Half-retirement fails CI (`tpcore/tests/test_provider_lifecycle_consistency.py` — 3-way `ProviderBinding` + `FeedProfile` + `HealSpec`).
- **NEVER hand-edit `tpcore/providers.py::ProviderBinding`.** The data-acceptance gate (`tpcore/quality/validation/`) reads the registry; an unsanctioned edit silently desyncs it.

## Adjacent SoT

- `.claude/rules/data-feed-roster.md` — the path-scoped invariant
- `.claude/rules/data-adapter.md` — adapter readiness (pre-DFCR)
- `.claude/skills/adapter-readiness/SKILL.md`
- `docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`
