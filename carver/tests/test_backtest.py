"""carver/backtest.py — walk-forward + LAB_TARGET + write_credibility_score."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd


def _synthetic_panels(
    n_tickers: int = 3, n_days: int = 700, *, seed: int = 7,
) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    start = date(2023, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    out: dict[str, pd.DataFrame] = {}
    for i in range(n_tickers):
        drift = 0.0005 + 0.0001 * i
        sigma = 0.013 + 0.001 * i
        ret = rng.normal(loc=drift, scale=sigma, size=n_days)
        closes = 50.0 * np.cumprod(1.0 + ret)
        out[f"T{i}"] = pd.DataFrame(
            {
                "open": closes,
                "high": closes * 1.005,
                "low": closes * 0.995,
                "close": closes,
                "volume": [1_000_000] * n_days,
            },
            index=pd.DatetimeIndex(dates),
        )
    return out


def test_default_params_returns_six_keys() -> None:
    from carver.backtest import default_params

    params = default_params()
    assert set(params.keys()) == {
        "trend_fast",
        "trend_slow",
        "value_lookback_months",
        "meanrev_window",
        "annualized_vol_target",
        "idm_cap",
    }


def test_LAB_TARGET_is_valid_LabTarget_instance() -> None:
    from carver.backtest import LAB_TARGET, default_params
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    assert isinstance(LAB_TARGET, LabTarget)
    assert set(LAB_TARGET.param_ranges.keys()) == set(default_params().keys())
    # SP-D ranking metric — Sharpe is the default; spec calls for the
    # annualized variant but the enum exposes a single SHARPE value
    # (annualized is the implementation in the Lab resolver).
    assert LAB_TARGET.primary_metric == LabPrimaryMetric.SHARPE


def test_run_carver_with_context_returns_BacktestRunResult() -> None:
    from carver.backtest import CarverWindowContext, run_carver_with_context

    panels = _synthetic_panels()
    ctx = CarverWindowContext(
        panels=panels,
        tier_round_trip_costs={},
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        universe=tuple(panels.keys()),
        raw_start=date(2023, 1, 1),
    )
    result = run_carver_with_context(ctx)
    assert result.engine == "carver"
    assert result.trades >= 0


def test_run_for_search_calls_write_credibility_score(
    monkeypatch: Any,
) -> None:
    """run_for_search must call write_credibility_score exactly once with
    engine_name='carver' (compliance grep #3)."""
    import carver.backtest as bt

    panels = _synthetic_panels()

    # Stub pool + asyncpg pool builder.
    class _StubConn:
        async def fetch(self, *a: Any, **kw: Any) -> list:
            return []

        async def fetchval(self, *a: Any, **kw: Any) -> Any:
            return None

        async def fetchrow(self, *a: Any, **kw: Any) -> None:
            return None

        async def execute(self, *a: Any, **kw: Any) -> None:
            return None

    class _Acquire:
        async def __aenter__(self) -> _StubConn:
            return _StubConn()

        async def __aexit__(self, *a: Any) -> None:
            return None

    class _StubPool:
        def acquire(self) -> _Acquire:
            return _Acquire()

        async def close(self) -> None:
            return None

    async def _build_pool(_db_url: str) -> _StubPool:
        return _StubPool()

    async def _load_universe(_pool: Any) -> tuple[str, ...]:
        return tuple(panels.keys())

    async def _load_bars(_pool: Any, tickers: Any, start: Any, end: Any) -> dict:
        del start, end, tickers
        return panels

    async def _load_tier_costs(_pool: Any) -> dict:
        return {}

    write_calls: list[dict] = []

    async def _write_creds(pool: Any, *, engine_name: str, score: Any) -> bool:
        write_calls.append({"engine_name": engine_name, "score": score})
        del pool
        return True

    monkeypatch.setattr(bt, "build_asyncpg_pool", _build_pool)
    monkeypatch.setattr(bt, "_load_universe_t12", _load_universe)
    monkeypatch.setattr(bt, "_load_bars", _load_bars)
    monkeypatch.setattr(bt, "load_tier_costs", _load_tier_costs)
    monkeypatch.setattr(bt, "write_credibility_score", _write_creds)

    asyncio.run(
        bt.run_for_search(
            db_url="postgresql://stub",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )
    )
    assert len(write_calls) == 1
    assert write_calls[0]["engine_name"] == "carver"
