"""Regression tests for ``momentum.scheduler._filter_to_engine_holdings``.

Pins the cross-engine isolation contract: momentum's rebalance only
diffs against positions that originated from momentum-prefixed orders.
Without this filter, the diff saw the whole Alpaca account as its book
and emitted sell orders for any non-momentum holding whose ticker
wasn't in today's target list — exactly the YUMC tier1 incident on
2026-05-14.
"""

from __future__ import annotations

from dataclasses import dataclass

from momentum.scheduler import ENGINE_ORDER_PREFIX, _filter_to_engine_holdings


@dataclass
class _FakePos:
    symbol: str
    qty: int


@dataclass
class _FakeOrder:
    symbol: str
    client_order_id: str | None


def test_filter_keeps_only_momentum_prefixed_symbols():
    positions = [_FakePos("AAPL", 10), _FakePos("YUMC", 16)]
    orders = [
        _FakeOrder("AAPL", "mo_AAPL_1700000000"),
        _FakeOrder("YUMC", "YUMC_1778582356_tier1"),  # sigma — not momentum
    ]
    out = _filter_to_engine_holdings(positions, orders, ENGINE_ORDER_PREFIX)
    assert out == {"AAPL": 10}, (
        f"momentum must NOT see YUMC (a sigma tier1 fill) as one of its "
        f"holdings. got: {out}"
    )


def test_filter_drops_positions_with_zero_qty():
    positions = [_FakePos("AAPL", 0), _FakePos("MSFT", 5)]
    orders = [
        _FakeOrder("AAPL", "mo_AAPL_1"),
        _FakeOrder("MSFT", "mo_MSFT_1"),
    ]
    out = _filter_to_engine_holdings(positions, orders, ENGINE_ORDER_PREFIX)
    assert out == {"MSFT": 5}


def test_filter_returns_empty_when_no_momentum_orders():
    positions = [_FakePos("AAPL", 10)]
    orders = [_FakeOrder("AAPL", "manual_AAPL_xyz")]
    out = _filter_to_engine_holdings(positions, orders, ENGINE_ORDER_PREFIX)
    assert out == {}


def test_filter_handles_missing_client_order_id():
    # Some broker order responses may omit client_order_id (None / empty).
    # We must not crash, and such orders must not match any prefix.
    positions = [_FakePos("AAPL", 10), _FakePos("MSFT", 5)]
    orders = [
        _FakeOrder("AAPL", None),
        _FakeOrder("MSFT", ""),
        _FakeOrder("MSFT", "mo_MSFT_1"),  # makes MSFT ours
    ]
    out = _filter_to_engine_holdings(positions, orders, ENGINE_ORDER_PREFIX)
    assert out == {"MSFT": 5}


def test_yumc_regression_scenario():
    """The 2026-05-14 incident, replayed against the new filter.

    Before the fix: positions = [25 momentum picks + YUMC tier1 (16
    shares)]. momentum's diff saw YUMC, target=0, emitted a SELL.
    After the fix: YUMC is filtered out before the diff runs.
    """
    positions = [
        _FakePos("NSI", 25),    # momentum buy
        _FakePos("NXTG", 6),    # momentum buy
        _FakePos("PATN", 28),   # momentum buy
        _FakePos("YUMC", 16),   # sigma tier1 — must NOT count as momentum's
    ]
    orders = [
        _FakeOrder("NSI", "mo_NSI_1778650929"),
        _FakeOrder("NXTG", "mo_NXTG_1778650929"),
        _FakeOrder("PATN", "mo_PATN_1778650929"),
        _FakeOrder("YUMC", "YUMC_1778582356_tier1"),
    ]
    out = _filter_to_engine_holdings(positions, orders, ENGINE_ORDER_PREFIX)
    assert "YUMC" not in out
    assert sorted(out.keys()) == ["NSI", "NXTG", "PATN"]
