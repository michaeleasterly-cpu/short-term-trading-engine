"""Reversion partial-axis regime filter (2026-05-22) - pin the contract.

The autonomous finder's ``reversion_earnings_season_5d_range_normal``
candidate FAILED on 2026-05-22 with n_trades=0 because the full-4-axis
match for the candidate's regime tuple (``968624efa259`` = vol=normal,
trend=range, macro=expansion, sentiment=neutral) was too restrictive
(0 occurrences in the 2024-2025 final holdout). The partial-axis
choice menu (``vol_only`` / ``trend_only`` / ``macro_only`` /
``sentiment_only`` / ``vol_trend`` / ``full``) lets the LLM finder
condition on FEWER axes when the full match is non-actionable.

Coverage:

* C1  classifier thresholds match the snapshot SoT (the leftover
      classifier tests, retained as the pin against drift).
* C2  ``RegimeClassification.regime_tuple_id`` round-trips through
      :func:`_compute_regime_tuple_id`.
* C3  ``decompose_regime_tuple_id`` is the inverse of the SHA12 hash
      for every (vol, trend, macro, sentiment) combination - and the
      candidate's hash ``968624efa259`` decomposes to (normal, range,
      expansion, neutral).
* C4  ``session_matches_target`` for each of the 7 choice values
      (off, vol_only, trend_only, macro_only, sentiment_only,
      vol_trend, full).
* C5  ``REGIME_FILTER_CHOICES`` matches the LAB_TARGET ``choice:`` CSV.
* C6  LAB_TARGET declares ``regime_filter_v1`` as a 7-arm choice.
* C7  ``REVERSION_OVERRIDE_KEYS`` carries ``regime_filter_v1`` +
      ``regime_target``.
* C8  ``default_params()['regime_filter_v1'] == 'off'``.
* C9  per-call reset discipline: a run with regime_filter_v1 set
      leaves the module globals at None.
* C10 partial-axis mask: a synthetic context where the per-session
      regime classifies as the target on the selected axes shows a
      live trade; one where it doesn't shows none.
* C11 live byte-identicality: with regime_filter_v1=off (default),
      the result is identical to a run with no override at all.
* C12 fail-loud: regime_filter_v1!=off without regime_target raises
      ValueError; regime_filter_v1!=off without context.regime_bundle
      raises ValueError.
* C13 live module constants unchanged after a Lab variant run.
* C14 live-path import isolation: reversion.scheduler does NOT import
      reversion.regime_filter.

Fully hermetic - no DB, no network.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import date

import numpy as np
import pandas as pd
import pytest

from reversion import backtest as bt
from reversion.regime_filter import (
    REGIME_FILTER_CHOICES,
    RegimeBundle,
    RegimeClassification,
    _classify_macro,
    _classify_sentiment,
    _classify_trend,
    _classify_vol,
    classify_session,
    decompose_regime_tuple_id,
    is_earnings_season,
    regime_tuple_id_for_session,
    session_matches_target,
)
from tpcore.lab.llm_finder.models import _compute_regime_tuple_id

# Module-private accessor used in test fixtures - SLF allowlisted in
# pyproject.toml below.


# ── C1 — classifier thresholds (from the leftover-test contract) ────


def test_C1_vol_classifier_thresholds_match_snapshot_sot() -> None:
    """SoT: ``tpcore.lab.llm_finder.snapshot._VIX_*_HI`` constants."""
    assert _classify_vol(None) == "normal"
    assert _classify_vol(14.99) == "calm"
    assert _classify_vol(15.0) == "normal"
    assert _classify_vol(19.99) == "normal"
    assert _classify_vol(20.0) == "stress"
    assert _classify_vol(29.99) == "stress"
    assert _classify_vol(30.0) == "crisis"


def test_C1_macro_classifier_priority_order() -> None:
    """Sahm contraction > CFNAI contraction > yield_curve slowing > expansion."""
    assert _classify_macro(None, None, None) == "expansion"
    assert _classify_macro(None, None, -0.1) == "slowing"
    assert _classify_macro(None, -0.71, -0.1) == "contraction"
    assert _classify_macro(0.50, None, None) == "contraction"


def test_C1_sentiment_classifier_defaults_to_neutral_without_fear_greed() -> None:
    """We don't have a fear_greed series; per SoT, no extreme can be
    confirmed -> always neutral. Candidate's target regime is also
    neutral, so this is consistent."""
    empty = pd.DataFrame()
    assert _classify_sentiment(empty, date(2024, 1, 1)) == "neutral"
    df = pd.DataFrame(
        [{"date": pd.Timestamp("2024-01-01"), "bullish_pct": 80.0, "bearish_pct": 5.0}]
    ).set_index("date")
    assert _classify_sentiment(df, date(2024, 1, 15)) == "neutral"


def test_C1_trend_classifier_needs_200_bars() -> None:
    short = pd.Series(
        {pd.Timestamp(f"2024-01-{d:02d}"): 100.0 + d for d in range(1, 31)}
    )
    assert _classify_trend(short, date(2024, 1, 31)) == "range"


def test_C1_trend_classifier_flat_is_range() -> None:
    idx = pd.date_range("2023-01-01", periods=250, freq="B")
    flat = pd.Series([100.0] * 250, index=idx)
    assert _classify_trend(flat, idx[-1].date()) == "range"


def test_C1_trend_classifier_uptrend() -> None:
    idx = pd.date_range("2023-01-01", periods=250, freq="B")
    up = pd.Series([100.0 + i * 0.05 for i in range(250)], index=idx)
    assert _classify_trend(up, idx[-1].date()) == "trend_up"


def test_C1_target_regime_sha12_matches_snapshot_sot() -> None:
    """The candidate's regime_tuple_id MUST match the snapshot SoT hash
    for (range, normal, expansion, neutral) - the canonical
    decomposition pinned for this enrichment."""
    assert (
        _compute_regime_tuple_id("normal", "range", "expansion", "neutral")
        == "968624efa259"
    )


# ── C2 — RegimeClassification.regime_tuple_id round-trip ─────────────


def test_C2_classification_round_trips_through_sha12() -> None:
    cls = RegimeClassification(
        vol="normal", trend="range", macro="expansion", sentiment="neutral",
    )
    assert cls.regime_tuple_id == "968624efa259"


def test_C2_regime_tuple_id_for_session_synthetic_bundle() -> None:
    """Smoke - a self-consistent bundle resolves to the candidate's
    target regime."""
    idx = pd.date_range("2023-01-01", periods=250, freq="B")
    bundle = RegimeBundle(
        spy_close=pd.Series([100.0] * 250, index=idx),
        vix=pd.Series({idx[-1]: 18.0}),
        sahm=pd.Series(dtype=float),
        cfnai_ma3=pd.Series(dtype=float),
        yield_curve=pd.Series(dtype=float),
        aaii=pd.DataFrame(),
    )
    tid = regime_tuple_id_for_session(bundle, idx[-1].date())
    assert tid == "968624efa259"
    cls = classify_session(bundle, idx[-1].date())
    assert cls.vol == "normal"
    assert cls.trend == "range"
    assert cls.macro == "expansion"
    assert cls.sentiment == "neutral"


# ── C3 — decompose_regime_tuple_id inverse ───────────────────────────


def test_C3_decompose_candidate_target() -> None:
    """The candidate's hash decomposes to the documented axes."""
    cls = decompose_regime_tuple_id("968624efa259")
    assert cls.vol == "normal"
    assert cls.trend == "range"
    assert cls.macro == "expansion"
    assert cls.sentiment == "neutral"


def test_C3_decompose_round_trip_every_combo() -> None:
    """Every (vol, trend, macro, sentiment) -> hash -> decompose -> axes
    round-trips. Pins the 108-row enumeration table."""
    for vol in ("calm", "normal", "stress", "crisis"):
        for trend in ("range", "trend_up", "trend_down"):
            for macro in ("expansion", "slowing", "contraction"):
                for sent in ("extreme_bull", "neutral", "extreme_bear"):
                    h = _compute_regime_tuple_id(vol, trend, macro, sent)
                    cls = decompose_regime_tuple_id(h)
                    assert (cls.vol, cls.trend, cls.macro, cls.sentiment) == (
                        vol, trend, macro, sent
                    )


def test_C3_decompose_rejects_malformed_input() -> None:
    """Wrong-length or non-string hashes fail-loud."""
    with pytest.raises(ValueError, match="12-char string"):
        decompose_regime_tuple_id("968624efa25")  # 11 chars
    with pytest.raises(ValueError, match="12-char string"):
        decompose_regime_tuple_id("968624efa2599")  # 13 chars
    with pytest.raises(ValueError, match="12-char string"):
        decompose_regime_tuple_id(123)  # type: ignore[arg-type]


def test_C3_decompose_unknown_hash_raises_keyerror() -> None:
    """A 12-char string that doesn't correspond to any combination
    raises KeyError (not silently miss-match) - the decomposition
    table is the SoT."""
    with pytest.raises(KeyError):
        decompose_regime_tuple_id("aaaaaaaaaaaa")


# ── C4 — session_matches_target per choice value ─────────────────────


def _target() -> RegimeClassification:
    """The candidate's pre-registered target regime."""
    return RegimeClassification(
        vol="normal", trend="range", macro="expansion", sentiment="neutral",
    )


@pytest.mark.parametrize(
    "choice,session,expected",
    [
        # off - always True regardless of axes
        ("off", RegimeClassification(
            vol="calm", trend="trend_up", macro="slowing", sentiment="extreme_bull",
        ), True),
        # vol_only - matches if vol axis matches
        ("vol_only", RegimeClassification(
            vol="normal", trend="trend_up", macro="slowing", sentiment="extreme_bull",
        ), True),
        ("vol_only", RegimeClassification(
            vol="calm", trend="range", macro="expansion", sentiment="neutral",
        ), False),
        # trend_only - matches if trend axis matches
        ("trend_only", RegimeClassification(
            vol="calm", trend="range", macro="slowing", sentiment="extreme_bull",
        ), True),
        ("trend_only", RegimeClassification(
            vol="normal", trend="trend_up", macro="expansion", sentiment="neutral",
        ), False),
        # macro_only
        ("macro_only", RegimeClassification(
            vol="calm", trend="trend_up", macro="expansion", sentiment="extreme_bull",
        ), True),
        ("macro_only", RegimeClassification(
            vol="normal", trend="range", macro="contraction", sentiment="neutral",
        ), False),
        # sentiment_only
        ("sentiment_only", RegimeClassification(
            vol="calm", trend="trend_up", macro="slowing", sentiment="neutral",
        ), True),
        ("sentiment_only", RegimeClassification(
            vol="normal", trend="range", macro="expansion", sentiment="extreme_bear",
        ), False),
        # vol_trend - both vol AND trend
        ("vol_trend", RegimeClassification(
            vol="normal", trend="range", macro="slowing", sentiment="extreme_bull",
        ), True),
        ("vol_trend", RegimeClassification(
            vol="normal", trend="trend_up", macro="expansion", sentiment="neutral",
        ), False),
        ("vol_trend", RegimeClassification(
            vol="calm", trend="range", macro="expansion", sentiment="neutral",
        ), False),
        # full - all 4 axes
        ("full", RegimeClassification(
            vol="normal", trend="range", macro="expansion", sentiment="neutral",
        ), True),
        ("full", RegimeClassification(
            vol="normal", trend="range", macro="expansion", sentiment="extreme_bull",
        ), False),
        ("full", RegimeClassification(
            vol="calm", trend="range", macro="expansion", sentiment="neutral",
        ), False),
    ],
)
def test_C4_session_matches_target_per_choice(
    choice: str, session: RegimeClassification, expected: bool,
) -> None:
    assert session_matches_target(
        session_class=session, target_class=_target(), choice=choice,
    ) is expected


def test_C4_session_matches_target_unknown_choice_raises() -> None:
    with pytest.raises(ValueError, match="unknown regime_filter_v1 choice"):
        session_matches_target(
            session_class=_target(), target_class=_target(), choice="bogus",
        )


# ── C5 — REGIME_FILTER_CHOICES tuple matches the LAB_TARGET CSV ──────


def test_C5_regime_filter_choices_matches_lab_target_csv() -> None:
    """The module-public choice menu IS the LAB_TARGET param_ranges
    spec - the test ensures the two stay in sync."""
    _, _, kind = bt.LAB_TARGET.param_ranges["regime_filter_v1"]
    csv = kind.split(":", 1)[1]
    members = tuple(c.strip() for c in csv.split(","))
    assert members == REGIME_FILTER_CHOICES


# ── C6 — LAB_TARGET declares the 7-arm choice ────────────────────────


def test_C6_lab_target_regime_filter_v1_shape() -> None:
    spec = bt.LAB_TARGET.param_ranges["regime_filter_v1"]
    assert spec == (
        0,
        0,
        "choice:off,vol_only,trend_only,macro_only,sentiment_only,vol_trend,full",
    )


# ── C7 — REVERSION_OVERRIDE_KEYS carries the new keys ────────────────


def test_C7_override_keys_carry_regime_filter_v1_and_regime_target() -> None:
    assert "regime_filter_v1" in bt.REVERSION_OVERRIDE_KEYS
    assert "regime_target" in bt.REVERSION_OVERRIDE_KEYS


# ── C8 — default_params surfaces the new knob ────────────────────────


def test_C8_default_params_regime_filter_v1_is_off() -> None:
    assert bt.default_params()["regime_filter_v1"] == "off"


# ── C9 — per-call reset discipline ───────────────────────────────────


def _make_synthetic_panel(
    *, ticker: str, start: date, end: date, seed: int,
) -> pd.DataFrame:
    """Mirrors test_lab_pca_residual_byte_identical helper."""
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, end)
    n = len(sessions)
    if n < 100:
        raise AssertionError("fixture must span >= 100 sessions")
    rets = rng.normal(0.0005, 0.02, size=n)
    closes = 100.0 * np.exp(np.cumsum(rets))
    highs = closes * (1 + rng.uniform(0, 0.01, size=n))
    lows = closes * (1 - rng.uniform(0, 0.01, size=n))
    opens = closes * (1 + rng.normal(0, 0.005, size=n))
    volumes = rng.integers(1_500_000, 5_000_000, size=n)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(np.maximum(highs, opens), closes),
            "low": np.minimum(np.minimum(lows, opens), closes),
            "close": closes,
            "volume": volumes,
        },
        index=pd.Index([d.date() for d in sessions], name="date"),
    )
    df["ticker"] = ticker
    return bt._precompute_indicators(df)  # noqa: SLF001


def _make_bundle_for_target(
    *, start: date, end: date,
) -> RegimeBundle:
    """A regime bundle whose classification == the candidate's target
    on every session (vol=normal via VIX=18, trend=range via flat SPY,
    macro=expansion via empty macro, sentiment=neutral via empty AAII)."""
    sessions = pd.bdate_range(start, end)
    # SPY flat at 100 ⇒ trend=range
    spy_close = pd.Series([100.0] * len(sessions), index=sessions)
    # VIX=18 on each session ⇒ vol=normal
    vix = pd.Series([18.0] * len(sessions), index=sessions)
    return RegimeBundle(
        spy_close=spy_close,
        vix=vix,
        sahm=pd.Series(dtype=float),
        cfnai_ma3=pd.Series(dtype=float),
        yield_curve=pd.Series(dtype=float),
        aaii=pd.DataFrame(),
    )


def _make_bundle_off_target(
    *, start: date, end: date,
) -> RegimeBundle:
    """A regime bundle whose classification NEVER matches (normal,
    range, expansion, neutral): VIX=35 ⇒ vol=crisis on every session."""
    sessions = pd.bdate_range(start, end)
    spy_close = pd.Series([100.0] * len(sessions), index=sessions)
    vix = pd.Series([35.0] * len(sessions), index=sessions)
    return RegimeBundle(
        spy_close=spy_close,
        vix=vix,
        sahm=pd.Series(dtype=float),
        cfnai_ma3=pd.Series(dtype=float),
        yield_curve=pd.Series(dtype=float),
        aaii=pd.DataFrame(),
    )


def _make_synthetic_context(
    *,
    seed_base: int = 200,
    n_tickers: int = 10,
    with_regime_bundle: bool = False,
    on_target: bool = True,
) -> bt.ReversionWindowContext:
    start = date(2022, 1, 3)
    end = date(2023, 12, 29)
    panels = {
        f"TST{i:03d}": _make_synthetic_panel(
            ticker=f"TST{i:03d}", start=start, end=end, seed=seed_base + i,
        )
        for i in range(n_tickers)
    }
    spy = _make_synthetic_panel(ticker="SPY", start=start, end=end, seed=999)
    regime_bundle = None
    if with_regime_bundle:
        regime_bundle = (
            _make_bundle_for_target(start=start, end=end)
            if on_target
            else _make_bundle_off_target(start=start, end=end)
        )
    return bt.ReversionWindowContext(
        panels=panels,
        spy_panel=spy,
        fundamentals={},
        tier_round_trip_costs={t: 0.001 for t in panels},
        funded_tickers=list(panels.keys()),
        start=start,
        end=end,
        universe=tuple(panels.keys()),
        regime_bundle=regime_bundle,
    )


def test_C9_regime_filter_overrides_reset_per_call() -> None:
    """A variant run with regime_filter_v1 set leaves both module
    globals at None - the per-call reset discipline (mirror of the
    signal_mode C4 no-leak test)."""
    ctx = _make_synthetic_context(with_regime_bundle=True, on_target=True)
    bt.run_reversion_with_context(
        ctx,
        overrides={
            "regime_filter_v1": "trend_only",
            "regime_target": "968624efa259",
        },
    )
    assert bt._REGIME_FILTER_OVERRIDE is None, (  # noqa: SLF001
        "_REGIME_FILTER_OVERRIDE leaked across calls - per-call reset broken"
    )
    assert bt._REGIME_TARGET_OVERRIDE is None, (  # noqa: SLF001
        "_REGIME_TARGET_OVERRIDE leaked across calls - per-call reset broken"
    )


# ── C10 — partial-axis mask drives trade emission ────────────────────


def test_C10_on_target_bundle_does_not_block_trades() -> None:
    """A bundle whose every session matches the target on the trend
    axis should NOT filter out trades on the trend_only choice. The
    result has the same trade count as the off-flag run."""
    ctx_with = _make_synthetic_context(with_regime_bundle=True, on_target=True)
    ctx_without = _make_synthetic_context(with_regime_bundle=False)
    result_with = bt.run_reversion_with_context(
        ctx_with,
        overrides={
            "regime_filter_v1": "trend_only",
            "regime_target": "968624efa259",
        },
    )
    result_off = bt.run_reversion_with_context(
        ctx_without, overrides={},
    )
    # The bundle-on-target passes the gate every session, so trade
    # counts are identical to the no-gate baseline.
    assert result_with.trades == result_off.trades, (
        f"on-target bundle should not block any trades: "
        f"got {result_with.trades} (gated) vs {result_off.trades} (off)"
    )
    assert result_with.parameters["regime_filter_v1"] == "trend_only"
    assert result_with.parameters["regime_target"] == "968624efa259"


def test_C10_off_target_bundle_blocks_trades() -> None:
    """A bundle whose every session is OFF the target (vol=crisis vs
    target vol=normal) should produce zero trades under vol_only -
    the gate filters every session out."""
    ctx = _make_synthetic_context(with_regime_bundle=True, on_target=False)
    result = bt.run_reversion_with_context(
        ctx,
        overrides={
            "regime_filter_v1": "vol_only",
            "regime_target": "968624efa259",
        },
    )
    assert result.trades == 0, (
        f"off-target bundle should block all trades under vol_only: "
        f"got {result.trades}"
    )


def test_C10_off_choice_is_no_op_regardless_of_bundle() -> None:
    """With regime_filter_v1='off', the regime_bundle is ignored and
    trade emission matches the no-override baseline byte-identically.
    """
    ctx_on = _make_synthetic_context(with_regime_bundle=True, on_target=False)
    ctx_off = _make_synthetic_context(with_regime_bundle=False)
    # The off-target bundle is attached but the gate is 'off' - it
    # MUST NOT consult the bundle.
    result_with_bundle = bt.run_reversion_with_context(
        ctx_on, overrides={"regime_filter_v1": "off"},
    )
    result_no_bundle = bt.run_reversion_with_context(
        ctx_off, overrides={},
    )
    assert result_with_bundle.trades == result_no_bundle.trades


# ── C11 — byte-identical with the flag off ───────────────────────────


def test_C11_omitting_override_is_byte_identical_to_off() -> None:
    """Adding ``regime_filter_v1`` to the override surface MUST NOT
    change the result of a run that doesn't sample it. Pin this by
    comparing a call with ``overrides={}`` to a call with
    ``overrides={'regime_filter_v1': 'off'}``."""
    ctx_a = _make_synthetic_context()
    ctx_b = _make_synthetic_context()
    res_implicit = bt.run_reversion_with_context(ctx_a, overrides={})
    res_explicit = bt.run_reversion_with_context(
        ctx_b, overrides={"regime_filter_v1": "off"},
    )
    assert res_implicit.trades == res_explicit.trades
    assert res_implicit.sharpe == res_explicit.sharpe
    assert res_implicit.max_drawdown == res_explicit.max_drawdown
    # Parameters block now includes regime_filter_v1: 'off' on both
    # paths (default surfaces from _regime_filter_v1()).
    assert res_implicit.parameters["regime_filter_v1"] == "off"
    assert res_explicit.parameters["regime_filter_v1"] == "off"


# ── C12 — fail-loud on misconfiguration ──────────────────────────────


def test_C12_regime_filter_v1_without_regime_target_fails_loud() -> None:
    ctx = _make_synthetic_context(with_regime_bundle=True, on_target=True)
    with pytest.raises(ValueError, match="regime_target is None"):
        bt.run_reversion_with_context(
            ctx, overrides={"regime_filter_v1": "trend_only"},
        )


def test_C12_regime_filter_v1_without_bundle_fails_loud() -> None:
    ctx = _make_synthetic_context(with_regime_bundle=False)
    with pytest.raises(ValueError, match="context.regime_bundle is None"):
        bt.run_reversion_with_context(
            ctx,
            overrides={
                "regime_filter_v1": "trend_only",
                "regime_target": "968624efa259",
            },
        )


# ── C13 — live module constants unchanged after a Lab variant run ────


def test_C13_live_module_constants_unchanged_after_regime_variant_run() -> None:
    """A regime-filter variant run leaves the live reversion.models
    constants byte-identical. The override is a backtest-only global
    in reversion.backtest only.
    """
    import reversion.models as _models

    before_hard_stop = _models.HARD_STOP_PCT
    before_time_stop = _models.TIME_STOP_DAYS
    before_max_adx = _models.MAX_ADX_FOR_REVERSION
    ctx = _make_synthetic_context(with_regime_bundle=True, on_target=True)
    bt.run_reversion_with_context(
        ctx,
        overrides={
            "regime_filter_v1": "vol_trend",
            "regime_target": "968624efa259",
        },
    )
    assert _models.HARD_STOP_PCT == before_hard_stop
    assert _models.TIME_STOP_DAYS == before_time_stop
    assert _models.MAX_ADX_FOR_REVERSION == before_max_adx


# ── C14 — live-path import isolation ─────────────────────────────────


def test_C14_live_scheduler_does_not_import_regime_filter() -> None:
    """Subprocess probe: importing ``reversion.scheduler`` MUST NOT
    pull in ``reversion.regime_filter`` (which is Lab-only). Mirror of
    the lab_pca_residual C8 test."""
    code = textwrap.dedent(
        """
        import sys

        import reversion.scheduler  # noqa: F401

        forbidden = (
            "reversion.regime_filter",
            "reversion.backtest",
            "reversion.lab_pca_residual",
        )
        offenders = [m for m in forbidden if m in sys.modules]
        if offenders:
            print("LEAK:", ",".join(offenders))
            sys.exit(1)
        sys.exit(0)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, (
        f"live scheduler import leaked Lab-only modules: stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )


# ── Calendar shim retained from the leftover-test contract ───────────


@pytest.mark.parametrize(
    "month,expected",
    [
        (1, True), (2, True), (3, False), (4, True), (5, True),
        (6, False), (7, True), (8, True), (9, False), (10, True),
        (11, True), (12, False),
    ],
)
def test_is_earnings_season_calendar(month: int, expected: bool) -> None:
    assert is_earnings_season(date(2024, month, 15)) is expected


# ── _build_session_regime_mask returns None when gate is off ─────────


def test_build_session_regime_mask_returns_none_for_off_choice() -> None:
    """Private accessor smoke - confirms that the gate-off short-
    circuit returns None (no per-session classification overhead).
    """
    ctx = _make_synthetic_context(with_regime_bundle=False)
    # Module-global override must be the same as a fresh-process state.
    bt._REGIME_FILTER_OVERRIDE = None  # noqa: SLF001
    bt._REGIME_TARGET_OVERRIDE = None  # noqa: SLF001
    assert bt._build_session_regime_mask(ctx) is None  # noqa: SLF001
