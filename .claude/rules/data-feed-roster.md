---
name: data-feed-roster
paths:
  - "tpcore/providers.py"
  - "tpcore/quality/validation/checks/**"
description: "Path-scoped rule: data-feed roster (ProviderBinding) edited via DFCR ONLY; feed/provider decoupling; CUTOVER/EVALUATE automated vs ADD/REMOVE operator-approved."
---

# Data-feed roster SoT (DFCR-only)

Canonical SoT: `tpcore/providers.py::ProviderBinding` (flat-SoT registry). Heavy-lane rule applies (see `heavy-lane`).
Authoritative external: <https://code.claude.com/docs/en/extend>, <https://code.claude.com/docs/en/memory>.

Hard rule:

- **`ProviderBinding` registry is edited via the DFCR ONLY** — `docs/superpowers/checklists/data_feed_change_request.md` is the structured touchpoint. Status enum: `CANDIDATE / ACTIVE / FALLBACK / DEPRECATED / RETIRED`. Exactly **one ACTIVE per feed**. A FALLBACK must be parity-verified (`tpcore/parity/data_parity.py`).
- **Feed (logical need) is decoupled from provider (concrete source+adapter).** Operator approves ONLY ADD (ONBOARD) and REMOVE (RETIRE) of a feed/derived datum — binary `APPROVE? (y/n)` on a system-prepared+validated diff.
- **CUTOVER (provider swap for an existing feed), EVALUATE (parity gate), and self-heal are AUTOMATED, deterministic, no approval.** Spec: `docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`.
- **`tpcore/quality/validation/checks/`** is the data-acceptance gate (13 checks; 100% green or no `DATA_OPERATIONS_COMPLETE`). `prices_daily_completeness` is the zero-tolerance invariant.
- **Half-retirement fails CI** (`tpcore/tests/test_provider_lifecycle_consistency.py` — 3-way: `ProviderBinding` + `FeedProfile` + `HealSpec`).
- **Weekly digest** (`ops/weekly_digest.py`) is the non-skippable state-comprehension floor: weekly push of every cutover/self-heal/near-miss-gate + one adversarial "most likely silently wrong"; 30s binary ack; ≥2 unacked weeks ⇒ `live_clearance` auto-de-escalates live trading.

Adapter-contract sentinel (`tpcore/ingestion/adapter_contract.py`) is a producer-hard-stop on silent vendor contract drift — see `data-adapter` rule.

Escalation: `docs/ESCALATION_HARDENING_LADDER.md` (data lane) — every escalation class has a disposition (`auto_converted` / `escalate_operator` / `structural` / `removed`), clockwork-enforced via `tpcore/ladder/`.
