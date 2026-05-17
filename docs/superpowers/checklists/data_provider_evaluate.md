# Data Provider EVALUATE Checklist

Gate a **CANDIDATE** provider must pass before it may become a
`FALLBACK` or `ACTIVE` for a feed. Stage 2 of the Data Provider
Lifecycle (spec `…/specs/2026-05-17-data-provider-lifecycle-design.md`
§5; plan Phase 2). Mirrors `adapter_readiness.md` — every box checked
before the binding's status advances.

> **Automated, not operator-confirmed.** EVALUATE runs automatically;
> it gates the automated CUTOVER (`data_provider_cutover.md`). The
> operator does **not** approve EVALUATE or CUTOVER — only ADD/REMOVE
> via the [Data Feed Change Request](data_feed_change_request.md)
> (spec §10).

This is the structural defense against the silent-degradation class
(the prices_daily 506/7,650 incident; FRED truncation; Alpaca
SIP/IEX). **A failing parity verdict BLOCKS cutover.**

## 0. Prerequisites

- [ ] A `ProviderBinding(status=CANDIDATE)` exists for `(feed,
      provider)` with a real `evidence` string (registry SoT).
- [ ] The candidate adapter satisfies the **ONBOARD** gate
      (`adapter_readiness.md` — the 6-stage adapter contract). EVALUATE
      assumes a working adapter; it certifies *parity*, not basic
      correctness.

## 1. Overlap window (per feed class)

Run the candidate **alongside** the incumbent over a contiguous recent
window long enough to be representative:

- [ ] `PRICE` — ≥ 20 trading sessions.
- [ ] `MACRO` — ≥ 60 calendar days (series cadence-dependent).
- [ ] `SENTIMENT` — ≥ 8 publication cycles.
- [ ] `FILING` — ≥ 90 calendar days (event sparsity).
- [ ] `DERIVED` — **N/A**: no external provider to parity-test;
      correctness is the internal recompute + its upstream feeds
      (`HealSpec.depends_on`). A derived feed never goes through
      EVALUATE.

## 2. Parity verdict (the gate)

- [ ] Candidate + incumbent samples for the window are normalized to
      `tpcore.parity.ParitySample` (`key`, `asof`, `value`).
- [ ] `tpcore.parity.compare_provider_parity(...)` returns
      `ParityVerdict.PASS` — i.e. **all** applicable dimensions clear
      the feed-class `ParityTolerance`:
  - [ ] **coverage** ≥ `coverage_min_ratio` (candidate does not
        silently drop keys the incumbent has).
  - [ ] **freshness** lag ≤ `freshness_max_lag_days`.
  - [ ] **accuracy** ≥ `accuracy_min_ratio` (value feeds only;
        `FILING` is presence-only).
- [ ] `NOT_EVALUABLE` is **not** a pass. An empty incumbent or a
      derived feed cannot be cut over on a parity basis — escalate to
      the operator with the reason; do not advance the binding.
- [ ] Any per-feed `ParityTolerance` override (vs the class default)
      is justified in writing and recorded in the binding `evidence`
      (no-vendor-blame discipline — relax only on recorded evidence).

## 3. Record + sign-off

- [ ] The `DataParityResult.evidence` string is persisted (the EVALUATE
      audit trail — same discipline as the credibility rubric).
- [ ] `ProviderBinding.parity_verified_at` is set to the evaluation
      date when promoting to `FALLBACK` (the registry invariant test
      enforces `FALLBACK ⇒ parity_verified_at`).
- [ ] **CUTOVER is operator-confirmed** (spec non-goal: not an
      automatic swapper). A passing EVALUATE makes a provider
      *cutover-eligible*; the ACTIVE flip is a deliberate operator
      action (Phase 5).

## 4. On failure

- [ ] A `FAIL` verdict blocks promotion. Record the failing
      dimension(s) in the binding `evidence`; the candidate stays
      `CANDIDATE`. Do not weaken a tolerance to force a pass — that
      reintroduces exactly the silent-degradation class this gate
      exists to prevent.
