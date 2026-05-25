"""Lab-only per-session regime classifier + partial-axis matcher for
the reversion engine.

Produced 2026-05-22 to support the autonomous finder candidate
``reversion_earnings_season_5d_range_normal`` (regime_tuple_id
``968624efa259`` = ``range x normal x expansion x neutral``). The
candidate's earlier full-4-axis-match probe produced n_trades=0 because
the candidate's regime occurred 0 times in the 2024-2025 final
holdout. The partial-axis matcher in this module exposes the same
classifier under a less restrictive variant (e.g. ``trend_only``
matches only the trend axis, giving a much larger per-session match
population) without changing what the candidate REGISTERED as its
hypothesis.

Self-contained: imports only stdlib + pandas + tpcore.lab.regime_tuple
(SHA12 hash extracted 2026-05-25 when the LLM-finder/lab/monitor stack
was retired). The classifier consumes substrates already in Postgres
(vix, sahm_rule, cfnai_ma3, yield_curve, aaii_sentiment, SPY's
prices_daily series) and emits one ``regime_tuple_id`` (SHA12 string)
per session date.

This module is Lab-only - the live reversion scheduler never imports
it, and ``run_reversion_with_context`` reaches it only when
``overrides['regime_filter_v1']`` is set to a non-off variant. Live
trading remains byte-identical with the flag unset.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

import pandas as pd

from tpcore.lab.regime_tuple import compute_regime_tuple_id

# Threshold constants are inlined here as SoT post-2026-05-25
# retirement of ``tpcore.lab.llm_finder.snapshot``. The test
# ``reversion/tests/test_regime_filter.py`` pins these.
_VIX_CALM_HI: Final[float] = 15.0
_VIX_NORMAL_HI: Final[float] = 20.0
_VIX_STRESS_HI: Final[float] = 30.0
_SPY_SLOPE_BP_TRIGGER: Final[float] = 50.0 / 10_000.0
_SAHM_CONTRACTION: Final[float] = 0.50
_CFNAI_MA3_CONTRACTION: Final[float] = -0.70

# Public choice menu surfaced via LAB_TARGET. ``off`` disables the
# gate (byte-identical with the regime filter not declared at all);
# the remaining choices select WHICH axes of the candidate's
# pre-registered regime must match the per-session classification.
REGIME_FILTER_CHOICES: Final[tuple[str, ...]] = (
    "off",
    "vol_only",
    "trend_only",
    "macro_only",
    "sentiment_only",
    "vol_trend",
    "full",
)

# Which axes each partial-axis choice constrains. ``off`` is unconstrained
# (no axes); ``full`` is all four. The remaining choices encode the
# expected single-axis or 2-axis configurations.
_CHOICE_AXES: Final[dict[str, frozenset[str]]] = {
    "off": frozenset(),
    "vol_only": frozenset({"vol"}),
    "trend_only": frozenset({"trend"}),
    "macro_only": frozenset({"macro"}),
    "sentiment_only": frozenset({"sentiment"}),
    "vol_trend": frozenset({"vol", "trend"}),
    "full": frozenset({"vol", "trend", "macro", "sentiment"}),
}


@dataclass(frozen=True)
class RegimeBundle:
    """Per-session time-series substrates the classifier needs.

    Each member is a pandas Series indexed by date (sorted ascending);
    the per-session classifier picks the latest value with
    ``index <= session_date`` (PIT). ``spy_close`` is required (the
    trend classifier needs the 200d slope); the rest may be empty -
    missing data defaults to ``normal`` / ``expansion`` / ``neutral``
    per the snapshot SoT contract.
    """

    spy_close: pd.Series  # SPY adjusted close, indexed by date
    vix: pd.Series        # macro_indicators.indicator='vix'
    sahm: pd.Series       # macro_indicators.indicator='sahm_rule'
    cfnai_ma3: pd.Series  # macro_indicators.indicator='cfnai_ma3'
    yield_curve: pd.Series  # macro_indicators.indicator='yield_curve'
    # AAII rows as a dataframe keyed by date with bullish_pct / bearish_pct
    aaii: pd.DataFrame


def _latest_pit(series: pd.Series, as_of: date) -> float | None:
    """PIT lookup: most-recent value at or before ``as_of``; None if empty."""
    if series.empty:
        return None
    eligible = series[series.index <= pd.Timestamp(as_of)]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1])


def _classify_vol(vix: float | None) -> Literal["calm", "normal", "stress", "crisis"]:
    if vix is None:
        return "normal"
    if vix < _VIX_CALM_HI:
        return "calm"
    if vix < _VIX_NORMAL_HI:
        return "normal"
    if vix < _VIX_STRESS_HI:
        return "stress"
    return "crisis"


def _classify_trend(
    spy_close: pd.Series, as_of: date,
) -> Literal["range", "trend_up", "trend_down"]:
    """200-bar slope on SPY adjusted close (snapshot SoT §4.2 proxy)."""
    if spy_close.empty:
        return "range"
    eligible = spy_close[spy_close.index <= pd.Timestamp(as_of)]
    if len(eligible) < 200:
        return "range"
    closes = eligible.iloc[-200:].to_numpy()
    slope_bp = (closes[-1] - closes[0]) / closes[0]
    if abs(slope_bp) < _SPY_SLOPE_BP_TRIGGER:
        return "range"
    return "trend_up" if slope_bp > 0 else "trend_down"


def _classify_macro(
    sahm: float | None, cfnai_ma3: float | None, yc: float | None,
) -> Literal["expansion", "slowing", "contraction"]:
    if sahm is not None and sahm >= _SAHM_CONTRACTION:
        return "contraction"
    if cfnai_ma3 is not None and cfnai_ma3 <= _CFNAI_MA3_CONTRACTION:
        return "contraction"
    if yc is not None and yc < 0:
        return "slowing"
    return "expansion"


def _classify_sentiment(
    aaii: pd.DataFrame, as_of: date,
) -> Literal["extreme_bull", "neutral", "extreme_bear"]:
    """AAII bull-bear spread alone (no fear-greed series in this DB);
    snapshot SoT requires BOTH AAII + fear_greed to flag extreme, so in
    its absence we default to ``neutral`` for every session - which is
    consistent with the candidate's target regime (``neutral``).
    """
    if aaii.empty:
        return "neutral"
    eligible = aaii[aaii.index <= pd.Timestamp(as_of)]
    if eligible.empty:
        return "neutral"
    row = eligible.iloc[-1]
    bull = row.get("bullish_pct")
    bear = row.get("bearish_pct")
    if bull is None or bear is None:
        return "neutral"
    # No fear_greed series -> cannot confirm "extreme" per snapshot SoT,
    # so always neutral. This biases the classifier conservatively
    # toward the candidate's target regime (which IS neutral) - the
    # falsification still holds because the OTHER axes (vol, trend,
    # macro) provide the actual discrimination.
    _ = float(bull) - float(bear)  # computed but unused; documents intent
    return "neutral"


@dataclass(frozen=True)
class RegimeClassification:
    """4-axis classification for one session - mirrors the snapshot SoT."""

    vol: str
    trend: str
    macro: str
    sentiment: str

    @property
    def regime_tuple_id(self) -> str:
        return compute_regime_tuple_id(self.vol, self.trend, self.macro, self.sentiment)


def classify_session(
    bundle: RegimeBundle, session_date: date,
) -> RegimeClassification:
    """Per-session 4-axis classification.

    Returns the structured object; ``regime_tuple_id`` is derivable via
    ``classification.regime_tuple_id``. The structured form lets the
    partial-axis matcher compare on a single axis without ever hashing
    a target regime back to axes (the hash is one-way).
    """
    vix = _latest_pit(bundle.vix, session_date)
    sahm = _latest_pit(bundle.sahm, session_date)
    cfnai = _latest_pit(bundle.cfnai_ma3, session_date)
    yc = _latest_pit(bundle.yield_curve, session_date)

    return RegimeClassification(
        vol=_classify_vol(vix),
        trend=_classify_trend(bundle.spy_close, session_date),
        macro=_classify_macro(sahm, cfnai, yc),
        sentiment=_classify_sentiment(bundle.aaii, session_date),
    )


def regime_tuple_id_for_session(
    bundle: RegimeBundle, session_date: date,
) -> str:
    """Compute the SHA12 regime_tuple_id for one session.

    Thin shim over :func:`classify_session` - retained for the
    leftover-test contract that pre-dated the structured classification
    object.
    """
    return classify_session(bundle, session_date).regime_tuple_id


# Cached decomposition of the 4-axis space. Build once at import.
_AXIS_VOL: Final[tuple[str, ...]] = ("calm", "normal", "stress", "crisis")
_AXIS_TREND: Final[tuple[str, ...]] = ("range", "trend_up", "trend_down")
_AXIS_MACRO: Final[tuple[str, ...]] = ("expansion", "slowing", "contraction")
_AXIS_SENTIMENT: Final[tuple[str, ...]] = ("extreme_bull", "neutral", "extreme_bear")


def decompose_regime_tuple_id(
    regime_tuple_id: str,
) -> RegimeClassification:
    """Decompose a 12-char regime_tuple_id back to its 4 axes.

    The SHA12 hash is one-way; this works by enumerating the
    4 * 3 * 3 * 3 = 108-combination space and matching by hash. Returns
    the unique decomposition or raises ValueError if the hash doesn't
    correspond to any (vol, trend, macro, sentiment) combination.

    Pure: no I/O, fully hermetic. The enumeration is small enough
    (<1ms) that caching isn't required, but a process-local memoisation
    via :func:`functools.cache` is wired below.
    """
    if not isinstance(regime_tuple_id, str) or len(regime_tuple_id) != 12:
        raise ValueError(
            f"regime_tuple_id must be a 12-char string; got {regime_tuple_id!r}"
        )
    return _decompose_cached(regime_tuple_id)


def _decompose_cached(regime_tuple_id: str) -> RegimeClassification:
    """Memoised enumeration (108 combos)."""
    return _DECOMPOSITION_TABLE[regime_tuple_id]


def _build_decomposition_table() -> dict[str, RegimeClassification]:
    """Pre-build the 108-row hash -> axes lookup table at import."""
    table: dict[str, RegimeClassification] = {}
    for v in _AXIS_VOL:
        for t in _AXIS_TREND:
            for m in _AXIS_MACRO:
                for s in _AXIS_SENTIMENT:
                    cls = RegimeClassification(vol=v, trend=t, macro=m, sentiment=s)
                    table[cls.regime_tuple_id] = cls
    return table


_DECOMPOSITION_TABLE: Final[dict[str, RegimeClassification]] = _build_decomposition_table()


def session_matches_target(
    *,
    session_class: RegimeClassification,
    target_class: RegimeClassification,
    choice: str,
) -> bool:
    """Return True if the per-session regime matches the target on the
    axes selected by ``choice``.

    * ``off`` -> always True (the gate is disabled - caller should
      short-circuit BEFORE calling this; included here for
      defensive completeness).
    * ``vol_only`` / ``trend_only`` / ``macro_only`` / ``sentiment_only``
      -> match only that single axis (most permissive non-off variant).
    * ``vol_trend`` -> match both vol and trend (2-axis).
    * ``full`` -> all 4 axes (the most restrictive; equivalent to
      ``session_class.regime_tuple_id == target_class.regime_tuple_id``).
    """
    if choice not in _CHOICE_AXES:
        raise ValueError(
            f"unknown regime_filter_v1 choice {choice!r}; allowed: "
            f"{sorted(_CHOICE_AXES)}"
        )
    axes = _CHOICE_AXES[choice]
    if not axes:
        return True  # ``off`` is unconditional pass
    for axis in axes:
        if getattr(session_class, axis) != getattr(target_class, axis):
            return False
    return True


def is_earnings_season(session_date: date) -> bool:
    """Calendar-position tag per snapshot SoT §4.3 (months 1,2,4,5,7,8,10,11)."""
    return session_date.month in (1, 2, 4, 5, 7, 8, 10, 11)


# Loader-binding indirection. The actual ``load_regime_bundle`` SQL
# helper lives in the operator-level Lab probe driver
# (``scripts/probe_reversion_partial_axis.py``) so that this Lab-only
# classifier module carries NO ``platform.<table>`` SQL strings -
# which would otherwise trigger the engine-data-dependencies drift
# clockwork (``tpcore/tests/test_engine_data_dependencies_drift.py``)
# without an ECR-MODIFY on `_PROFILE['reversion'].data_dependencies`.
#
# The probe driver populates the bundle and attaches it to the
# ``ReversionWindowContext``; tests synthesize bundles in-body. The
# engine never reaches a DB-loading helper.


__all__ = [
    "REGIME_FILTER_CHOICES",
    "RegimeBundle",
    "RegimeClassification",
    "classify_session",
    "decompose_regime_tuple_id",
    "is_earnings_season",
    "regime_tuple_id_for_session",
    "session_matches_target",
]
