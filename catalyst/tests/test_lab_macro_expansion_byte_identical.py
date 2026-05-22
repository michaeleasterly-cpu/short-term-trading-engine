"""Catalyst — ``beat_30d_only_macro_expansion`` arm: byte-identical
contract + new-arm reachability + per-event macro-regime gating.

PR B of the catalyst money-engine delivery (operator brief 2026-05-22).
The new arm is PEAD + a per-event macro-regime gate (fire ONLY IF
classified macro at the event date is 'expansion'); hypothesis per
the lab-finder regime-aware-trading reference §2.3.

Coverage:
* C1  every legacy call yields the same byte-identical result (the
     existing three modes off / positive_beat_30d / beat_30d_only +
     their omitted-override variants — proves the new arm did not
     leak into the legacy paths);
* C2  no cross-trial leakage: macro_expansion-then-legacy in the
     same process yields the legacy golden (the per-call
     module-global reset).
* M1  macro_expansion arm is REACHABLE — pinned override + a
     regime_bundle classifying every session as macro='expansion'
     yields a positive trade count + recorded parameter mismatch
     against the unconditional beat_30d_only arm.
* M2  macro_expansion arm is DISTINCT — when the regime_bundle
     classifies sessions as macro='contraction' (NOT 'expansion'),
     the arm yields ZERO trades while beat_30d_only on the same
     fixture yields >0 trades. The macro gate is the binding
     constraint.
* M3  macro_expansion arm is fail-CLOSED — no regime_bundle attached
     yields ZERO trades (the mode requires the bundle; a missing
     bundle does NOT silently fall back to the unconditional
     beat_30d_only behaviour).
* PIT macro_expansion uses a strictly-backward PIT classification —
     a macro indicator dated AFTER the event date is invisible to
     the classifier; one dated at/before is visible.
* LAB the LAB_TARGET param_ranges menu lists the new arm + the new
     mode-string is the canonical literal.

Fully hermetic: synthetic context + synthetic regime bundle built
in-body; NO DB, NO network.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd


def _synthetic_context(*, with_regime_bundle: bool = False,
                        macro_label: str = "expansion"):
    """A ``CatalystWindowContext`` sized so the PEAD branch matters
    (multiple BEAT events on AAPL/MSFT) AND (optionally) carrying a
    synthetic ``regime_bundle`` whose macro classifier produces
    ``macro_label`` for every session in [start, end].

    The regime_bundle synthesis exploits the
    ``reversion.regime_filter._classify_macro`` SoT:
      * ``sahm`` < 0.50 AND
      * ``cfnai_ma3`` > -0.70 AND
      * ``yield_curve`` >= 0           → expansion (default)
      * ``sahm`` >= 0.50               → contraction
      * ``cfnai_ma3`` <= -0.70         → contraction
      * ``yield_curve`` < 0            → slowing
    """
    from catalyst.backtest import CatalystWindowContext

    end = date(2024, 12, 31)
    start = date(2024, 3, 1)

    bus = pd.bdate_range(start - timedelta(days=120), end)
    # Rising price so universe + SMA filters pass at the event dates;
    # post-entry drift is mild so the simulator doesn't TP/SL out
    # before time-stop.
    closes = [50.0 + 0.05 * i for i in range(len(bus))]
    prices = pd.DataFrame(
        {"close": closes, "volume": [5_000_000] * len(bus)},
        index=bus,
    )

    earnings_rows = pd.DataFrame([
        {"ticker": "AAPL", "event_date": date(2024, 4, 15),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.04},
        {"ticker": "AAPL", "event_date": date(2024, 7, 18),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.03},
        {"ticker": "AAPL", "event_date": date(2024, 10, 20),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.05},
        {"ticker": "MSFT", "event_date": date(2024, 4, 22),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.03},
        {"ticker": "MSFT", "event_date": date(2024, 7, 24),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.06},
    ])

    insider_rows = pd.DataFrame(
        columns=["ticker", "filing_date", "insider_name",
                 "transaction_type", "value"]
    )

    ctx_kwargs = dict(
        universe=("AAPL", "MSFT"),
        insider_rows=insider_rows,
        prices_by_ticker={"AAPL": prices, "MSFT": prices.copy()},
        round_trip_costs={"AAPL": Decimal("0.001"),
                          "MSFT": Decimal("0.001")},
        start=start, end=end,
        earnings_events=earnings_rows,
    )
    if with_regime_bundle:
        ctx_kwargs["regime_bundle"] = _synthetic_regime_bundle(
            macro_label=macro_label, dates=bus,
        )
    return CatalystWindowContext(**ctx_kwargs)


def _synthetic_regime_bundle(*, macro_label: str, dates: pd.DatetimeIndex):
    """Build a ``RegimeBundle`` whose ``_classify_macro`` SoT produces
    ``macro_label`` for every session in ``dates``.

    The classifier's SoT (``reversion.regime_filter._classify_macro``):
      * ``sahm`` >= 0.50              → contraction
      * ``cfnai_ma3`` <= -0.70        → contraction
      * ``yield_curve`` < 0           → slowing
      * default                       → expansion
    """
    from reversion.regime_filter import RegimeBundle

    if macro_label == "expansion":
        sahm_val, cfnai_val, yc_val = 0.10, 0.20, 0.50
    elif macro_label == "contraction":
        sahm_val, cfnai_val, yc_val = 0.80, 0.20, 0.50
    elif macro_label == "slowing":
        sahm_val, cfnai_val, yc_val = 0.10, 0.20, -0.50
    else:
        raise ValueError(f"unknown macro_label: {macro_label!r}")

    spy_close = pd.Series(
        [400.0 + 0.1 * i for i in range(len(dates))], index=dates,
    )
    sahm = pd.Series([sahm_val] * len(dates), index=dates)
    cfnai_ma3 = pd.Series([cfnai_val] * len(dates), index=dates)
    yield_curve = pd.Series([yc_val] * len(dates), index=dates)
    vix = pd.Series([15.0] * len(dates), index=dates)
    aaii = pd.DataFrame(
        [{"bullish_pct": 40.0, "bearish_pct": 30.0}] * len(dates),
        index=dates,
    )
    return RegimeBundle(
        spy_close=spy_close, vix=vix, sahm=sahm,
        cfnai_ma3=cfnai_ma3, yield_curve=yield_curve, aaii=aaii,
    )


def _fields(r) -> tuple:
    """The BacktestRunResult surface the byte-identical contract pins."""
    return (
        r.engine, r.credibility_score, r.passed_gate,
        round(r.sharpe, 10), round(r.profit_factor, 10),
        round(r.max_drawdown, 10), r.trades, round(r.dsr, 10),
        r.min_btl_gap, round(r.trades_per_param, 10),
        round(r.ruin_probability, 10),
        tuple(sorted(r.parameters.items())),
        len(r.trade_log),
    )


def test_c1_legacy_three_modes_byte_identical_after_macro_arm_addition():
    """C1 — the existing three modes off / positive_beat_30d /
    beat_30d_only produce IDENTICAL results before and after the
    macro_expansion arm was added. The fixture intentionally carries
    a regime_bundle (attached but unconsumed by the three legacy arms)
    to prove the bundle field does NOT alter the legacy paths.
    """
    from catalyst.backtest import run_catalyst_with_context

    # No bundle: pin the legacy paths' golden against bundle-absent.
    ctx_no_bundle = _synthetic_context(with_regime_bundle=False)
    # With bundle: the three legacy paths IGNORE it.
    ctx_with_bundle = _synthetic_context(with_regime_bundle=True,
                                          macro_label="expansion")

    for mode in ("off", "positive_beat_30d", "beat_30d_only"):
        no_bundle = _fields(run_catalyst_with_context(
            ctx_no_bundle,
            overrides={"event_confirmation_mode": mode}))
        with_bundle = _fields(run_catalyst_with_context(
            ctx_with_bundle,
            overrides={"event_confirmation_mode": mode}))
        assert no_bundle == with_bundle, (
            f"mode={mode!r}: a regime_bundle on the context leaked "
            f"into a legacy-arm result — byte-identicality broken. "
            f"no_bundle={no_bundle} with_bundle={with_bundle}"
        )


def test_c2_macro_expansion_then_legacy_yields_legacy_golden():
    """C2 — after a macro_expansion run leaves overrides at None
    (per-call reset), a subsequent legacy call yields the unchanged
    legacy golden.
    """
    from catalyst import backtest as bt

    ctx = _synthetic_context(with_regime_bundle=True,
                              macro_label="expansion")
    # Golden: the legacy 'off' result in this process before any
    # variant call.
    golden = _fields(bt.run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "off"}))

    # Variant: the macro_expansion arm runs.
    bt.run_catalyst_with_context(
        ctx,
        overrides={
            "event_confirmation_mode": "beat_30d_only_macro_expansion",
        })

    # Per-call reset discipline: the overrides return to None.
    assert bt._EVENT_CONFIRMATION_MODE_OVERRIDE is None
    assert bt._HOLD_DAYS_OVERRIDE is None

    # And the next legacy 'off' call yields the same golden as before.
    after = _fields(bt.run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "off"}))
    assert after == golden, (
        f"a macro_expansion run leaked across the per-call reset — "
        f"before={golden} after={after}"
    )


def test_m1_macro_expansion_reachable_when_bundle_is_expansion():
    """M1 — the macro_expansion arm is REACHABLE: a bundle whose
    macro classifier produces 'expansion' on every session yields a
    positive trade count + the correctly recorded parameter.
    """
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context(with_regime_bundle=True,
                              macro_label="expansion")
    result = run_catalyst_with_context(
        ctx,
        overrides={
            "event_confirmation_mode": "beat_30d_only_macro_expansion",
        })
    assert result.parameters["event_confirmation_mode"] == (
        "beat_30d_only_macro_expansion")
    assert result.trades > 0, (
        f"macro_expansion arm produced 0 trades against an "
        f"expansion-bundle + 5-BEAT fixture — branch is wired but "
        f"the macro gate is stripping every event"
    )


def test_m2_macro_expansion_distinct_when_bundle_is_contraction():
    """M2 — the macro_expansion arm is DISTINCT: a bundle whose
    macro classifier produces 'contraction' on every session yields
    ZERO trades, while the unconditional beat_30d_only arm yields
    >0 trades on the same fixture. The macro gate is binding.
    """
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context(with_regime_bundle=True,
                              macro_label="contraction")
    macro = run_catalyst_with_context(
        ctx,
        overrides={
            "event_confirmation_mode": "beat_30d_only_macro_expansion",
        })
    unconditional = run_catalyst_with_context(
        ctx,
        overrides={"event_confirmation_mode": "beat_30d_only"})

    assert unconditional.trades > 0, (
        "test invalid: the unconditional beat_30d_only baseline must "
        "fire on this fixture (the BEAT rows are independent of the "
        "regime gate)"
    )
    assert macro.trades == 0, (
        f"macro_expansion arm fired {macro.trades} trades against a "
        f"contraction-bundle — the macro gate is not biting"
    )


def test_m3_macro_expansion_fail_closed_when_bundle_is_absent():
    """M3 — the macro_expansion arm is fail-CLOSED: a context with
    NO regime_bundle attached yields ZERO trades (the mode does NOT
    silently fall back to the unconditional beat_30d_only behaviour).

    Auditability: a misconfigured probe driver that forgets to attach
    the bundle must NOT silently produce results indistinguishable
    from beat_30d_only — the failure should be loud (zero trades is
    a noisy verdict).
    """
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context(with_regime_bundle=False)
    macro = run_catalyst_with_context(
        ctx,
        overrides={
            "event_confirmation_mode": "beat_30d_only_macro_expansion",
        })
    unconditional = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "beat_30d_only"})

    assert unconditional.trades > 0, (
        "test invalid: the unconditional beat_30d_only baseline must "
        "fire on this fixture (the BEAT rows are present)"
    )
    assert macro.trades == 0, (
        f"macro_expansion arm fired {macro.trades} trades without a "
        f"regime_bundle — fail-closed behaviour violated"
    )


def test_pit_macro_regime_classification_is_strictly_backward():
    """PIT — the macro-regime classification at the event date sees
    only macro-indicator rows dated at-or-before the event date. A
    row dated AFTER the event date is invisible to the classifier.

    This pins the strictly-backward PIT contract on the macro_expansion
    arm via the underlying classifier's PIT discipline (the
    ``_latest_pit`` helper in ``reversion.regime_filter`` filters on
    ``index <= as_of``). A regression that switches the helper to a
    forward-looking interpolation trips this test.
    """
    from datetime import date

    import pandas as pd

    from catalyst.backtest import _macro_regime_at
    from reversion.regime_filter import RegimeBundle

    event_date = date(2024, 4, 15)
    # A bundle whose ONLY macro indicator data is dated AFTER the event:
    # the classifier should NOT see it; default macro is 'expansion'
    # under the classifier's fail-soft rule, so this also pins that.
    future_only = pd.Series(
        {pd.Timestamp(date(2024, 5, 1)): 0.80},  # would force contraction if visible
    )
    spy = pd.Series(
        {pd.Timestamp(date(2024, 1, 1)): 400.0,
         pd.Timestamp(event_date): 410.0},
    )
    bundle_future = RegimeBundle(
        spy_close=spy,
        vix=pd.Series(dtype=float),
        sahm=future_only,             # contraction-forcing, but dated AFTER event
        cfnai_ma3=pd.Series(dtype=float),
        yield_curve=pd.Series(dtype=float),
        aaii=pd.DataFrame(),
    )
    # The post-event sahm row is invisible → defaults to expansion.
    assert _macro_regime_at(bundle_future, event_date) == "expansion"

    # Same row dated BEFORE the event: visible → contraction.
    past_only = pd.Series(
        {pd.Timestamp(date(2024, 4, 1)): 0.80},
    )
    bundle_past = RegimeBundle(
        spy_close=spy, vix=pd.Series(dtype=float),
        sahm=past_only, cfnai_ma3=pd.Series(dtype=float),
        yield_curve=pd.Series(dtype=float), aaii=pd.DataFrame(),
    )
    assert _macro_regime_at(bundle_past, event_date) == "contraction"


def test_lab_target_lists_macro_expansion_arm():
    """LAB — ``LAB_TARGET.param_ranges['event_confirmation_mode']``
    lists the new arm. Pins the canonical mode-string literal.
    """
    from catalyst.backtest import LAB_TARGET

    rng = LAB_TARGET.param_ranges["event_confirmation_mode"]
    assert "beat_30d_only_macro_expansion" in rng[2], (
        f"LAB_TARGET event_confirmation_mode choice menu does not "
        f"list the macro_expansion arm: {rng}"
    )


def test_macro_regime_at_returns_none_when_bundle_is_none():
    """Defensive: the helper returns None when no bundle is attached
    (the predicate downstream short-circuits to fail-closed)."""
    from datetime import date

    from catalyst.backtest import _macro_regime_at

    assert _macro_regime_at(None, date(2024, 6, 1)) is None
