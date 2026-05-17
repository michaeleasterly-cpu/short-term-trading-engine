# Data Provider Lifecycle — Design

**Status:** draft 2026-05-17 (DATA lane). Brainstorm → **spec (this doc)** →
plan → incremental build. Operator directive: *"I want to snap data
providers in and out… the market changes and the data sources will
change with it… we need a repeatable data-provider lifecycle like the
SDLC."*

## 1. Problem

A *feed* (a logical data need — daily bars, HY-spread, social
sentiment) is today **conflated with its provider** (Alpaca, FRED,
ApeWisdom). When the market or a vendor changes, the provider must
change — and we have no repeatable, gated process for it. This session
alone surfaced three provider-change events handled ad hoc:

- **FRED truncated `BAMLH0A0HYM2`** to a rolling window → manual
  eco-archive + Scribd recovery.
- **Alpaca free tier has no SIP entitlement** → `feed="sip"` 403'd
  every chunk; fixed only after a live incident.
- **ApeWisdom coverage ceiling (~23%)** → floor recalibrated reactively.

Each is the *same class*: a provider silently degraded or changed, and
nothing structurally forced an evaluation, a clean cutover, or an
honest retirement. This is the data-side analog of the fake-healable
class fixed in #2 — undetected divergence between intent and reality.

## 2. Core concept — Provider as a first-class entity

Decouple **Feed** (the logical need; what consumers reference) from
**Provider** (a concrete source + adapter that satisfies it). A feed is
served by exactly one **active** provider, with declared **candidate**
and **fallback** providers. "Snap in/out" = change which provider is
active for a feed **without touching any consumer** — consumers depend
on the feed via the existing `DataProviderInterface`, never on a vendor.

This is **not a new framework.** ~80% of the lifecycle already exists;
this spec promotes Provider to first-class and adds the two missing
gates (EVALUATE, RETIRE) plus the binding registry.

## 3. The provider↔feed registry (flat SoT)

Symmetric to the platform's proven flat-SoT pattern
(`tpcore.feeds.FeedProfile`, `tpcore.selfheal.HealSpec`,
`tpcore.engine_profile`, `tpcore.risk.limits_profile`). Pydantic v2,
frozen, evidence-backed.

```python
class ProviderStatus(StrEnum):
    CANDIDATE  = "candidate"   # proposed; not serving
    ACTIVE     = "active"      # the one serving the feed now
    FALLBACK   = "fallback"    # parity-verified; cutover-ready standby
    DEPRECATED = "deprecated"  # scheduled for retirement
    RETIRED    = "retired"     # offboarded; kept for provenance only

class ProviderBinding(BaseModel):     # frozen
    feed: str                         # logical feed (== HealSpec.source vocabulary)
    provider: str                     # "alpaca", "fred", "apewisdom", …
    adapter_module: str               # importable adapter (DataProviderInterface)
    status: ProviderStatus
    evidence: str                     # WHY this binding/decision (no-vendor-blame discipline)
    parity_verified_at: date | None   # last EVALUATE pass vs the incumbent
```

Invariants (registry test, like the HealSpec drift test): exactly one
`ACTIVE` per feed; every feed in `HealSpec.source` / `FeedProfile` has a
binding; `adapter_module` imports and conforms to
`DataProviderInterface`; a `FALLBACK` must have a non-null
`parity_verified_at`. The registry **is** the snap-in/out control
surface.

## 4. The lifecycle (the SDLC) — stages & gates

| Stage | Gate (must pass to advance) | Reuses |
|---|---|---|
| **1. PROPOSE** | A `ProviderBinding(status=CANDIDATE)` + rationale recorded. | registry |
| **2. EVALUATE** | **Data-parity gate** (§5): candidate ≥ incumbent on coverage/freshness/accuracy over an overlap window. | new (parity analog) |
| **3. ONBOARD** | The existing **6-stage adapter contract** (ingest/test/validate/dashboard/schedule/self-heal) — `adapter_readiness.md`. | `data_adapter_pipeline.md`, `adapter_template.py` |
| **4. ACTIVATE / CUTOVER** | Atomic swap: candidate→ACTIVE, incumbent→FALLBACK. Validation suite + audit **green post-cutover**. | validation suite, `audit_data_pipeline` |
| **5. MONITOR** | `FeedProfile` freshness/coverage + `HealSpec`(+`depends_on`) self-heal + audit — continuous. | `tpcore.feeds`, `tpcore.selfheal` |
| **6. DEPRECATE** | Replacement is ACTIVE and stable N cycles; incumbent flagged DEPRECATED with a retire date. | registry |
| **7. RETIRE / OFFBOARD** | CSV-archive the provider's history; repoint feed to FALLBACK *or* mark feed deprecated; **retire/repoint its `HealSpec`, `FeedProfile`, and audit check in the SAME change**; validation green. | CSV-first archive, the "audit tracks current reality" rule |

Each gate is a checklist mirroring `adapter_readiness.md`. **EVALUATE**
and **RETIRE** are the stages we have no process for today and are the
load-bearing new work.

## 5. The data-parity cutover gate (new)

The structural defense against silent provider degradation — the data
analog of `tpcore/parity` (engine live-vs-paper parity). Before a
candidate may reach FALLBACK/ACTIVE, run it **alongside** the incumbent
over an overlap window and assert, per feed:

- **Coverage:** candidate ≥ incumbent ticker/series coverage (no
  silent shrinkage — the 506/7,650 class).
- **Freshness:** candidate meets the feed's `FeedProfile`
  vendor-anchored cadence.
- **Accuracy:** values agree within a per-feed tolerance over the
  overlap (price bars exact-ish; sentiment within band).

Verdict + evidence persisted (like the credibility rubric). A failing
parity gate **blocks cutover** — you cannot snap in a provider that is
quietly worse.

## 6. Snap-in / snap-out mechanics

- Consumers import the **feed** via `DataProviderInterface`; they never
  name a vendor. Swapping the active provider is a registry
  `status` change + the adapter behind the interface — zero consumer
  edits.
- **Snap-in:** PROPOSE → EVALUATE(parity) → ONBOARD → CUTOVER.
- **Snap-out:** DEPRECATE → RETIRE (archive + repoint + retire
  HealSpec/FeedProfile/audit in one change). The incumbent stays a
  parity-verified FALLBACK until RETIRE, so cutover is reversible.

## 7. Reused vs new (explicit)

| Reused (no rebuild) | New (this spec) |
|---|---|
| 6-stage adapter contract; `adapter_template.py`; `adapter_readiness.md` | `ProviderBinding` registry (flat SoT) + drift test |
| `FeedProfile` (cadence/freshness/targeting) | EVALUATE stage + **data-parity gate** |
| `HealSpec` + `depends_on` + self-heal | RETIRE/OFFBOARD stage gate (archive + 3-way retire) |
| validation suite + `audit_data_pipeline` | Provider/Feed decoupling at the interface boundary |
| CSV-first archive (eco-archive pattern) | Lifecycle checklist docs (mirror `adapter_readiness.md`) |
| `tpcore/parity` pattern (engine) | — |

## 8. Failure modes this prevents (mapped)

- FRED truncation → caught at **MONITOR** (FeedProfile freshness), a
  FALLBACK already parity-verified makes **CUTOVER** a one-line status
  change instead of a manual recovery.
- Alpaca SIP/IEX entitlement → caught at **EVALUATE** (parity would
  403 before any cutover) instead of in production.
- ApeWisdom ceiling → **EVALUATE** records the real coverage ceiling as
  the binding's `evidence`; the floor is set from parity data, not
  reactively.
- Ad-hoc retirement losing history / leaving fake-healable specs → the
  **RETIRE** gate forces archive + 3-way (HealSpec/FeedProfile/audit)
  retirement in one change.

## 9. Phased build plan

1. `ProviderBinding` registry + drift/invariant test (load-bearing;
   makes the control surface real, zero behavior change — landed dark).
2. Data-parity gate (`tpcore/parity` data analog) + EVALUATE checklist.
3. RETIRE/OFFBOARD checklist + the 3-way-retire enforcement test.
4. Backfill bindings for all existing feeds (evidence-derived, like the
   `depends_on` map — never assumed).
5. Wire CUTOVER to flip status + re-validate; document snap-in/out runbook.

Each phase independently testable; (1) and (2) are the new structural
core.

## 10. Non-goals / open questions

- **Not** an automatic provider-swapper — CUTOVER is operator-confirmed
  (a provider change is structural, like engine archival).
- Open: does a feed ever need *simultaneous* multi-provider blending
  (e.g. union coverage), or strictly one ACTIVE? (Default: one ACTIVE;
  blending is a future variant, not v1.)
- Open: parity overlap-window length per feed class (price vs
  sentiment vs macro) — to be set from evidence in the plan phase.

---
*Lane: DATA. Engine-session specs untouched. Brainstorm artifacts and
the implementation plan follow in a separate plan doc before any code.*
