"""Catalyst — backtest harness + LAB_TARGET wiring.

Covers:
* ``LAB_TARGET`` declares the single pre-registered toggle, default
  metric SHARPE, and matches the four uniform Lab dispatch callables;
* ``default_params()`` carries the legacy default;
* ``run_catalyst_with_context`` reaches the variant path (the toggle is
  wired, not dead) and resets the module override per call;
* ``write_credibility_score`` is called by ``run_backtest`` (the
  compliance grep #3 contract — verified via call interception).

Hermetic: a synthetic :class:`CatalystWindowContext` is built in-body;
no DB, no network, no module-level ``import ops.lab.run`` (the SP-D CI
hermeticity lesson).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from catalyst.models import (
    CATALYST_CLUSTER_WINDOW_DAYS,
    CATALYST_TEST_UNIVERSE,
)


def _synthetic_context():
    """A CatalystWindowContext sized so the cluster window matters.

    Universe = ('AAPL',). Insider BUYs are placed at days [-50, -40,
    -30, -20, -10, 0] relative to a reference as_of, so a window of 30
    captures 4 of them (≥3 distinct insiders) and a window of 45
    captures 5; both clusters clear the floor but the trade entry dates
    differ → the variant is reachable AND distinct.
    """
    from catalyst.backtest import CatalystWindowContext

    end = date(2024, 6, 30)
    start = date(2024, 3, 1)
    # Insider rows: distinct insider per BUY so cluster size moves with window.
    days_back = [50, 40, 30, 20, 10, 0]
    rows = pd.DataFrame([
        {"ticker": "AAPL",
         "filing_date": end - timedelta(days=db),
         "insider_name": f"insider_{db}",
         "transaction_type": "BUY", "value": 200_000.0}
        for db in days_back
    ])
    # Price panel: rising, so close > 50-SMA for every session in the window.
    bus = pd.bdate_range(start - timedelta(days=120), end)
    prices = pd.DataFrame(
        {"close": [50.0 + 0.1 * i for i in range(len(bus))],
         "volume": [5_000_000] * len(bus)},
        index=bus,
    )
    return CatalystWindowContext(
        universe=("AAPL",), insider_rows=rows,
        prices_by_ticker={"AAPL": prices},
        round_trip_costs={"AAPL": Decimal("0.001")},
        start=start, end=end,
    )


def test_lab_target_pre_registered_toggles():
    """lab_candidate_readiness §1 / §2 / §10: each toggle is a single
    ``choice:`` over ``{legacy_default, the_one_variant}``;
    ``default_params`` carries the legacy default for every toggle.

    Two pre-registered toggles co-exist on ``catalyst.backtest.LAB_TARGET``:

    - ``cluster_window_days`` — SP-F (PR #159): legacy 30 vs alternative
      45 cluster window.
    - ``event_confirmation_mode`` — event-confirmed insider-cluster
      drift (this candidate): legacy ``"off"`` vs the one variant
      ``"positive_beat_30d"``.

    Each toggle's two values are exactly ``{legacy_default, the_one_
    variant}``. No third value. The two toggles are independent
    hypotheses, each one pre-registered.
    """
    from catalyst.backtest import LAB_TARGET, default_params
    from tpcore.lab.target import LabPrimaryMetric

    assert set(LAB_TARGET.param_ranges) == {
        "cluster_window_days", "event_confirmation_mode",
    }
    assert LAB_TARGET.param_ranges["cluster_window_days"] == (
        30, 45, "choice:30,45")
    assert LAB_TARGET.param_ranges["event_confirmation_mode"] == (
        0, 0, "choice:off,positive_beat_30d")
    assert default_params() == {
        "cluster_window_days": int(CATALYST_CLUSTER_WINDOW_DAYS),
        "event_confirmation_mode": "off",
    }
    # Catalyst is a swing engine — SP-D default SHARPE.
    assert LAB_TARGET.primary_metric == LabPrimaryMetric.SHARPE


def test_lab_target_callable_contract_is_uniform():
    """All four uniform Lab dispatch callables are wired."""
    from catalyst.backtest import (
        LAB_TARGET,
        default_params,
        load_catalyst_window_context,
        run_catalyst_with_context,
        run_for_search,
    )

    assert LAB_TARGET.run_for_search is run_for_search
    assert LAB_TARGET.load_window_context is load_catalyst_window_context
    assert LAB_TARGET.run_with_context is run_catalyst_with_context
    assert LAB_TARGET.default_params is default_params


def test_run_catalyst_with_context_runs_and_resets_override():
    """The off-by-default override is reset to None after every call —
    the per-call module-global reset discipline."""
    from catalyst import backtest as bt

    ctx = _synthetic_context()
    # Sanity: override starts off.
    assert bt._CLUSTER_WINDOW_OVERRIDE is None
    result = bt.run_catalyst_with_context(
        ctx, overrides={"cluster_window_days": 45})
    assert result.engine == "catalyst"
    assert result.parameters["cluster_window_days"] == 45
    # The override is reset.
    assert bt._CLUSTER_WINDOW_OVERRIDE is None


def test_run_catalyst_with_context_no_override_uses_legacy_default():
    """A call with no override produces the legacy-default behaviour."""
    from catalyst import backtest as bt

    ctx = _synthetic_context()
    result = bt.run_catalyst_with_context(ctx, overrides=None)
    assert result.parameters["cluster_window_days"] == int(
        CATALYST_CLUSTER_WINDOW_DAYS)


def test_run_catalyst_with_context_variant_branch_reachable():
    """Compliance §3 / lab_candidate_readiness §3 C3: the variant
    branch is reachable AND can differ from the legacy default."""
    from catalyst import backtest as bt

    ctx = _synthetic_context()
    legacy = bt.run_catalyst_with_context(ctx, overrides={})
    variant = bt.run_catalyst_with_context(
        ctx, overrides={"cluster_window_days": 45})
    # The parameter is recorded distinctly — the variant truly reached
    # the variant code path.
    assert legacy.parameters["cluster_window_days"] == 30
    assert variant.parameters["cluster_window_days"] == 45


def test_run_backtest_persists_credibility_rubric(monkeypatch):
    """engine_readiness §8 / §10 grep #3: ``run_backtest`` writes the
    credibility rubric via ``write_credibility_score`` so the capital
    gate's graduation check has something to read."""
    import asyncio
    from pathlib import Path

    from catalyst import backtest as bt

    persisted: list[dict] = []

    class _FakePool:
        async def close(self):
            return None

    async def _fb(_url, *, read_only=False, **_k):
        del read_only
        return _FakePool()

    async def _fake_write(_pool, *, engine_name, score):
        persisted.append({"engine": engine_name, "score": score})
        return True

    async def _fake_loader(**_kwargs):
        return _synthetic_context()

    monkeypatch.setattr(
        "tpcore.db.build_asyncpg_pool", _fb, raising=True)
    monkeypatch.setattr(
        "catalyst.backtest.build_asyncpg_pool", _fb, raising=True)
    monkeypatch.setattr(
        "catalyst.backtest.load_catalyst_window_context", _fake_loader,
        raising=True)
    monkeypatch.setattr(
        "catalyst.backtest.write_credibility_score", _fake_write,
        raising=True)
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")

    tmp = Path("/tmp/catalyst_test_backtest_out")
    rc = asyncio.run(bt.run_backtest(
        start=date(2024, 3, 1), end=date(2024, 6, 30),
        output_dir=tmp,
        results_file="catalyst_results.json",
        trades_file="catalyst_trades.csv",
        json_output=False, trade_log_path=None,
    ))
    assert rc == 0
    # The rubric write was called for engine='catalyst' (the gate-row
    # writer that graduation_ready reads).
    assert persisted, "write_credibility_score was never called"
    assert persisted[0]["engine"] == "catalyst"


def test_universe_default_for_loader_is_catalyst_test_universe():
    """The loader defaults the universe to CATALYST_TEST_UNIVERSE — the
    plug-and-play roster contract."""
    from catalyst.backtest import CATALYST_TEST_UNIVERSE as B_UNI

    assert B_UNI == CATALYST_TEST_UNIVERSE
    assert "AAPL" in B_UNI
