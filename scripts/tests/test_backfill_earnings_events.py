"""Tests for ``scripts/backfill_earnings_events.py::_classify_earnings``.

The classifier is the upstream half of the NO_BEAT-sentinel ingestion
that resolves the prior ``earnings_events_monotone`` KNOWN GAP. The
contract is:

* ``actual_eps`` OR ``estimated_eps`` is ``None`` → return ``None``
  (no event happened — pre-announcement / calendar-only / suspended).
* ``actual > estimated × (1 + 0.05)`` → ``("EARNINGS_BEAT", magnitude)``
  where ``magnitude = (actual − estimated) / estimated`` quantized.
* Anything else where both sides reported →
  ``("EARNINGS_NO_BEAT", None)``. ``magnitude_pct`` is NULL on NO_BEAT
  rows (beat magnitude is undefined for misses).

The test file deliberately pins the classifier — that's the ingestion
contract the monotone-on-the-union invariant gates on. Anything else
(httpx, pool insert) is end-to-end glue and is not the layer under
test.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier
# test in full-suite collection order, so ``import scripts.*``
# resolves the package — the scripts/ops.py vs ops/ collision
# convention from test_gen_engine_manifest_render.py.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from scripts.backfill_earnings_events import (  # noqa: E402
    _classify_earnings,
)

# pytest-xdist: pin to one worker — scripts/ops.py vs ops/ package
# collision is single-process. Mirrors test_gen_engine_manifest_render.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ── Beat path — > 5% beat → EARNINGS_BEAT with magnitude ─────────────


def test_classify_clear_beat_returns_beat_with_magnitude() -> None:
    """actual=2.20, est=2.00 → +10% → BEAT, magnitude=0.1."""
    row = {"epsActual": "2.20", "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result is not None
    event_type, magnitude = result
    assert event_type == "EARNINGS_BEAT"
    assert magnitude == Decimal("0.100000")


def test_classify_zero_estimate_positive_actual_returns_beat_sentinel() -> None:
    """estimate=0, actual>0 → BEAT with sentinel magnitude 999999."""
    row = {"epsActual": "0.50", "epsEstimated": "0.00"}
    result = _classify_earnings(row)
    assert result is not None
    event_type, magnitude = result
    assert event_type == "EARNINGS_BEAT"
    assert magnitude == Decimal("999999")


# ── NO_BEAT path — every reported-but-not-beat row gets a sentinel ───


def test_classify_clear_miss_returns_no_beat_with_null_magnitude() -> None:
    """actual=1.80, est=2.00 → miss → NO_BEAT, magnitude_pct=NULL."""
    row = {"epsActual": "1.80", "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


def test_classify_in_line_returns_no_beat() -> None:
    """actual ≈ est → not a >5% beat → NO_BEAT."""
    row = {"epsActual": "2.00", "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


def test_classify_small_beat_below_threshold_returns_no_beat() -> None:
    """Beat magnitude below the 5% threshold → not a catalyst beat,
    but still a reported event → NO_BEAT (sentinel preserved)."""
    # 2.05 / 2.00 = 1.025 → +2.5%, below the 5% threshold.
    row = {"epsActual": "2.05", "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


def test_classify_zero_estimate_zero_actual_returns_no_beat() -> None:
    """estimate=0, actual=0 → not a beat → NO_BEAT."""
    row = {"epsActual": "0.00", "epsEstimated": "0.00"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


def test_classify_zero_estimate_negative_actual_returns_no_beat() -> None:
    """estimate=0, actual<0 → not a beat → NO_BEAT."""
    row = {"epsActual": "-0.10", "epsEstimated": "0.00"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


def test_classify_negative_estimate_returns_no_beat() -> None:
    """Negative-estimate "beats" (less-of-a-loss-than-expected) are
    NO_BEAT — don't carry the same momentum, per the classifier
    docstring."""
    # actual=-0.05 vs est=-0.20 — less of a loss but neg-est.
    row = {"epsActual": "-0.05", "epsEstimated": "-0.20"}
    result = _classify_earnings(row)
    assert result == ("EARNINGS_NO_BEAT", None)


# ── Skip path — actual OR estimate missing → None (no row) ───────────


def test_classify_missing_actual_returns_none() -> None:
    """No actual_eps → no event happened → skip (return None)."""
    row = {"epsActual": None, "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result is None


def test_classify_missing_estimated_returns_none() -> None:
    """No estimated_eps → no event happened → skip (return None)."""
    row = {"epsActual": "2.20", "epsEstimated": None}
    result = _classify_earnings(row)
    assert result is None


def test_classify_both_missing_returns_none() -> None:
    """Both missing → skip."""
    row = {"epsActual": None, "epsEstimated": None}
    result = _classify_earnings(row)
    assert result is None


def test_classify_malformed_actual_returns_none() -> None:
    """Non-numeric actual → skip (defensive)."""
    row = {"epsActual": "not a number", "epsEstimated": "2.00"}
    result = _classify_earnings(row)
    assert result is None


# ── Mixed-population integration check ───────────────────────────────


def test_classify_mixed_population_emits_both_event_types() -> None:
    """A vendor response with beats + misses + skips emits both event
    types (and skips the missing-data rows). This is the BEAT-only
    KNOWN GAP closer: pre-NO_BEAT-sentinel, only the beat row would
    have landed; post-sentinel, all four reported events become rows,
    so the monotone-on-the-union invariant sees the full population.
    """
    rows = [
        {"epsActual": "2.20", "epsEstimated": "2.00"},  # BEAT
        {"epsActual": "1.80", "epsEstimated": "2.00"},  # NO_BEAT (miss)
        {"epsActual": "2.00", "epsEstimated": "2.00"},  # NO_BEAT (in-line)
        {"epsActual": None, "epsEstimated": "2.00"},  # skip
        {"epsActual": "3.00", "epsEstimated": "2.50"},  # BEAT (+20%)
    ]
    classifications = [_classify_earnings(r) for r in rows]
    # 2 BEATs, 2 NO_BEATs, 1 skip.
    beats = [c for c in classifications if c and c[0] == "EARNINGS_BEAT"]
    no_beats = [c for c in classifications if c and c[0] == "EARNINGS_NO_BEAT"]
    skips = [c for c in classifications if c is None]
    assert len(beats) == 2
    assert len(no_beats) == 2
    assert len(skips) == 1
    # NO_BEAT magnitudes are NULL (the contract).
    assert all(m is None for _, m in no_beats)
    # BEAT magnitudes are set.
    assert all(m is not None for _, m in beats)
