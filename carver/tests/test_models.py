"""carver/models.py — Pydantic v2 model + constant shape tests."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError


def test_constants_present_and_well_typed() -> None:
    from carver.models import (
        ANNUALIZED_VOL_TARGET,
        DAILY_LOSS_FREEZE_PCT,
        DRAWDOWN_BREAKER_LOOKBACK_DAYS,
        FORECAST_CAP_ABS,
        FORECAST_TARGET_ABS,
        IDM_CAP,
        IDM_FLOOR,
        MAX_CONCURRENT_POSITIONS,
        MAX_TRADES_PER_INSTRUMENT_PER_YEAR,
        PRE_GRAD_POSITION_CAP_USD,
    )
    assert FORECAST_TARGET_ABS == 10
    assert FORECAST_CAP_ABS == 20
    assert ANNUALIZED_VOL_TARGET == Decimal("0.25")
    assert MAX_TRADES_PER_INSTRUMENT_PER_YEAR == 12
    assert PRE_GRAD_POSITION_CAP_USD == Decimal("1500")
    assert MAX_CONCURRENT_POSITIONS == 20  # portfolio-scale, not per-trade
    assert DAILY_LOSS_FREEZE_PCT == Decimal("0.05")
    assert DRAWDOWN_BREAKER_LOOKBACK_DAYS == 365
    assert IDM_FLOOR == Decimal("1.0")
    assert IDM_CAP == Decimal("2.5")


def test_carver_forecast_caps_at_plus_minus_twenty() -> None:
    from carver.models import CarverForecast

    f = CarverForecast(rule="trend", scaled=25.0, raw=99.0)
    assert f.capped == 20.0  # FORECAST_CAP_ABS
    f2 = CarverForecast(rule="trend", scaled=-30.0, raw=-50.0)
    assert f2.capped == -20.0
    f3 = CarverForecast(rule="trend", scaled=12.5, raw=2.3)
    assert f3.capped == 12.5  # within cap, untouched


def test_carver_assessment_combined_forecast_is_capped_after_fdm() -> None:
    from carver.models import CarverAssessment, CarverForecast

    forecasts = [
        CarverForecast(rule="trend", scaled=18.0, raw=1.0, capped=18.0),
        CarverForecast(rule="value", scaled=15.0, raw=1.0, capped=15.0),
        CarverForecast(rule="meanrev", scaled=17.0, raw=1.0, capped=17.0),
    ]
    a = CarverAssessment(
        ticker="AAPL",
        forecasts=forecasts,
        idm=Decimal("1.6"),
        combined_forecast=99.0,  # provided pre-cap by the plug
    )
    # The model exposes a `combined_capped` derived field for sizing.
    assert a.combined_capped == 20.0  # capped at +20
    assert a.ticker == "AAPL"
    assert a.idm == Decimal("1.6")


def test_models_are_frozen_and_extra_forbid() -> None:
    from carver.models import CarverForecast

    f = CarverForecast(rule="trend", scaled=1.0, raw=1.0)
    with pytest.raises(ValidationError):
        f.scaled = 2.0  # frozen
    with pytest.raises(ValidationError):
        CarverForecast(rule="trend", scaled=1.0, raw=1.0, bogus_field=True)


def test_carver_aar_uses_classify_exit_reason_and_resolves_time_stop() -> None:
    from datetime import UTC, datetime

    from carver.plugs.aar_logging import CarverAARLogging
    from tpcore.aar.models import ExitReason

    plug = CarverAARLogging()
    # Portfolio engines pass take_profit=None, stop_loss=None -> TIME_STOP.
    aar = plug.build_aar(
        trade_id="cv_AAPL_20260520_001",
        ticker="AAPL",
        entry_price=Decimal("150"),
        exit_price=Decimal("152"),
        qty=10,
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 2, 1, tzinfo=UTC),
        take_profit=None,
        stop_loss=None,
    )
    assert aar.exit_reason == ExitReason.TIME_STOP
    assert aar.engine == "carver"


def test_rebalance_decision_count_buckets() -> None:
    from carver.models import (
        CarverTarget,
        RebalanceAction,
        RebalanceDecision,
        RebalanceOrder,
    )

    targets = [
        CarverTarget(
            ticker="AAPL", target_shares=10,
            target_notional_usd=Decimal("1500"),
            combined_forecast=15.0,
        ),
    ]
    orders = [
        RebalanceOrder(
            ticker="AAPL", action=RebalanceAction.OPEN, side="buy",
            qty=10, notional_usd=Decimal("1500"), order_payload={},
        ),
    ]
    d = RebalanceDecision(
        targets=targets, orders=orders,
        n_open=1, n_close=0, n_increase=0, n_decrease=0, n_hold=0,
        total_buy_notional_usd=Decimal("1500"),
    )
    assert d.n_open == 1
    assert d.total_buy_notional_usd == Decimal("1500")
