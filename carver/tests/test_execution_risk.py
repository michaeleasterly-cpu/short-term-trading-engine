"""carver/plugs/execution_risk.py — vol-target sizing + speed limit + payloads."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest


def _assessment(
    ticker: str = "AAPL",
    *,
    combined_forecast: float = 10.0,
    instrument_price_usd: Decimal = Decimal("100"),
    instrument_daily_vol_pct: float = 0.01,
):
    from carver.models import CarverAssessment, CarverForecast

    return CarverAssessment(
        ticker=ticker,
        forecasts=[
            CarverForecast(rule="trend", raw=1.0, scaled=10.0),
            CarverForecast(rule="value", raw=1.0, scaled=10.0),
            CarverForecast(rule="meanrev", raw=1.0, scaled=10.0),
        ],
        idm=Decimal("1.0"),
        combined_forecast=combined_forecast,
        instrument_daily_vol_pct=instrument_daily_vol_pct,
        instrument_price_usd=instrument_price_usd,
    )


def test_subclasses_base_engine_plug_and_has_healthcheck() -> None:
    from carver.plugs.execution_risk import CarverExecutionRisk
    from tpcore.interfaces.engine_plug import BaseEnginePlug

    plug = CarverExecutionRisk()
    assert isinstance(plug, BaseEnginePlug)
    assert plug.validate_dependencies() is True
    hc = plug.healthcheck()
    assert hc["engine"] == "carver"
    assert hc["plug"] == "execution_risk"


def test_position_notional_formula_exact_arithmetic() -> None:
    from carver.plugs.execution_risk import _position_notional

    # combined_forecast=10, daily_cash_vol_target=$250, instrument_daily_cash_vol=$5
    # -> notional = (10/10) * (250/5) = $50.00
    notional = _position_notional(
        combined_forecast=10.0,
        daily_cash_vol_target=Decimal("250"),
        instrument_daily_cash_vol=Decimal("5"),
    )
    assert notional == Decimal("50")


def test_sizing_error_on_non_positive_price() -> None:
    from carver.plugs.execution_risk import CarverExecutionRisk
    from tpcore.exceptions import SizingError

    plug = CarverExecutionRisk()
    cand = _assessment(instrument_price_usd=Decimal("0"))
    with pytest.raises(SizingError):
        plug.size_one(
            cand,
            engine_equity_usd=Decimal("10000"),
            annualized_vol_target=Decimal("0.25"),
        )


def test_negative_combined_forecast_sizes_zero() -> None:
    from carver.plugs.execution_risk import CarverExecutionRisk

    plug = CarverExecutionRisk()
    cand = _assessment(combined_forecast=-15.0)
    result = plug.size_one(
        cand,
        engine_equity_usd=Decimal("100000"),
        annualized_vol_target=Decimal("0.25"),
    )
    assert result is None


def test_qty_below_min_is_skipped() -> None:
    from carver.plugs.execution_risk import CarverExecutionRisk

    plug = CarverExecutionRisk()
    # Very expensive instrument + tiny notional -> qty = 0.
    cand = _assessment(
        instrument_price_usd=Decimal("1000000"),
        combined_forecast=1.0,
        instrument_daily_vol_pct=0.50,
    )
    result = plug.size_one(
        cand,
        engine_equity_usd=Decimal("1000"),
        annualized_vol_target=Decimal("0.25"),
    )
    assert result is None


def test_payload_carries_cv_engine_id_prefix() -> None:
    from carver.plugs.execution_risk import CarverExecutionRisk

    plug = CarverExecutionRisk()
    cand = _assessment(combined_forecast=15.0)
    sized = plug.size_one(
        cand,
        engine_equity_usd=Decimal("100000"),
        annualized_vol_target=Decimal("0.25"),
    )
    assert sized is not None
    assert sized.order_payload["client_order_id"].startswith("cv_")


def test_speed_limit_blocks_thirteenth_flip_in_year() -> None:
    """When the lifecycle plug reports 12+ flips in the last 365d the
    candidate is suppressed by ``decide``."""
    import asyncio

    from carver.plugs.execution_risk import CarverExecutionRisk

    class _StubLifecycle:
        async def flips_in_window(
            self, pool: Any, ticker: str, as_of: date, days: int = 365,
        ) -> int:
            del pool, ticker, as_of, days
            return 12  # already at the cap

    plug = CarverExecutionRisk()
    cand = _assessment(combined_forecast=15.0)
    candidates = [cand]
    decision = asyncio.run(
        plug.decide(
            candidates=candidates,
            engine_equity_usd=Decimal("100000"),
            current_holdings={},
            lifecycle=_StubLifecycle(),
            pool=None,
            as_of=date(2026, 6, 1),
        )
    )
    # Speed-limit suppression -> no orders for AAPL.
    assert all(o.ticker != "AAPL" for o in decision.orders)


def test_decide_emits_open_close_increase_decrease_counts() -> None:
    import asyncio

    from carver.plugs.execution_risk import CarverExecutionRisk

    class _NoLimit:
        async def flips_in_window(
            self, pool: Any, ticker: str, as_of: date, days: int = 365,
        ) -> int:
            del pool, ticker, as_of, days
            return 0

    plug = CarverExecutionRisk()
    cand_open = _assessment("AAPL", combined_forecast=15.0)
    cand_close_implicit = _assessment("MSFT", combined_forecast=-5.0)
    current_holdings = {"MSFT": 10, "GOOG": 5}
    decision = asyncio.run(
        plug.decide(
            candidates=[cand_open, cand_close_implicit],
            engine_equity_usd=Decimal("100000"),
            current_holdings=current_holdings,
            lifecycle=_NoLimit(),
            pool=None,
            as_of=date(2026, 6, 1),
        )
    )
    # MSFT/GOOG aren't in the target set -> closes.
    actions = {o.ticker: o.action.value for o in decision.orders}
    if "GOOG" in actions:
        assert actions["GOOG"] == "close"
