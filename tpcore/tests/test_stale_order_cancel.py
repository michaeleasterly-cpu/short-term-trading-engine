"""Characterization tests for ``tpcore.order_management.stale_order_cancel``.

Lean P5 Phase P5.4a (#1, LIVE-MONEY). These tests are written **before** the
refactor and pin the EXACT observable behavior of momentum's current
``_cancel_stale_momentum_orders``:

* the precise set of cancelled broker-order IDs,
* the exact return count,
* the exact emitted structlog event NAMES (forensics/dashboards key on them).

Expected values are constructed **independently** from the fixture — never by
calling the engine fn as an oracle. The momentum delegate is then asserted to
produce the same independently-derived results.

Fake broker only — no real broker / DB / network.
"""

from __future__ import annotations

import structlog

from tpcore.order_management.stale_order_cancel import cancel_stale_orders

# Momentum's existing prefix constant + the exact namespace it logs under.
MOMENTUM_PREFIX = "mo_"
MOMENTUM_NS = "momentum.scheduler"


class _Status:
    """Mimics an Alpaca order status enum exposing ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


class _Order:
    def __init__(
        self,
        *,
        client_order_id: str | None,
        status: str,
        broker_order_id: str | None,
    ) -> None:
        self.client_order_id = client_order_id
        self.status = _Status(status)
        self.broker_order_id = broker_order_id


class _FakeBroker:
    """Deterministic fake broker.

    ``list_recent_orders`` returns the configured order mix; ``cancel_order``
    records the IDs it was asked to cancel (and may raise for a configured ID).
    """

    def __init__(self, orders: list[_Order], *, raise_on: str | None = None) -> None:
        self._orders = orders
        self._raise_on = raise_on
        self.cancelled: list[str] = []
        self.list_calls: list[int] = []

    async def list_recent_orders(self, *, limit: int):  # noqa: ANN201
        self.list_calls.append(limit)
        return self._orders

    async def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id == self._raise_on:
            raise RuntimeError("broker rejected cancel")
        self.cancelled.append(broker_order_id)


class _NoListBroker:
    """Broker that does NOT expose list_recent_orders (non-Alpaca)."""


class _ListFailsBroker:
    async def list_recent_orders(self, *, limit: int):  # noqa: ANN201
        raise RuntimeError("alpaca 503")

    async def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover
        raise AssertionError("cancel_order must not be called when list fails")


def _mixed_orders() -> list[_Order]:
    """A representative mix.

    Independently, ONLY these two qualify (prefix match + open status +
    non-empty broker_order_id): ``bk-open-new`` and ``bk-open-partial``.
    """
    return [
        # qualifies — prefix + new + has broker id
        _Order(client_order_id="mo_a", status="new", broker_order_id="bk-open-new"),
        # qualifies — prefix + partially_filled + has broker id
        _Order(
            client_order_id="MO_B",  # case-insensitive prefix match
            status="partially_filled",
            broker_order_id="bk-open-partial",
        ),
        # wrong prefix (sentinel) — skip
        _Order(client_order_id="sn_c", status="new", broker_order_id="bk-sn"),
        # no prefix at all — skip
        _Order(client_order_id="xx_d", status="new", broker_order_id="bk-xx"),
        # prefix but terminal status (filled) — skip
        _Order(client_order_id="mo_e", status="filled", broker_order_id="bk-filled"),
        # prefix but terminal status (canceled) — skip
        _Order(client_order_id="mo_f", status="canceled", broker_order_id="bk-cxl"),
        # prefix + open but no broker_order_id — skip
        _Order(client_order_id="mo_g", status="new", broker_order_id=None),
        # prefix + None client id edge — skip
        _Order(client_order_id=None, status="new", broker_order_id="bk-none"),
    ]


# Independently-derived expectations (NOT via the engine fn).
EXPECTED_CANCELLED_IDS = ["bk-open-new", "bk-open-partial"]
EXPECTED_COUNT = 2


def _capture():
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    return cap


# ── tpcore.cancel_stale_orders behavior ─────────────────────────────────────


async def test_cancels_exact_id_set_and_count() -> None:
    broker = _FakeBroker(_mixed_orders())
    cap = _capture()
    try:
        n = await cancel_stale_orders(
            broker, order_prefix=MOMENTUM_PREFIX, log_namespace=MOMENTUM_NS
        )
    finally:
        structlog.reset_defaults()
    assert broker.cancelled == EXPECTED_CANCELLED_IDS
    assert n == EXPECTED_COUNT
    assert broker.list_calls == [500]
    # The success summary event is emitted exactly once with the count.
    summary = [e for e in cap.entries if e["event"] == f"{MOMENTUM_NS}.stale_orders_cancelled"]
    assert len(summary) == 1
    assert summary[0]["n"] == EXPECTED_COUNT


async def test_no_list_recent_orders_returns_zero_no_events() -> None:
    cap = _capture()
    try:
        n = await cancel_stale_orders(
            _NoListBroker(), order_prefix=MOMENTUM_PREFIX, log_namespace=MOMENTUM_NS
        )
    finally:
        structlog.reset_defaults()
    assert n == 0
    assert cap.entries == []


async def test_list_failure_warns_and_returns_zero() -> None:
    cap = _capture()
    try:
        n = await cancel_stale_orders(
            _ListFailsBroker(), order_prefix=MOMENTUM_PREFIX, log_namespace=MOMENTUM_NS
        )
    finally:
        structlog.reset_defaults()
    assert n == 0
    events = [e["event"] for e in cap.entries]
    assert events == [f"{MOMENTUM_NS}.list_orders_failed"]
    assert cap.entries[0]["log_level"] == "warning"


async def test_cancel_failure_is_contained_and_warns() -> None:
    # First qualifying order's cancel raises; second still cancels.
    broker = _FakeBroker(_mixed_orders(), raise_on="bk-open-new")
    cap = _capture()
    try:
        n = await cancel_stale_orders(
            broker, order_prefix=MOMENTUM_PREFIX, log_namespace=MOMENTUM_NS
        )
    finally:
        structlog.reset_defaults()
    assert broker.cancelled == ["bk-open-partial"]
    assert n == 1
    names = [e["event"] for e in cap.entries]
    assert f"{MOMENTUM_NS}.cancel_failed" in names
    assert f"{MOMENTUM_NS}.stale_orders_cancelled" in names
    cf = next(e for e in cap.entries if e["event"] == f"{MOMENTUM_NS}.cancel_failed")
    assert cf["log_level"] == "warning"
    assert cf["broker_order_id"] == "bk-open-new"


async def test_nothing_to_cancel_emits_no_summary_event() -> None:
    # All orders are terminal/non-matching → 0 cancelled, no summary event.
    orders = [
        _Order(client_order_id="mo_x", status="filled", broker_order_id="bk1"),
        _Order(client_order_id="sn_y", status="new", broker_order_id="bk2"),
    ]
    broker = _FakeBroker(orders)
    cap = _capture()
    try:
        n = await cancel_stale_orders(
            broker, order_prefix=MOMENTUM_PREFIX, log_namespace=MOMENTUM_NS
        )
    finally:
        structlog.reset_defaults()
    assert n == 0
    assert broker.cancelled == []
    assert [e["event"] for e in cap.entries] == []


# ── momentum delegate produces the SAME independently-expected results ───────


async def test_momentum_delegate_matches_independent_expectation() -> None:
    from momentum.scheduler import MomentumScheduler

    broker = _FakeBroker(_mixed_orders())
    cap = _capture()
    try:
        n = await MomentumScheduler._cancel_stale_momentum_orders(broker)
    finally:
        structlog.reset_defaults()
    assert broker.cancelled == EXPECTED_CANCELLED_IDS
    assert n == EXPECTED_COUNT
    # Momentum's exact current event name, unchanged by the delegation.
    summary = [
        e for e in cap.entries if e["event"] == "momentum.scheduler.stale_orders_cancelled"
    ]
    assert len(summary) == 1
    assert summary[0]["n"] == EXPECTED_COUNT


async def test_momentum_delegate_list_failure_event_name_unchanged() -> None:
    cap = _capture()
    try:
        from momentum.scheduler import MomentumScheduler

        n = await MomentumScheduler._cancel_stale_momentum_orders(_ListFailsBroker())
    finally:
        structlog.reset_defaults()
    assert n == 0
    assert [e["event"] for e in cap.entries] == ["momentum.scheduler.list_orders_failed"]
