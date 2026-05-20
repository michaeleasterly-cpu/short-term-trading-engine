"""Demand-driven targeting wiring for ``handle_finnhub_insider_sentiment``.

Mirrors the IBorrowDesk precedent: a CONSTRAINED_DEMAND_DRIVEN handler
with a per-ticker loop must call ``demand_targets`` + ``prioritise``
so demand tickers (open_orders ∪ recent AAR ∪ recent candidates) land
at the FRONT of the loop. A truncated run (Supabase drop, OOM, kill)
then still covers what the engines care about.

This test pins the wiring: a known demand set monkeypatched into
``tpcore.feeds.targeting.demand_targets`` MUST re-order the universe
before the FinnhubAdapter loop sees it. Removing the
``demand_targets``/``prioritise`` call from the handler reds this.

httpx is NOT mocked here — the test fakes the FinnhubAdapter +
asyncpg.Pool directly and records the ticker order observed during
the per-ticker loop.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

handlers = importlib.import_module("tpcore.ingestion.handlers")


class _FakeRes:
    """Minimal stand-in for FinnhubAdapter.get_insider_sentiment's
    return — the handler iterates ``res.records`` and stops if empty."""
    records: list = []


class _FakeFinnhubAdapter:
    """Captures the order tickers are queried in — the targeting
    proof point. Returns empty records to short-circuit the upsert."""
    observed_order: list[str]

    def __init__(self, *a, **k) -> None:
        self.observed_order = []

    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None

    async def get_insider_sentiment(self, symbol, *_a, **_k):
        self.observed_order.append(symbol)
        return _FakeRes()


# Universe-fetch SQL response + skip-guard fetchval + demand-fetch SQL.
_UNIVERSE_ORDER = ["AAPL", "AMZN", "META", "MSFT", "NVDA", "TSLA"]


class _FakeConn:
    def __init__(self) -> None:
        # Universe fetch shape.
        self._universe_rows = [{"ticker": t} for t in _UNIVERSE_ORDER]

    async def fetchval(self, sql: str, *args):
        # Skip-guard MAX(recorded_at) returns "stale enough to pull".
        if "MAX(recorded_at) FROM platform.insider_sentiment" in sql:
            return datetime.now(UTC) - timedelta(days=60)
        return None

    async def fetch(self, sql: str, *args):
        if "FROM platform.liquidity_tiers" in sql:
            return self._universe_rows
        # demand_targets SQL — return a known demand set with two
        # tickers in-universe (TSLA, NVDA) and one not (XYZ).
        if "platform.open_orders" in sql or "platform.aar_events" in sql:
            return [{"ticker": "TSLA"}, {"ticker": "NVDA"}, {"ticker": "XYZ"}]
        return []


class _AcquireCM:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _FakePool:
    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn())


# ops-shadow discipline — the handler imports from scripts.ops via the
# package-shadow path. Even though this handler doesn't, marking the
# test is the safe default for any handlers.py touch.
pytestmark = pytest.mark.xdist_group("ops_shadow")


async def test_finnhub_handler_prioritises_demand_tickers_at_front(monkeypatch):
    """The handler must reorder the T1/T2 universe so demand tickers
    land at the front of the FinnhubAdapter loop, mirroring the
    IBorrowDesk wiring. Demand tickers NOT in-universe are skipped
    by ``prioritise`` (no widening); WHOLE_UNIVERSE feeds would have
    returned ``demand=None`` and stayed in alpha order."""
    # Patch the FinnhubAdapter source-module reference the handler
    # uses (in-body ``from tpcore.finnhub import FinnhubAdapter``).
    monkeypatch.setattr(
        "tpcore.finnhub.FinnhubAdapter", _FakeFinnhubAdapter, raising=True,
    )
    # Capture the adapter instance the handler constructs — it's the
    # one whose observed_order we want to inspect.
    observed: list[_FakeFinnhubAdapter] = []
    original = _FakeFinnhubAdapter

    def _make(*a, **k):
        inst = original(*a, **k)
        observed.append(inst)
        return inst
    monkeypatch.setattr("tpcore.finnhub.FinnhubAdapter", _make, raising=True)

    # Bypass the 1.1s per-ticker courtesy sleep so the test is fast.
    async def _no_sleep(*_a, **_k): return None
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    await handlers.handle_finnhub_insider_sentiment(
        _FakePool(), {"skip_guard_days": 1, "lookback_months": 1},
    )

    assert len(observed) == 1, "handler must construct one adapter"
    order = observed[0].observed_order

    # Demand tickers TSLA + NVDA must come FIRST (in demand sort order,
    # which is the alpha sort applied inside demand_targets).
    # demand_targets sorts the set: {NVDA, TSLA, XYZ} → ["NVDA","TSLA","XYZ"];
    # prioritise filters XYZ (not in universe) → head = ["NVDA","TSLA"];
    # tail keeps the universe's original order minus the head members.
    assert order[:2] == ["NVDA", "TSLA"], (
        f"demand tickers must lead the loop; got {order[:2]}. "
        f"Removing the demand_targets/prioritise call from the handler "
        f"would red this — that's the targeting-wiring pin."
    )
    # Tail covers the rest of the universe (no widening, no drops).
    assert set(order) == set(_UNIVERSE_ORDER), (
        f"every universe ticker must still be visited; got {sorted(order)}"
    )
    assert len(order) == len(_UNIVERSE_ORDER), (
        f"no duplicate visits; got {order}"
    )
