"""Unit tests for ``tpcore.fred.diffusion.compute_sos_diffusion``.

Pure function, no I/O — every test feeds a synthetic
``{state: [{"date", "value"}]}`` mapping in and asserts the diffusion
value at each output ``t``. Coverage pins behavior NOT implementation:

* C1 — every state monotone-up over the span → diffusion = 0.0.
* C2 — every state monotone-down over the span → diffusion = 1.0.
* C3 — 30 states down + 20 states up → diffusion = 0.4 (30/50).
* C4 — one state missing the date ``t`` → ``t`` is EXCLUDED from
  output entirely (not silently averaged across the remaining 49).
* C5 — ``span_months`` actually changes the lookback (1 vs 3 produce
  different valid outputs given the same synthetic series).
* C6 — non-overlapping date ranges across states → empty output, no
  crash.

The reference paper (Crone/Clayton-Matthews 2005) defines the
diffusion index over ALL anchor states; "all-or-exclude" at month
``t`` is the principled choice — silently averaging across an
incomplete subset would game the invariant.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tpcore.fred.diffusion import (
    DEFAULT_SPAN_MONTHS,
    compute_sos_diffusion,
)

# ── Builders ───────────────────────────────────────────────────────────


def _monotone_series(
    start: date,
    *,
    n_months: int,
    start_value: float,
    step: float,
) -> list[dict[str, object]]:
    """Build ``n_months`` first-of-month rows with linearly evolving values."""
    out: list[dict[str, object]] = []
    y, m = start.year, start.month
    v = start_value
    for _ in range(n_months):
        out.append({"date": date(y, m, 1), "value": Decimal(repr(v))})
        v += step
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def _states_with(series: list[dict[str, object]], n: int) -> dict[str, list[dict[str, object]]]:
    """Replicate one series across ``n`` synthetic state codes."""
    return {f"phci_s{i:02d}": list(series) for i in range(n)}


# ── C1 — all rising ────────────────────────────────────────────────────


def test_diffusion_zero_when_all_states_rising() -> None:
    series = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=1.0,
    )
    by_state = _states_with(series, 50)
    out = compute_sos_diffusion(by_state, span_months=3)

    # Expect 9 output months: months 4..12 each have a t-3 reference.
    assert len(out) == 9
    assert all(row["value"] == 0.0 for row in out)
    # Dates strictly increasing.
    dates = [row["date"] for row in out]
    assert dates == sorted(dates)
    assert dates[0] == date(2020, 4, 1)
    assert dates[-1] == date(2020, 12, 1)


# ── C2 — all falling ───────────────────────────────────────────────────


def test_diffusion_one_when_all_states_falling() -> None:
    series = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=-1.0,
    )
    by_state = _states_with(series, 50)
    out = compute_sos_diffusion(by_state, span_months=3)

    assert len(out) == 9
    assert all(row["value"] == 1.0 for row in out)


# ── C3 — mixed (30 down + 20 up) ───────────────────────────────────────


def test_diffusion_mixed_30_down_20_up_yields_0_6() -> None:
    """30 of 50 states with negative span → 30/50 = 0.6 diffusion."""
    rising = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=1.0,
    )
    falling = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=-1.0,
    )
    by_state: dict[str, list[dict[str, object]]] = {}
    for i in range(30):
        by_state[f"phci_d{i:02d}"] = list(falling)
    for i in range(20):
        by_state[f"phci_u{i:02d}"] = list(rising)

    out = compute_sos_diffusion(by_state, span_months=3)
    assert out, "non-empty output expected"
    # Every month with a t-3 reference: 30/50 = 0.6.
    assert all(row["value"] == pytest.approx(0.6) for row in out)


# Operator-brief variant ("30 up + 20 down → 0.4"): with the
# fraction defined as share-of-decreasing-states, 20 of 50 = 0.4.
def test_diffusion_mixed_20_down_30_up_yields_0_4() -> None:
    rising = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=1.0,
    )
    falling = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=-1.0,
    )
    by_state: dict[str, list[dict[str, object]]] = {}
    for i in range(20):
        by_state[f"phci_d{i:02d}"] = list(falling)
    for i in range(30):
        by_state[f"phci_u{i:02d}"] = list(rising)

    out = compute_sos_diffusion(by_state, span_months=3)
    assert out
    assert all(row["value"] == pytest.approx(0.4) for row in out)


# ── C4 — missing-state-month is excluded entirely ──────────────────────


def test_diffusion_excludes_months_with_missing_state() -> None:
    """If one state lacks the observation at month X, month X is NOT
    in the output at all — not silently averaged over the other 49
    (zero-tolerance, mirrors macro_indicators_completeness)."""
    full = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=-1.0,
    )
    by_state = _states_with(full, 50)
    # Knock month 2020-07-01 out of exactly one state.
    by_state["phci_s07"] = [
        r for r in by_state["phci_s07"] if r["date"] != date(2020, 7, 1)
    ]

    out = compute_sos_diffusion(by_state, span_months=3)
    dates = [r["date"] for r in out]
    # 2020-07-01 cannot appear (state 7 is missing the anchor).
    assert date(2020, 7, 1) not in dates
    # And 2020-10-01 cannot appear either: it would need 2020-07-01 as
    # its 3-month lookback (which state 7 lacks).
    assert date(2020, 10, 1) not in dates
    # Months outside both windows DO appear.
    assert date(2020, 4, 1) in dates
    assert date(2020, 12, 1) in dates


# ── C5 — span_months parameterizes the lookback ────────────────────────


def test_span_months_parameterizes_lookback() -> None:
    """A synthetic up-then-down series must produce different outputs
    for span_months=1 vs span_months=3 — proving the parameter is
    actually consulted, not silently fixed at 3."""
    # 6 up steps then 6 down steps — peaks at month 6.
    rows: list[dict[str, object]] = []
    y, m = 2020, 1
    v = 100.0
    for i in range(12):
        rows.append({"date": date(y, m, 1), "value": Decimal(repr(v))})
        v += 1.0 if i < 6 else -1.0
        m += 1
        if m == 13:
            m, y = 1, y + 1
    by_state = _states_with(rows, 5)

    out_span1 = compute_sos_diffusion(by_state, span_months=1)
    out_span3 = compute_sos_diffusion(by_state, span_months=3)

    # span=1 has more output months (only loses month 1).
    # span=3 loses months 1..3.
    assert len(out_span1) > len(out_span3)

    # At month 2020-08-01 (8th month, post-peak):
    # span=1 compares Aug to Jul → falling → 1.0.
    # span=3 compares Aug to May (May=104, Aug=106) → rising → 0.0.
    aug1 = next(r for r in out_span1 if r["date"] == date(2020, 8, 1))
    aug3 = next(r for r in out_span3 if r["date"] == date(2020, 8, 1))
    assert aug1["value"] == 1.0
    assert aug3["value"] == 0.0


# ── C6 — non-overlapping ranges → empty ────────────────────────────────


def test_returns_empty_when_no_overlap() -> None:
    s1 = _monotone_series(date(2020, 1, 1), n_months=6, start_value=100.0, step=1.0)
    # Disjoint window 2030, no overlap with 2020.
    s2 = _monotone_series(date(2030, 1, 1), n_months=6, start_value=100.0, step=1.0)
    by_state = {"phci_s1": s1, "phci_s2": s2}
    assert compute_sos_diffusion(by_state, span_months=3) == []


# ── Edge: empty input ──────────────────────────────────────────────────


def test_returns_empty_when_input_empty() -> None:
    assert compute_sos_diffusion({}, span_months=3) == []


# ── Edge: a state with zero usable rows excludes everything ────────────


def test_returns_empty_when_any_state_has_no_usable_rows() -> None:
    series = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=1.0,
    )
    by_state = _states_with(series, 5)
    by_state["phci_blank"] = []  # zero usable rows
    out = compute_sos_diffusion(by_state, span_months=3)
    # The contract: ANY blank state forces the entire derivation
    # empty (no silent dropping of anchor states).
    assert out == []


# ── Edge: span_months ≥ window length → empty (no lookback) ────────────


def test_span_months_exceeds_window_returns_empty() -> None:
    series = _monotone_series(
        date(2020, 1, 1), n_months=3, start_value=100.0, step=1.0,
    )
    by_state = _states_with(series, 5)
    # window has 3 months; span=4 means no t has a t-4 anchor.
    out = compute_sos_diffusion(by_state, span_months=4)
    assert out == []


# ── Edge: invalid span_months ──────────────────────────────────────────


def test_span_months_zero_raises() -> None:
    with pytest.raises(ValueError):
        compute_sos_diffusion({"phci_s1": []}, span_months=0)


def test_span_months_negative_raises() -> None:
    with pytest.raises(ValueError):
        compute_sos_diffusion({"phci_s1": []}, span_months=-1)


# ── Output shape ───────────────────────────────────────────────────────


def test_default_span_is_three() -> None:
    assert DEFAULT_SPAN_MONTHS == 3


def test_output_value_bounded_zero_to_one() -> None:
    """No matter what synthetic mix is fed in, the per-month value is
    in [0.0, 1.0]."""
    rising = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=1.0,
    )
    falling = _monotone_series(
        date(2020, 1, 1), n_months=12, start_value=100.0, step=-1.0,
    )
    by_state: dict[str, list[dict[str, object]]] = {}
    # 17 rising + 33 falling → 33/50 = 0.66.
    for i in range(17):
        by_state[f"phci_u{i:02d}"] = list(rising)
    for i in range(33):
        by_state[f"phci_d{i:02d}"] = list(falling)
    out = compute_sos_diffusion(by_state, span_months=3)
    assert out
    for row in out:
        assert 0.0 <= float(row["value"]) <= 1.0
