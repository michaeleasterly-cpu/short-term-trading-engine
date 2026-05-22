"""Catalyst — engine surface enrichment (2026-05-22): the
``beat_30d_only`` pure-PEAD arm + the Lab-sampled ``hold_days`` knob.

Context: the autonomous finder's ``catalyst_pead_expansion_range``
candidate FAILED on 2026-05-22 with ``n_trades=2`` because the
``event_confirmation_mode`` toggle's two prior arms (``off``,
``positive_beat_30d``) BOTH required the insider-cluster floor
(``CATALYST_MIN_DISTINCT_INSIDERS=3``) — stripping the LLM's PEAD-only
hypothesis to two cluster-AND-beat coincidences in the holdout. The
LLM's hypothesis was pure PEAD (post-earnings drift on confirmed
beats, NO insider clustering requirement). The new ``beat_30d_only``
arm + ``hold_days`` Lab knob make that hypothesis expressible in the
existing engine surface.

Coverage:
* B1  the ``beat_30d_only`` branch is reachable + emits trades
     when BEAT rows exist (n_trades > 0);
* B2  the ``beat_30d_only`` branch is event-driven, not
     cluster-driven (a fixture with NO insider rows + BEAT rows yields
     trades; the legacy paths yield zero);
* H1  ``hold_days`` is threaded through ``_simulate_trade`` — a
     trade entered when shorter hold_days would time-stop before a
     larger TP fires shows a different exit horizon under a longer
     hold_days knob;
* H2  the recorded ``parameters['hold_days']`` reflects the active
     override (the per-call reset discipline);
* L1  LAB_TARGET declares the three arms + the int range;
* C-RESET no cross-trial leakage: a variant run leaves
     ``_HOLD_DAYS_OVERRIDE`` and ``_EVENT_CONFIRMATION_MODE_OVERRIDE``
     at None.

Fully hermetic: a synthetic context is built in-body; NO DB, NO
network.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd


def _synthetic_context_with_beats(
    *,
    with_insider_rows: bool = False,
    rising_price: bool = True,
):
    """A ``CatalystWindowContext`` sized so the PEAD branch matters.

    ``earnings_events`` carries multiple BEAT rows on distinct sessions
    within [start, end] for AAPL/MSFT, so the beat_30d_only branch has
    something to iterate.

    ``with_insider_rows=False`` deliberately omits the insider-cluster
    rows so the legacy off / positive_beat_30d paths yield ZERO trades
    — proving the new branch is event-driven, not cluster-driven.

    ``rising_price`` controls whether the simulator hits the +12% TP
    (rising) or the time-stop (flat); the rising case is needed to
    exercise the hold_days delta.
    """
    from catalyst.backtest import CatalystWindowContext

    end = date(2024, 12, 31)
    start = date(2024, 3, 1)

    # Insider rows: empty by default — the beat_30d_only branch MUST NOT
    # require them.
    if with_insider_rows:
        days_back = [50, 40, 30, 20, 10, 0]
        insider_rows = pd.DataFrame([
            {"ticker": "AAPL",
             "filing_date": end - timedelta(days=db),
             "insider_name": f"insider_{db}",
             "transaction_type": "BUY", "value": 200_000.0}
            for db in days_back
        ])
    else:
        insider_rows = pd.DataFrame(
            columns=["ticker", "filing_date", "insider_name",
                     "transaction_type", "value"]
        )

    bus = pd.bdate_range(start - timedelta(days=120), end)
    if rising_price:
        closes = [50.0 + 0.5 * i for i in range(len(bus))]
    else:
        closes = [50.0] * len(bus)
    prices = pd.DataFrame(
        {"close": closes, "volume": [5_000_000] * len(bus)},
        index=bus,
    )

    # Multiple BEAT events across 2024 for AAPL and MSFT — quarterly
    # cadence approximating the real-world earnings calendar.
    earnings_rows = pd.DataFrame([
        # AAPL
        {"ticker": "AAPL", "event_date": date(2024, 4, 15),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.04},
        {"ticker": "AAPL", "event_date": date(2024, 7, 18),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.03},
        {"ticker": "AAPL", "event_date": date(2024, 10, 20),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.05},
        # MSFT
        {"ticker": "MSFT", "event_date": date(2024, 4, 22),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.03},
        {"ticker": "MSFT", "event_date": date(2024, 7, 24),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.06},
    ])

    return CatalystWindowContext(
        universe=("AAPL", "MSFT"), insider_rows=insider_rows,
        prices_by_ticker={
            "AAPL": prices,
            "MSFT": prices.copy(),
        },
        round_trip_costs={
            "AAPL": Decimal("0.001"),
            "MSFT": Decimal("0.001"),
        },
        start=start, end=end,
        earnings_events=earnings_rows,
    )


def test_b1_beat_30d_only_branch_is_reachable_and_emits_trades():
    """B1 — the beat_30d_only branch is reachable + emits trades when
    BEAT rows exist."""
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context_with_beats(
        with_insider_rows=True, rising_price=True)
    result = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "beat_30d_only"})
    assert result.parameters["event_confirmation_mode"] == "beat_30d_only"
    # The fixture seeds 5 BEATs across AAPL + MSFT. Each should produce
    # a trade subject to entry-day availability + filters.
    assert result.trades >= 3, (
        f"beat_30d_only produced only {result.trades} trades from a "
        f"5-BEAT fixture — the branch is wired but stripping too much"
    )


def test_b2_beat_30d_only_is_event_driven_not_cluster_driven():
    """B2 — beat_30d_only emits trades even when there are NO insider
    rows. The legacy off / positive_beat_30d arms yield zero in the
    same fixture (no cluster to fire on)."""
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context_with_beats(
        with_insider_rows=False, rising_price=True)
    legacy = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "off"})
    positive = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "positive_beat_30d"})
    pead = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "beat_30d_only"})

    # Without insider rows, the cluster-required arms have nothing to fire on.
    assert legacy.trades == 0, (
        "the legacy 'off' arm must yield 0 trades when no insider rows "
        "exist (cluster floor cannot clear) — surface enrichment "
        "regression detected"
    )
    assert positive.trades == 0, (
        "the 'positive_beat_30d' arm must yield 0 trades when no "
        "insider rows exist (still requires the cluster) — surface "
        "enrichment regression detected"
    )
    # The new arm is event-driven, not cluster-driven.
    assert pead.trades > 0, (
        "beat_30d_only must fire on BEAT events independent of insider "
        "clusters — that's the entire point of the surface enrichment"
    )


def _context_for_hold_days_isolation():
    """A fixture that lets us prove hold_days threads to the simulator
    without TP/SL firing. We need: (a) universe + SMA + liquidity
    filters all pass (close > SMA), and (b) the post-entry close drift
    is tiny so neither the +12% TP nor the -7% SL bind before the
    time-stop. A linearly rising price clearing SMA over the lookback,
    then a near-flat continuation after the first BEAT, achieves both.
    """
    from catalyst.backtest import CatalystWindowContext

    end = date(2024, 12, 31)
    start = date(2024, 3, 1)
    pre_event_anchor = pd.Timestamp(date(2024, 4, 15))  # the first BEAT

    bus = pd.bdate_range(start - timedelta(days=120), end)
    closes = []
    for ts in bus:
        if ts <= pre_event_anchor:
            # Gentle ramp from 50.0 to ~55.0 over the lookback — pushes
            # close above the 50-SMA so the entry filters pass.
            closes.append(50.0 + 0.02 * (ts - bus[0]).days)
        else:
            # Tiny drift after entry — 0.05% per session — keeps the
            # post-entry close well inside the (-7%, +12%) flat-bracket
            # window for any hold_days in [5, 30].
            base = 50.0 + 0.02 * (pre_event_anchor - bus[0]).days
            closes.append(base * (1.0005 ** (ts - pre_event_anchor).days))
    prices = pd.DataFrame(
        {"close": closes, "volume": [5_000_000] * len(bus)},
        index=bus,
    )
    earnings_rows = pd.DataFrame([
        {"ticker": "AAPL", "event_date": date(2024, 4, 15),
         "event_type": "EARNINGS_BEAT", "magnitude_pct": 0.04},
    ])
    return CatalystWindowContext(
        universe=("AAPL",),
        insider_rows=pd.DataFrame(
            columns=["ticker", "filing_date", "insider_name",
                     "transaction_type", "value"]),
        prices_by_ticker={"AAPL": prices},
        round_trip_costs={"AAPL": Decimal("0.001")},
        start=start, end=end,
        earnings_events=earnings_rows,
    )


def test_h1_hold_days_is_threaded_to_simulator():
    """H1 — ``hold_days`` is read by ``_simulate_trade`` via the
    public override seam. A near-flat post-entry-price fixture lets
    the time-stop bind (neither TP +12% nor SL -7% fire) so the trade
    exit horizon is exactly ``hold_days`` sessions."""
    from catalyst.backtest import run_catalyst_with_context

    ctx = _context_for_hold_days_isolation()

    short_hold = run_catalyst_with_context(
        ctx, overrides={
            "event_confirmation_mode": "beat_30d_only",
            "hold_days": 5,
        })
    long_hold = run_catalyst_with_context(
        ctx, overrides={
            "event_confirmation_mode": "beat_30d_only",
            "hold_days": 20,
        })

    # Both runs emit trades — the universe + filters pass, only
    # hold_days differs.
    assert short_hold.trades > 0, "hold_days=5 must still emit trades"
    assert long_hold.trades > 0, "hold_days=20 must still emit trades"
    # Trade log records the active hold horizon as a side-effect: the
    # exit_date of each trade must differ between the two runs when the
    # underlying price drift is tiny. Compare the first trade's exit
    # horizon directly.
    short_first = short_hold.trade_log[0]
    long_first = long_hold.trade_log[0]
    assert short_first.entry_date == long_first.entry_date, (
        "different runs entered the same fixture on different sessions "
        "— bug in hold_days isolation"
    )
    short_horizon = (short_first.exit_date - short_first.entry_date).days
    long_horizon = (long_first.exit_date - long_first.entry_date).days
    assert long_horizon > short_horizon, (
        f"hold_days knob ineffective: short={short_horizon}d "
        f"long={long_horizon}d on a near-flat fixture (TP/SL "
        f"shouldn't fire, so the time-stop should bind)"
    )


def test_h2_recorded_parameter_reflects_active_override():
    """H2 — the recorded ``parameters['hold_days']`` carries the active
    override, not the legacy default. lab_candidate_readiness §3 C3."""
    from catalyst.backtest import run_catalyst_with_context

    ctx = _synthetic_context_with_beats(with_insider_rows=True)
    legacy = run_catalyst_with_context(ctx, overrides={})
    variant = run_catalyst_with_context(ctx, overrides={"hold_days": 7})

    # Legacy: HOLDING_PERIOD_DAYS (30) — the pre-enrichment default.
    assert legacy.parameters["hold_days"] == 30
    assert variant.parameters["hold_days"] == 7
    # The two parameter sets differ — the branch is wired, not dead.
    assert legacy.parameters != variant.parameters


def test_l1_lab_target_declares_three_arms_and_hold_days_range():
    """L1 — LAB_TARGET surface mirrors the enrichment."""
    from catalyst.backtest import LAB_TARGET

    assert LAB_TARGET.param_ranges["event_confirmation_mode"] == (
        0, 0, "choice:off,positive_beat_30d,beat_30d_only",
    )
    assert LAB_TARGET.param_ranges["hold_days"] == (5, 30, "int")


def test_c_reset_hold_days_and_event_mode_reset_after_call():
    """C-RESET — the per-call module-global reset discipline applies to
    BOTH new overrides. A variant run leaves them at None."""
    from catalyst import backtest as bt

    ctx = _synthetic_context_with_beats(with_insider_rows=True)
    bt.run_catalyst_with_context(
        ctx, overrides={
            "event_confirmation_mode": "beat_30d_only",
            "hold_days": 12,
        })
    assert bt._HOLD_DAYS_OVERRIDE is None, (
        "_HOLD_DAYS_OVERRIDE leaked across calls — per-call reset broken"
    )
    assert bt._EVENT_CONFIRMATION_MODE_OVERRIDE is None, (
        "_EVENT_CONFIRMATION_MODE_OVERRIDE leaked across calls — "
        "per-call reset broken"
    )


def test_b3_beat_30d_only_does_not_blow_up_with_empty_events():
    """A beat_30d_only run with NO earnings_events rows is degenerate
    but must not blow up — it just emits zero trades.

    Engine-readiness §10 / fail-gracefully: a missing-data edge case
    surfaces as 0 trades, not an exception.
    """
    from catalyst.backtest import CatalystWindowContext, run_catalyst_with_context

    end = date(2024, 6, 30)
    start = date(2024, 3, 1)
    bus = pd.bdate_range(start - timedelta(days=120), end)
    prices = pd.DataFrame(
        {"close": [50.0 + 0.1 * i for i in range(len(bus))],
         "volume": [5_000_000] * len(bus)},
        index=bus,
    )
    ctx = CatalystWindowContext(
        universe=("AAPL",),
        insider_rows=pd.DataFrame(
            columns=["ticker", "filing_date", "insider_name",
                     "transaction_type", "value"]),
        prices_by_ticker={"AAPL": prices},
        round_trip_costs={"AAPL": Decimal("0.001")},
        start=start, end=end,
        # explicit empty events
        earnings_events=pd.DataFrame(
            columns=["ticker", "event_date", "event_type",
                     "magnitude_pct"]
        ),
    )
    result = run_catalyst_with_context(
        ctx, overrides={"event_confirmation_mode": "beat_30d_only"})
    assert result.trades == 0


def test_live_module_constants_unchanged_after_pead_variant_run():
    """LIVE — after a beat_30d_only variant run, the catalyst.models
    module constants (the live scheduler's bind point) are byte-
    identical. The override is a backtest-only global in
    catalyst.backtest only.
    """
    import catalyst.models as _models
    from catalyst.backtest import run_catalyst_with_context

    before_window = _models.CATALYST_CLUSTER_WINDOW_DAYS
    before_floor = _models.CATALYST_MIN_DISTINCT_INSIDERS
    ctx = _synthetic_context_with_beats(with_insider_rows=True)
    run_catalyst_with_context(
        ctx, overrides={
            "event_confirmation_mode": "beat_30d_only",
            "hold_days": 7,
        })
    after_window = _models.CATALYST_CLUSTER_WINDOW_DAYS
    after_floor = _models.CATALYST_MIN_DISTINCT_INSIDERS
    assert before_window == after_window, (
        "CATALYST_CLUSTER_WINDOW_DAYS moved after a Lab variant run — "
        "the backtest seam leaked into the live path"
    )
    assert before_floor == after_floor, (
        "CATALYST_MIN_DISTINCT_INSIDERS moved after a Lab variant run "
        "— the backtest seam leaked into the live path"
    )
