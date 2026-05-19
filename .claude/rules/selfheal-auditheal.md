---
name: selfheal-auditheal
paths:
  - "tpcore/selfheal/**"
  - "tpcore/auditheal/**"
description: "Path-scoped rule: self-heal + audit-heal are GENERIC engines in tpcore, NOT per-source bash; detector/healer parity; 100%-green invariant; HealSpec registry-coverage test."
---

# Self-heal & audit-heal — generic engines, not per-source bash

Canonical SoT: `tpcore/selfheal/__init__.py`, `tpcore/selfheal/registry.py`, `tpcore/auditheal/spec.py`. Heavy-lane rule applies (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Mandate (architecture invariant):

- **Self-heal is a GENERIC capability in `tpcore/selfheal/`** — never per-source bash. Per-source capability is added by registering a `HealSpec`; never by editing `scripts/run_data_operations.sh`. The bash step is a thin caller of `python -m tpcore.selfheal`.
- **Detector + healer parity**: the validation suite is the detector; `tpcore.selfheal` is the healer in the same layer. They share `_evaluate` so the bounded targeted repair re-pulls only invariant-flagged tickers (the proven `daily_bars --param repair_gaps=true` pattern).
- **100%-green-or-don't-trade.** `DATA_OPERATIONS_COMPLETE` is **NEVER** emitted unless self-heal returns 100% green. Hard safety invariant.
- **HealSpec registry-coverage test** asserts the HealSpec set == `suite.KNOWN_CHECK_NAMES`. A new feed/check **fails the build** until a HealSpec decision is recorded (healable OR honest `healable=False`) — self-heal cannot be forgotten.
- **6th stage of the canonical data-adapter contract** (`docs/superpowers/pipelines/data_adapter_pipeline.md`): a feed isn't shipped until all 6 stages exist.
- **`auditheal` (Step 3)**: structured cross-table referential audit persisted to `data_quality_log`; auto-runs the bounded `cross_ref_cleanup` remediation for the proven `tradier_options_chains` expired/orphan class; re-audits; hard-stops (exit 1, no `DATA_OPERATIONS_COMPLETE`) on any unremediated or escalate-only red — symmetric to the Step-4 `tpcore.selfheal` loop.

All other cross-table checks are escalate-only. The Escalation & Hardening Ladder (`docs/ESCALATION_HARDENING_LADDER.md` data lane; `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` engine lane) is the canonical contract.

`tpcore/datasupervisor/` is state-tracking ONLY — it never gates and does NOT affect the `DATA_OPERATIONS_COMPLETE` 100%-green invariant (sacred invariant unchanged).
