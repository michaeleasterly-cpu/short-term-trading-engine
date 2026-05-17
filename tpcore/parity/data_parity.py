"""Data-provider parity gate — the EVALUATE-stage cutover defense.

The data analog of the live/paper engine parity harness
(``tpcore.parity.harness``). Before a CANDIDATE provider may become a
FALLBACK/ACTIVE for a feed (spec
``docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md``
§5; plan Phase 2), it must be proven **≥ the incumbent** over an
overlap window on three dimensions:

* **coverage** — the candidate must not silently drop keys the
  incumbent has (the 506/7,650 silent-shrinkage class);
* **freshness** — the candidate's latest as-of must keep up with the
  incumbent's (within a per-feed-class lag);
* **accuracy** — overlapping values must agree within a per-feed-class
  tolerance.

This module is the **pure, deterministic verdict primitive** — no DB,
no network, no provider execution. **Landed dark** in Phase 2 (no
runtime caller); Phase 5 wires it into CUTOVER. Inputs are normalized
sample sets the caller has already pulled; the dual-pull/staging
orchestration is the Phase-5 concern, kept out of this primitive on
purpose so the gate logic is unit-testable in isolation.

A failing parity verdict BLOCKS cutover — you cannot snap in a
provider that is quietly worse. A non-evaluable comparison (no
incumbent, or a derived feed with no external provider) is reported
explicitly — never a silent pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FeedClass(StrEnum):
    """Drives parity tolerances. A feed's class is a property of the
    DATA, not the provider (price bars must match exactly whoever
    serves them; sentiment is inherently noisier)."""

    PRICE = "price"          # OHLCV bars — must match near-exactly
    MACRO = "macro"          # FRED-style series — tight tolerance
    SENTIMENT = "sentiment"  # social/survey — noisy, banded
    FILING = "filing"        # SEC/earnings events — presence, not value
    DERIVED = "derived"      # computed internally — no external provider


class ParityTolerance(BaseModel):
    """Per-feed-class acceptance bar. Evidence-backed defaults below."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Candidate must cover ≥ this fraction of the incumbent's keys.
    coverage_min_ratio: float
    # Candidate's latest as-of may trail the incumbent's by ≤ this.
    freshness_max_lag_days: int
    # Relative tolerance for overlapping values; None = do not compare
    # values (presence-only feeds: filings/events).
    value_rel_tol: float | None
    # Fraction of the overlap that must be within value_rel_tol.
    accuracy_min_ratio: float = 0.99


_TOLERANCES: dict[FeedClass, ParityTolerance] = {
    # Bars are bars — a different vendor's split/adjustment handling is
    # the only legitimate source of small drift; coverage must be total.
    FeedClass.PRICE: ParityTolerance(
        coverage_min_ratio=1.0, freshness_max_lag_days=0,
        value_rel_tol=1e-4, accuracy_min_ratio=0.999,
    ),
    FeedClass.MACRO: ParityTolerance(
        coverage_min_ratio=0.99, freshness_max_lag_days=1,
        value_rel_tol=1e-4, accuracy_min_ratio=0.99,
    ),
    FeedClass.SENTIMENT: ParityTolerance(
        coverage_min_ratio=0.90, freshness_max_lag_days=7,
        value_rel_tol=0.05, accuracy_min_ratio=0.90,
    ),
    # Filings/events: a row existing is the signal; values vary by
    # vendor schema. Presence-only — value comparison skipped.
    FeedClass.FILING: ParityTolerance(
        coverage_min_ratio=0.95, freshness_max_lag_days=2,
        value_rel_tol=None,
    ),
}


@dataclass(frozen=True)
class ParitySample:
    """One normalized observation. ``key`` is the comparable identity
    (e.g. ``f"{ticker}|{date}"`` for bars, ``indicator|date`` for
    macro); ``asof`` is the observation date; ``value`` is the numeric
    payload (None for presence-only feeds)."""

    key: str
    asof: date
    value: float | None = None


class ParityVerdict(StrEnum):
    PASS = "pass"                  # candidate ≥ incumbent — cutover-eligible
    FAIL = "fail"                  # candidate quietly worse — BLOCK cutover
    NOT_EVALUABLE = "not_evaluable"  # no incumbent / derived feed — honest, NOT a pass


class DataParityResult(BaseModel):
    """Verdict + per-dimension detail + evidence (persisted at EVALUATE)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: ParityVerdict
    coverage_ratio: float | None = None
    freshness_lag_days: int | None = None
    accuracy_ratio: float | None = None
    evidence: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict is ParityVerdict.PASS


def _tol(feed_class: FeedClass, override: ParityTolerance | None) -> ParityTolerance | None:
    if override is not None:
        return override
    return _TOLERANCES.get(feed_class)


def compare_provider_parity(
    *,
    feed_class: FeedClass,
    incumbent: list[ParitySample],
    candidate: list[ParitySample],
    tolerances: ParityTolerance | None = None,
) -> DataParityResult:
    """Pure parity verdict: is ``candidate`` ≥ the ``incumbent``?

    DERIVED feeds have no external provider to parity-test → always
    ``NOT_EVALUABLE`` (honest — not a silent pass). An empty incumbent
    (first run / nothing to compare against) is also ``NOT_EVALUABLE``.
    """
    if feed_class is FeedClass.DERIVED:
        return DataParityResult(
            verdict=ParityVerdict.NOT_EVALUABLE,
            evidence="derived feed — no external provider to parity-test; "
                     "correctness is the internal recompute + its upstreams.",
        )
    tol = _tol(feed_class, tolerances)
    if tol is None:
        return DataParityResult(
            verdict=ParityVerdict.NOT_EVALUABLE,
            evidence=f"no ParityTolerance for feed_class={feed_class}",
        )
    if not incumbent:
        return DataParityResult(
            verdict=ParityVerdict.NOT_EVALUABLE,
            evidence="no incumbent samples — nothing to compare against "
                     "(first run); cannot certify a candidate blind.",
        )

    inc_keys = {s.key for s in incumbent}
    cand_keys = {s.key for s in candidate}
    coverage_ratio = len(inc_keys & cand_keys) / len(inc_keys)

    inc_latest = max(s.asof for s in incumbent)
    cand_latest = max((s.asof for s in candidate), default=None)
    freshness_lag = (
        (inc_latest - cand_latest).days if cand_latest is not None else 10**6
    )

    accuracy_ratio: float | None = None
    if tol.value_rel_tol is not None:
        inc_val = {s.key: s.value for s in incumbent if s.value is not None}
        cand_val = {s.key: s.value for s in candidate if s.value is not None}
        common = [k for k in inc_val if k in cand_val]
        if common:
            within = 0
            for k in common:
                iv, cv = inc_val[k], cand_val[k]
                denom = abs(iv) if iv else 1.0
                if abs(cv - iv) / denom <= tol.value_rel_tol:
                    within += 1
            accuracy_ratio = within / len(common)
        else:
            accuracy_ratio = 0.0  # value feed with zero comparable overlap = fail

    cov_ok = coverage_ratio >= tol.coverage_min_ratio
    fresh_ok = freshness_lag <= tol.freshness_max_lag_days
    acc_ok = (
        accuracy_ratio is None
        or accuracy_ratio >= tol.accuracy_min_ratio
    )
    passed = cov_ok and fresh_ok and acc_ok

    fails = [
        name for name, ok in (
            ("coverage", cov_ok), ("freshness", fresh_ok), ("accuracy", acc_ok)
        ) if not ok
    ]
    evidence = (
        f"coverage={coverage_ratio:.3f} (≥{tol.coverage_min_ratio}); "
        f"freshness_lag={freshness_lag}d (≤{tol.freshness_max_lag_days}); "
        f"accuracy={'n/a' if accuracy_ratio is None else f'{accuracy_ratio:.3f}'} "
        f"(≥{tol.accuracy_min_ratio}). "
        + ("PASS" if passed else f"FAIL on: {', '.join(fails)}")
    )
    return DataParityResult(
        verdict=ParityVerdict.PASS if passed else ParityVerdict.FAIL,
        coverage_ratio=coverage_ratio,
        freshness_lag_days=freshness_lag,
        accuracy_ratio=accuracy_ratio,
        evidence=evidence,
    )


__all__ = [
    "DataParityResult",
    "FeedClass",
    "ParitySample",
    "ParityTolerance",
    "ParityVerdict",
    "compare_provider_parity",
]
