"""SOS (Sum-Of-States) diffusion — pure derivation from state PHCI series.

Crone/Clayton-Matthews 2005 sum-of-states diffusion index. For each
month ``t``, the value is the fraction of US states whose Philadelphia
Fed state coincident activity index (``{XX}PHCI``) at month ``t`` is
strictly below its value at month ``t - span_months`` — i.e. the share
of states with a *negative* change-over-span in coincident activity.

Reference: Crone, Theodore M. and Clayton-Matthews, Alan,
"Consistent Economic Indexes for the 50 States," Review of Economics
and Statistics, 87(4): 593-603 (November 2005). The Philadelphia Fed
publishes the resulting ``{XX}PHCI`` series monthly.

Built 2026-05-21 to unblock the Sentinel graduated Bear Score Lab
candidate, which needs an SOS series with a ≥0.20 anchor (i.e. ≥20%
of states with deteriorating coincident activity over a 3-month span).
No single off-the-shelf FRED series provides this; we construct it
from the 50 ``{XX}PHCI`` raw series.

This module is pure / side-effect-free / unit-testable in isolation:
no DB, no HTTP, no logging beyond the structlog import (which is
import-time only). It is consumed by
``tpcore.ingestion.handlers.handle_macro_indicators`` after the FRED
fetch loop populates the 50 raw series.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_SPAN_MONTHS = 3


def compute_sos_diffusion(
    phci_rows_by_state: dict[str, list[dict[str, Any]]],
    span_months: int = DEFAULT_SPAN_MONTHS,
) -> list[dict[str, Any]]:
    """Crone/Clayton-Matthews 2005 SOS state diffusion.

    For each month ``t`` where EVERY anchor state has BOTH an
    observation at ``t`` AND an observation at ``t - span_months``,
    emit one row::

        {"date": t, "value": share_of_states_with_PHCI(t) < PHCI(t-span)}

    Args:
        phci_rows_by_state: ``{state_code: [{"date": date, "value":
            Decimal|float|int}, ...]}``. ``state_code`` is opaque to
            this function — every state present in the mapping is an
            anchor; the caller (the handler) is responsible for passing
            the canonical 50-state set. Within each state's list,
            entries may be in any order; this function sorts by date.
        span_months: lookback in months. Defaults to 3 (Crone/
            Clayton-Matthews 2005 standard). Must be ≥ 1.

    Returns:
        Sorted-by-date list of ``{"date": date, "value": float}`` rows.
        ``value`` is in ``[0.0, 1.0]``. Months where ANY anchor state
        is missing either ``t`` or ``t - span_months`` are excluded
        entirely (NOT silently filled / averaged) — this mirrors the
        zero-tolerance approach used by ``macro_indicators_completeness``
        and keeps the output ungameable.

        Returns ``[]`` when ``phci_rows_by_state`` is empty, when no
        single ``t`` satisfies the all-states-present requirement, or
        when ``span_months`` exceeds every state's active window.

    Raises:
        ValueError: ``span_months < 1``.
    """
    if span_months < 1:
        raise ValueError(
            f"span_months must be >= 1, got {span_months}"
        )

    n_states = len(phci_rows_by_state)
    if n_states == 0:
        return []

    # Build per-state {date -> float(value)} maps; tolerate any
    # numeric value type (Decimal, float, int, str-of-number) by
    # round-tripping through float — the comparison is "<", precision
    # to 1e-15 is sufficient for diffusion (values are eventually
    # binned to a fraction in [0,1]).
    by_state: dict[str, dict[date, float]] = {}
    for state, rows in phci_rows_by_state.items():
        m: dict[date, float] = {}
        for row in rows:
            d = row.get("date")
            v = row.get("value")
            if d is None or v is None:
                continue
            try:
                m[d] = float(v)
            except (TypeError, ValueError):
                continue
        if m:
            by_state[state] = m

    # If any state contributed zero usable rows, we can never satisfy
    # "all states present at t and t-span" for any t — return empty
    # (rather than silently dropping that state, which would game the
    # invariant).
    if len(by_state) != n_states:
        logger.warning(
            "tpcore.fred.diffusion.empty_state_excluded",
            input_states=n_states, usable_states=len(by_state),
        )
        return []

    # Candidate months: dates present in EVERY state's series.
    state_date_sets: list[set[date]] = [
        set(m.keys()) for m in by_state.values()
    ]
    common_dates: set[date] = set.intersection(*state_date_sets) if state_date_sets else set()
    if not common_dates:
        return []

    # For each candidate ``t``, the "value at t - span_months" exists
    # iff t - span_months is also a common date (i.e. every state has
    # it). Since the PHCI series publish monthly with no gaps within
    # a state's active range, the common-date set is the canonical
    # anchor surface; the lookback ``t - span_months`` is the
    # ``span_months``-th earlier element of ``sorted(common_dates)``
    # only when the cadence is strictly monthly — but rather than
    # assume cadence regularity, we compute the lookback by date
    # arithmetic (subtract span_months calendar months) and verify
    # presence per state.
    sorted_common = sorted(common_dates)

    out: list[dict[str, Any]] = []
    for t in sorted_common:
        lookback = _subtract_months(t, span_months)
        # The lookback month must be present in every state.
        missing_lookback = False
        n_decreasing = 0
        for state_map in by_state.values():
            v_t = state_map.get(t)
            v_lb = state_map.get(lookback)
            if v_t is None or v_lb is None:
                missing_lookback = True
                break
            if v_t < v_lb:
                n_decreasing += 1
        if missing_lookback:
            continue
        out.append({
            "date": t,
            "value": n_decreasing / n_states,
        })
    return out


def _subtract_months(d: date, months: int) -> date:
    """Calendar-month subtraction. ``date(2024, 3, 1) - 3 months ==
    date(2023, 12, 1)``. Day-of-month preserved when valid; clamped
    down when the target month is shorter (e.g. Mar 31 - 1mo → Feb 28).
    Phila Fed PHCI publishes on the first of the month, so the clamp
    is defensive — in practice every input is day=1.
    """
    y = d.year
    m = d.month - months
    while m < 1:
        m += 12
        y -= 1
    # Day clamp for safety (PHCI day=1 always, but be robust).
    day = d.day
    while day > 28:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1
    return date(y, m, day)


__all__ = ["DEFAULT_SPAN_MONTHS", "compute_sos_diffusion"]
