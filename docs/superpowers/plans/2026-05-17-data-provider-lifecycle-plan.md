# Data Provider Lifecycle — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`
(approved, merged `bf60a9f`). Lane: DATA. Flow: spec → **plan (this
doc)** → incremental build, each phase independently testable.

> **Operator-interaction policy (spec §10, authoritative):** operator
> approves ONLY ADD/REMOVE of a feed via the
> [Data Feed Change Request](../checklists/data_feed_change_request.md);
> CUTOVER and EVALUATE are **automated** (the earlier
> "operator-confirmed CUTOVER" framing — Phase 5 / PR #15 — is
> superseded; PR #15 left unmerged). Phase 5's remaining work is the
> runtime binding-state overlay + the deterministic cutover agent that
> applies `plan_cutover`, not an operator runbook.

## Phase decomposition

| Phase | Deliverable | Risk | Status |
|---|---|---|---|
| **1** | `ProviderBinding` registry + invariant/drift test — flat SoT, **landed dark** (no runtime caller; zero behavior change). | low | **this PR** |
| 2 | Data-parity cutover gate (`tpcore/parity` data analog) + EVALUATE checklist doc. | med | next |
| 3 | RETIRE/OFFBOARD checklist + the 3-way-retire enforcement test (HealSpec/FeedProfile/audit retired in one change). | med | — |
| 4 | Backfill richer `evidence` + declare known FALLBACK candidates per feed (e.g. hy_spread eco-archive). | low | — |
| 5 | Wire CUTOVER (status flip + re-validate) + the snap-in/out operator runbook. | med | — |

Phases 1 & 2 are the load-bearing new structural core; 3–5 build on
them. Each lands behind its own PR + CI-green gate.

## Phase 1 — `ProviderBinding` registry (this PR)

### Module: `tpcore/providers.py`
Flat SoT, symmetric to `tpcore/engine_profile.py` /
`tpcore/risk/limits_profile.py` (single module, frozen pydantic v2,
evidence-backed). Pure library — **nothing in the runtime/ingest path
imports it in Phase 1**; CUTOVER/EVALUATE wire it in Phases 2/5. This
is the same "landed dark" model as `engine_profile` Sub-project A.

```python
class ProviderStatus(StrEnum):
    CANDIDATE / ACTIVE / FALLBACK / DEPRECATED / RETIRED

class ProviderBinding(BaseModel):           # frozen, extra="forbid"
    feed: str            # == FeedProfile/HealSpec.source vocabulary
    provider: str        # "alpaca","fred","fmp","internal",…
    adapter_module: str  # dotted path to the current ingest entrypoint
    status: ProviderStatus
    evidence: str        # WHY (no-vendor-blame discipline)
    parity_verified_at: date | None = None

PROVIDER_BINDINGS: dict[str, list[ProviderBinding]]   # feed → bindings
def bindings_for(feed) / active_provider(feed) / all_feeds()
```

### Evidence-derived bindings (Phase 1 records *current reality*, never assumed)
All 13 `FEED_PROFILES` feeds, each one ACTIVE provider today (no
fallbacks yet — Phase 4 adds candidates). Derived by reading each
handler/adapter, not guessed:

| feed | provider | adapter (current entrypoint) |
|---|---|---|
| prices_daily | alpaca | `tpcore.data.ingest_alpaca_bars` (feed=iex, free tier) |
| macro_indicators | fred | `tpcore.ingestion.handlers.handle_macro_indicators` |
| earnings_events | fmp | `scripts.ops._stage_earnings_refresh` |
| sec_insider_transactions | sec_edgar | `tpcore.ingestion.handlers.handle_sec_filings` |
| finra_short_interest | finra | `tpcore.ingestion.handlers.handle_finra_short_interest` |
| apewisdom_social_sentiment | apewisdom | `tpcore.ingestion.handlers.handle_apewisdom_social_sentiment` |
| iborrowdesk_borrow_rates | iborrowdesk | `tpcore.ingestion.handlers.handle_iborrowdesk_borrow_rates` |
| aaii_sentiment | aaii | `tpcore.ingestion.handlers.handle_aaii_sentiment` |
| finnhub_insider_sentiment | finnhub | `tpcore.ingestion.handlers.handle_finnhub_insider_sentiment` |
| greeks_max_pain | tradier | `tpcore.ingestion.handlers.handle_greeks_max_pain` (computed from `platform.tradier_options_chains`) |
| ticker_classifications | alpaca | `tpcore.data.classify_tickers.classify_all_tickers` (Alpaca assets) |
| liquidity_tiers | internal | `scripts.ops._stage_tier_refresh` — **derived**, no external vendor |
| fear_greed | internal | `tpcore.ingestion.handlers.handle_fear_greed` — **derived**, no external vendor |

### Honest scoping (no over-reach on the dark landing)
Existing adapters are *functions/stages*, not a uniform
`DataProviderInterface`. Phase 1's invariant test asserts only that
`adapter_module` resolves to an **importable** dotted path (evidence
not assumed) — it does **not** retro-enforce interface conformance
(that would false-fail every existing handler). `DataProviderInterface`
conformance is enforced at the **ONBOARD** gate for *new* providers
(spec §4 stage 3), not retrofitted onto the SoT.

### Invariant / drift test (`tpcore/tests/test_providers.py`)
Symmetric to the `HealSpec` registry-coverage test:
1. **Coverage drift guard**: every `FEED_PROFILES` feed has ≥1
   binding, and every binding's feed is a known feed — both directions,
   so a new feed fails the build until a binding is recorded (the
   self-heal-registry pattern: a decision can't be forgotten).
2. **Exactly one ACTIVE** per feed (the snap-in/out invariant).
3. `adapter_module` resolves via `importlib` (evidence is real).
4. `FALLBACK` ⇒ `parity_verified_at` is not None (can't stand in
   without a parity pass — enforced now even though parity lands in
   Phase 2).
5. Model is frozen (mutation raises).

### Out of scope for Phase 1
No CUTOVER, no parity gate, no consumer rewiring — pure SoT + tests.
Backtesting stays back-burnered.
