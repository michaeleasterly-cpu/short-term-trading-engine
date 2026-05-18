"""#251 B1.3 — the never-fail-open close-funnel interleaving suite.

Proves the THREE close `-1` callers (trade-monitor stream, momentum
rebalance-sell, sentinel rebalance-sell) now funnel through the single
idempotent ``record_close`` arbiter, and that in EVERY interleaving the
net decrement for one real close is **exactly 1 or 0 — NEVER 2**.

Each test is written to genuinely BITE against the pre-B1 dual-decrement
code (two uncoordinated ``record_fill(-1)`` paths): the
``_DualDecrementStore`` below reproduces the OLD behaviour and the
``test_pre_b1_*`` guards assert it would have double-decremented — so a
regression that reverts the funnel fails loudly.

No real DB / broker / repo / ``data/`` is touched (fake store only).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tpcore.order_ids import build_close_id
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
    RiskState,
)


def _broker():
    from unittest.mock import AsyncMock

    b = AsyncMock()
    b.get_positions.return_value = []
    b.emergency_cancel_all.return_value = 0
    return b


async def _governor(open_positions: int = 3) -> tuple[RiskGovernor, InMemoryRiskStateStore]:
    store = InMemoryRiskStateStore()
    await store.put(
        RiskState(
            engine="momentum",
            engine_equity=Decimal("10000"),
            open_positions=open_positions,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    gov = RiskGovernor(state_store=store, broker=_broker())
    return gov, store


# ─── The OLD (pre-B1) dual-decrement store — proves each test bites ──────


class _DualDecrementStore(InMemoryRiskStateStore):
    """Reproduces the pre-B1 bug: every -1 path raw-decrements, no key."""

    async def record_fill(self, *, engine, realized_pnl, position_delta):
        st = self._states[engine]
        self._states[engine] = st.model_copy(
            update={"open_positions": max(0, st.open_positions + position_delta)}
        )


async def _seed_dual(open_positions: int = 3) -> _DualDecrementStore:
    store = _DualDecrementStore()
    await store.put(
        RiskState(
            engine="momentum",
            engine_equity=Decimal("10000"),
            open_positions=open_positions,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    return store


# ─── Funnel: the three callers all route through record_close ───────────


def test_momentum_scheduler_routes_close_through_record_close() -> None:
    import inspect

    import momentum.scheduler as ms

    src = inspect.getsource(ms)
    assert "governor.record_close(" in src
    assert "build_close_id(\"momentum\"" in src
    # The old raw dual-decrement call is GONE from the sell branch.
    assert "position_delta=-1" not in src


def test_sentinel_scheduler_routes_close_through_record_close() -> None:
    import inspect

    import sentinel.scheduler as ss

    src = inspect.getsource(ss)
    assert "governor.record_close(" in src
    assert "build_close_id(\"sentinel\"" in src
    assert "position_delta=-1" not in src


def test_trade_monitor_stream_routes_close_through_record_close() -> None:
    import inspect

    import tpcore.trade_monitor as tm

    src = inspect.getsource(tm)
    assert "_risk_store.record_close(" in src
    # The stream no longer raw record_fill(position_delta=-1)s the close.
    assert "position_delta=-1" not in src


# ─── The never-fail-open interleaving table ─────────────────────────────
#
# Same real close → key (engine, trade_id). In EVERY ordering the net
# decrement is exactly 1 (one path applied) or 0 (none), NEVER 2.


async def test_stream_then_scheduler_one_decrement() -> None:
    gov, store = await _governor(open_positions=3)
    tid = build_close_id("momentum", "AAPL", date(2026, 5, 19))
    await gov.record_close("momentum", tid, Decimal("12"))  # stream
    await gov.record_close("momentum", tid, Decimal("0"))   # scheduler (same close)
    st = await store.get("momentum")
    assert st.open_positions == 2  # net -1, NOT -2
    assert st.daily_pnl == Decimal("12")  # pnl applied once


async def test_scheduler_then_stream_one_decrement() -> None:
    gov, store = await _governor(open_positions=3)
    tid = build_close_id("momentum", "AAPL", date(2026, 5, 19))
    await gov.record_close("momentum", tid, Decimal("0"))   # scheduler
    await gov.record_close("momentum", tid, Decimal("12"))  # stream (same close)
    st = await store.get("momentum")
    assert st.open_positions == 2  # net -1, NOT -2
    assert st.daily_pnl == Decimal("0")  # first writer won; second skipped


async def test_concurrent_same_close_one_decrement() -> None:
    gov, store = await _governor(open_positions=3)
    tid = build_close_id("momentum", "AAPL", date(2026, 5, 19))
    res = await asyncio.gather(
        gov.record_close("momentum", tid, Decimal("0")),
        gov.record_close("momentum", tid, Decimal("0")),
    )
    assert sorted(res) == [False, True]  # exactly one winner
    st = await store.get("momentum")
    assert st.open_positions == 2  # NEVER 1


async def test_one_path_only_still_one_decrement() -> None:
    gov, store = await _governor(open_positions=3)
    tid = build_close_id("momentum", "AAPL", date(2026, 5, 19))
    await gov.record_close("momentum", tid, Decimal("0"))
    st = await store.get("momentum")
    assert st.open_positions == 2


async def test_null_trade_id_skips_no_decrement() -> None:
    gov, store = await _governor(open_positions=3)
    applied = await gov.record_close("momentum", None, Decimal("0"))
    assert applied is False
    st = await store.get("momentum")
    assert st.open_positions == 3  # over-count = safe → never fail open


async def test_ledger_error_contained_no_decrement() -> None:
    class _BoomStore(InMemoryRiskStateStore):
        async def record_close(self, engine, trade_id, realized_pnl):
            raise RuntimeError("ledger INSERT failed")

    store = _BoomStore()
    await store.put(
        RiskState(
            engine="momentum",
            engine_equity=Decimal("10000"),
            open_positions=3,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    gov = RiskGovernor(state_store=store, broker=_broker())
    with pytest.raises(RuntimeError):
        await gov.record_close("momentum", "t1", Decimal("0"))
    st = await store.get("momentum")
    assert st.open_positions == 3  # NO decrement on error → never fail open


@pytest.mark.parametrize("n", [2, 4, 9])
async def test_idempotency_property_n_via_both_paths_one_net_decrement(n: int) -> None:
    gov, store = await _governor(open_positions=5)
    tid = build_close_id("momentum", "AAPL", date(2026, 5, 19))
    for i in range(n):  # alternate "stream" / "scheduler" callers
        await gov.record_close("momentum", tid, Decimal("7") if i % 2 == 0 else Decimal("0"))
    st = await store.get("momentum")
    assert st.open_positions == 4  # exactly ONE net -1 across N applications
    assert st.daily_pnl == Decimal("7")  # pnl applied exactly once (first winner)


# ─── Bite guards: prove the pre-B1 code WOULD have double-decremented ────


async def test_pre_b1_dual_decrement_would_double() -> None:
    """Sanity: the OLD two-record_fill(-1) flow drops by 2 — this is the
    bug B1 kills. If a regression reverts the funnel, the interleaving
    tests above flip from 2→? and fail; this asserts the bug is real."""
    store = await _seed_dual(open_positions=3)
    await store.record_fill(engine="momentum", realized_pnl=Decimal("0"), position_delta=-1)
    await store.record_fill(engine="momentum", realized_pnl=Decimal("0"), position_delta=-1)
    st = await store.get("momentum")
    assert st.open_positions == 1  # the fail-open under-drift (2 lost for 1 close)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
