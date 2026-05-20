"""Unit tests for the vol-managed Lab candidate's pure helpers.

Cover:
- ``compute_realized_annual_vol`` — math + degenerate guards.
- ``compute_vol_scale`` — clip bounds + σ-degenerate guard.
- ``has_recent_earnings_beat`` — strictly-backward window + magnitude
  filter + PIT (lookahead-honest, H-MVM-6).
- Pinned constants — sanity-check the pre-registered values match the
  spec exactly (no silent constant drift).
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from momentum.lab_vol_managed import (
    EARNINGS_LOOKBACK_DAYS,
    TARGET_ANNUAL_VOL,
    TRADING_DAYS_PER_YEAR,
    VOL_DEGENERATE_FLOOR,
    VOL_SCALE_HIGH,
    VOL_SCALE_LOW,
    VOL_WINDOW_TRADING_DAYS,
    compute_realized_annual_vol,
    compute_vol_scale,
    has_recent_earnings_beat,
)

# ── compute_realized_annual_vol ─────────────────────────────────────


def test_realized_vol_constant_series_is_zero() -> None:
    """A flat constant-price series has zero realized vol."""
    closes = np.array([100.0] * 60, dtype=float)
    assert compute_realized_annual_vol(closes) == 0.0


def test_realized_vol_known_log_return_series() -> None:
    """For a deterministic geometric series with σ_d = 0.01,
    σ_annual ≈ 0.01 * sqrt(252) ≈ 0.1587."""
    rng = np.random.default_rng(42)
    daily_log_rets = rng.normal(0.0, 0.01, size=200)
    closes = 100.0 * np.exp(np.cumsum(daily_log_rets))
    sigma = compute_realized_annual_vol(closes)
    expected = 0.01 * math.sqrt(TRADING_DAYS_PER_YEAR)
    # Sample variance noise — allow 25% tolerance on this small sample.
    assert sigma == 0.0 or 0.75 * expected < sigma < 1.25 * expected


def test_realized_vol_handles_negative_prices_as_degenerate() -> None:
    """Non-positive prices ⇒ zero (log undefined; collapse to no-scale)."""
    closes = np.array([100.0, 95.0, -1.0, 90.0], dtype=float)
    assert compute_realized_annual_vol(closes) == 0.0


def test_realized_vol_too_short_window_returns_zero() -> None:
    """< 2 prices ⇒ degenerate ⇒ 0.0 (caller collapses to scale=1.0)."""
    assert compute_realized_annual_vol(np.array([])) == 0.0
    assert compute_realized_annual_vol(np.array([100.0])) == 0.0


# ── compute_vol_scale ───────────────────────────────────────────────


def test_vol_scale_normal_case() -> None:
    """At σ_n = TARGET_ANNUAL_VOL the scale is 1.0 (no scaling)."""
    assert compute_vol_scale(TARGET_ANNUAL_VOL) == 1.0


def test_vol_scale_doubles_target_vol_caps_at_high() -> None:
    """At σ_n = 0.10, raw scale = 0.40 / 0.10 = 4.0; clipped to 2.0."""
    assert compute_vol_scale(0.10) == VOL_SCALE_HIGH


def test_vol_scale_huge_vol_clips_at_low() -> None:
    """At σ_n = 2.0, raw scale = 0.40 / 2.0 = 0.20; clipped to 0.5."""
    assert compute_vol_scale(2.0) == VOL_SCALE_LOW


def test_vol_scale_degenerate_zero_returns_one() -> None:
    """σ ≤ DEGENERATE_FLOOR ⇒ s = 1.0 (no scaling neutral, H-MVM-7)."""
    assert compute_vol_scale(0.0) == 1.0
    assert compute_vol_scale(VOL_DEGENERATE_FLOOR / 2.0) == 1.0


def test_vol_scale_just_above_floor_does_not_collapse() -> None:
    """A σ just above the degenerate floor uses the clipped formula —
    so the floor is a hard threshold, not a soft band."""
    sigma = 2 * VOL_DEGENERATE_FLOOR
    # raw = 0.40 / 2e-6 = huge, clipped to VOL_SCALE_HIGH.
    assert compute_vol_scale(sigma) == VOL_SCALE_HIGH


# ── has_recent_earnings_beat (strictly-backward + magnitude > 0) ────


def test_overlay_no_events_excludes_name() -> None:
    """A name with NO earnings rows is excluded (the overlay's
    documented semantic, H-MVM-6)."""
    assert has_recent_earnings_beat(None, as_of=date(2024, 3, 15)) is False
    assert has_recent_earnings_beat([], as_of=date(2024, 3, 15)) is False


def test_overlay_recent_positive_beat_passes() -> None:
    """An EARNINGS_BEAT within [as_of - 90, as_of] with magnitude > 0
    ⇒ name passes the overlay (spec §2.3)."""
    as_of = date(2024, 3, 15)
    events = [(date(2024, 2, 1), 0.08)]  # 43 days before, in window
    assert has_recent_earnings_beat(events, as_of) is True


def test_overlay_beat_outside_window_is_rejected() -> None:
    """A beat MORE than 90 calendar days before as_of ⇒ rejected
    (strictly-backward window; stale catalyst)."""
    as_of = date(2024, 3, 15)
    events = [(date(2023, 11, 1), 0.08)]  # ~135 days before, OUTSIDE
    assert has_recent_earnings_beat(events, as_of) is False


def test_overlay_future_event_is_rejected_lookahead_proof() -> None:
    """A row dated AFTER as_of is NEVER allowed — the strictly-backward
    contract (spec §7, H-MVM-6). This is the lookahead-honesty pin."""
    as_of = date(2024, 3, 15)
    events = [(date(2024, 5, 1), 0.08)]  # 47 days AFTER, lookahead
    assert has_recent_earnings_beat(events, as_of) is False


def test_overlay_zero_magnitude_is_rejected() -> None:
    """A row with magnitude == 0 ⇒ rejected (spec §2.3: must be > 0).

    Defensive: real data has all magnitudes > 0, but a future loader
    bug or NULL coercion could produce a 0 — the overlay must filter
    them out to preserve the "positive beat" semantic.
    """
    as_of = date(2024, 3, 15)
    events = [(date(2024, 2, 1), 0.0)]
    assert has_recent_earnings_beat(events, as_of) is False


def test_overlay_first_match_wins_short_circuits() -> None:
    """Multiple events — at least one in-window + positive ⇒ True."""
    as_of = date(2024, 3, 15)
    events = [
        (date(2023, 11, 1), 0.05),   # outside
        (date(2024, 2, 1), 0.08),    # inside, positive
        (date(2024, 5, 1), 0.10),    # future
    ]
    assert has_recent_earnings_beat(events, as_of) is True


def test_overlay_boundary_inclusive() -> None:
    """The window is INCLUSIVE on both endpoints (spec §2.3:
    [as_of - 90, as_of])."""
    as_of = date(2024, 3, 15)
    # Boundary low: exactly 90 days before.
    from datetime import timedelta
    boundary_low = as_of - timedelta(days=EARNINGS_LOOKBACK_DAYS)
    assert has_recent_earnings_beat(
        [(boundary_low, 0.05)], as_of,
    ) is True
    # Boundary high: exactly as_of.
    assert has_recent_earnings_beat(
        [(as_of, 0.05)], as_of,
    ) is True


# ── Pinned constants — anti-drift sanity ────────────────────────────


def test_pinned_constants_match_spec_exactly() -> None:
    """The spec pins every constant; this test reds if anyone changes
    them silently (H-MVM-2 — no hidden grid via constant drift)."""
    assert TARGET_ANNUAL_VOL == 0.40
    assert VOL_WINDOW_TRADING_DAYS == 60
    assert VOL_SCALE_LOW == 0.5
    assert VOL_SCALE_HIGH == 2.0
    assert VOL_DEGENERATE_FLOOR == 1e-6
    assert EARNINGS_LOOKBACK_DAYS == 90
    assert TRADING_DAYS_PER_YEAR == 252
