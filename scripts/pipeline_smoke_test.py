"""End-to-end pipeline smoke test — engine → broker → monitor → AAR.

Runs in two modes depending on what ``tpcore.calendar.session_contains``
reports for "is the NYSE regular session open right now":

**LIVE mode (market open)** — exercises the full fill round-trip:

1. Fetches the live SPY quote via ``tpcore.alpaca.AlpacaDataAdapter.get_quote``
   so TP/SL anchor to current price (no drift from yesterday's close).
2. Submits one Tier 1 BUY bracket via
   ``AlpacaPaperBrokerAdapter.submit_tier1_only`` (1 share, wide TP/SL).
3. Inserts the matching ``platform.open_orders`` row with per-trade
   engine-shaped ``decision_data`` carrying ``tier2_qty = 1`` so the
   monitor reacts.
4. Polls open_orders for up to 60 s expecting status flips to
   ``'filled'`` and a tier2 row to appear (monitor's cascade).

**WIRE mode (market closed)** — exercises the broker→monitor wire only:

1. Fetches the live (last-known) quote; Alpaca returns the last NBBO
   even when the session is closed.
2. Submits one ``SIMPLE`` LIMIT BUY at ``mid * 0.5`` via
   ``broker.place_order`` — far enough below market that it cannot
   fill even if the session opens during the test window.
3. Inserts the matching open_orders row.
4. Polls ``platform.application_log`` for any ``engine='trade_monitor'``
   ``EVENT_*`` row tagged with our ``alpaca_order_id`` — proves the
   trade-monitor stream is live AND its ``_lookup_open_order`` path
   (previously the asyncpg-loop-mismatch crash site) is working.

Either mode always cancels any test orders + deletes its open_orders
rows in a ``finally`` block. The test is idempotent across re-runs.

Prerequisites
-------------
* ``tpcore.trade_monitor`` daemon running (launchd installs it).
* ``platform.open_orders`` migration applied (20260512_0000).
* ALPACA_KEY / ALPACA_SECRET / DATABASE_URL (or DATABASE_URL_IPV4).

What it doesn't do
------------------
* Wait for the Tier 2 to fill — the mocked-stream integration test
  (``tpcore/tests/test_trade_monitor.py::test_tier2_fill_writes_aar_and_bumps_risk_state``)
  covers the AAR write deterministically.
* Submit through the engine's order manager — that path is the full
  engine, outside the scope of a smoke. We exercise the broker → DB →
  monitor leg only.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/pipeline_smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.alpaca.data_adapter import AlpacaDataAdapter
from tpcore.calendar import next_open, session_contains
from tpcore.db import build_asyncpg_pool
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from tpcore.logging.db_handler import DBLogHandler

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger("scripts.pipeline_smoke_test")

TEST_ENGINE = "reversion"  # Tier-2 OCO per-trade engine — monitor's tier2 dispatch fires
TEST_TICKER = "SPY"
TEST_QTY = 1
# Wide TP/SL so the bracket's exit legs don't fire during the live smoke.
# 1% buffer against the live quote — large enough to absorb 60s of intraday
# volatility on SPY without tripping the bracket, small enough to keep
# the test economically realistic.
TP_OFFSET_PCT = Decimal("0.01")
SL_OFFSET_PCT = Decimal("0.01")
# Wire-mode limit price = mid * this — far below market so the order
# cannot fill even if the session opens during the test window.
WIRE_LIMIT_PRICE_PCT = Decimal("0.50")
POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 60.0


def _is_market_open(now: datetime | None = None) -> bool:
    """Wrapper around ``tpcore.calendar.session_contains``.

    Defers to the project's NYSE-aware calendar (``exchange_calendars``)
    rather than a hardcoded UTC window — so half-days, holidays, and
    DST shifts are handled the same way the engines see them.
    """
    return session_contains(now or datetime.now(UTC))


def _wire_mode_banner(now: datetime | None = None) -> str:
    """Banner text for wire mode (market closed) runs."""
    now = now or datetime.now(UTC)
    try:
        nxt = next_open(now).astimezone(UTC)
        return (
            f"WIRE MODE — NYSE session closed at {now.isoformat(timespec='minutes')}. "
            f"Next open per tpcore.calendar: {nxt.isoformat(timespec='minutes')}. "
            "Testing broker→monitor event wire only (no fill expected)."
        )
    except Exception:
        return (
            f"WIRE MODE — NYSE session closed at {now.isoformat(timespec='minutes')}. "
            "Testing broker→monitor event wire only (no fill expected)."
        )


async def _cleanup_test_rows(pool: asyncpg.Pool, ticker: str) -> int:
    """Drop any leftover smoke rows so reruns start clean."""
    sql = """
        DELETE FROM platform.open_orders
        WHERE engine = $1
          AND ticker = $2
          AND trade_id LIKE 'pipeline_smoke_%'
    """
    async with pool.acquire() as conn:
        result = await conn.execute(sql, TEST_ENGINE, ticker)
    # asyncpg returns 'DELETE N'
    n = 0
    if isinstance(result, str) and result.startswith("DELETE"):
        try:
            n = int(result.split(" ", 1)[1])
        except (IndexError, ValueError):
            n = 0
    return n


async def _cancel_test_orders(broker: AlpacaPaperBrokerAdapter, ticker: str) -> int:
    """Cancel every open Alpaca order on the test ticker."""
    cancelled = 0
    try:
        orders = await broker.list_recent_orders(limit=200)
    except Exception as exc:
        logger.warning("cleanup.list_orders_failed error=%s", exc)
        return 0
    for o in orders:
        if o.symbol != ticker:
            continue
        status = getattr(o.status, "value", str(o.status))
        if status not in ("new", "accepted", "pending_new", "partially_filled", "held"):
            continue
        try:
            await broker.cancel_order(o.broker_order_id)
            cancelled += 1
        except Exception as exc:
            logger.warning("cleanup.cancel_failed broker_id=%s error=%s", o.broker_order_id, exc)
    return cancelled


def _build_decision_data(
    *,
    ticker: str,
    qty: int,
    entry_estimate: Decimal,
    tp_price: Decimal,
    sl_price: Decimal,
    far_tp: Decimal,
) -> dict[str, Any]:
    """Shape that matches what the per-trade engine order manager persists."""
    return {
        "decision": {
            "ticker": ticker,
            "qty": qty * 2,            # total across tiers
            "tier1_qty": qty,
            "tier2_qty": qty,
            "notional_usd": str(entry_estimate * qty * 2),
            "risk_amount_usd": str(entry_estimate * SL_OFFSET_PCT * qty),
            "order_payloads": [
                {
                    "client_order_id": "placeholder",   # rewritten below
                    "symbol": ticker,
                    "side": "buy",
                    "qty": str(qty),
                    "type": "market",
                    "time_in_force": "day",
                    "order_class": "bracket",
                    "take_profit": {"limit_price": str(tp_price)},
                    "stop_loss": {"stop_price": str(sl_price)},
                },
                {
                    "client_order_id": "placeholder",
                    "symbol": ticker,
                    "side": "sell",
                    "qty": str(qty),
                    "type": "limit",
                    "limit_price": str(far_tp),
                    "time_in_force": "gtc",
                },
            ],
            "constructed_at": datetime.now(UTC).isoformat(),
        },
        "assessment": {
            "ticker": ticker,
            "as_of": datetime.now(UTC).date().isoformat(),
            "phase": "ACTIVE",
            "entry_price": str(entry_estimate),
            "stop_price": str(sl_price),
            "take_profit_mid": str(tp_price),
            "take_profit_far": str(far_tp),
            "notes": "pipeline_smoke_test synthetic decision",
        },
    }


_INSERT_TIER1_SQL = """
    INSERT INTO platform.open_orders
        (engine, trade_id, ticker, order_type,
         alpaca_order_id, status, decision_data)
    VALUES ($1, $2, $3, 'tier1', $4, 'pending', $5::jsonb)
"""

_SELECT_TIER1_SQL = """
    SELECT status, fill_price, filled_at
    FROM platform.open_orders
    WHERE engine = $1 AND trade_id = $2 AND order_type = 'tier1'
"""

_SELECT_TIER2_SQL = """
    SELECT alpaca_order_id, status
    FROM platform.open_orders
    WHERE engine = $1 AND trade_id = $2 AND order_type = 'tier2'
"""

# Wire-mode predicate: any trade_monitor event whose data payload
# carries our alpaca_order_id proves the websocket → DB-log → pool-
# acquire path is live. EVENT_NEW or EVENT_PENDING_NEW are the
# expected first events; we accept any EVENT_* (or FILL_CONFIRMED on
# the off chance the limit somehow fills).
_SELECT_WIRE_EVENT_SQL = """
    SELECT event_type, recorded_at
    FROM platform.application_log
    WHERE engine = 'trade_monitor'
      AND (event_type LIKE 'EVENT_%' OR event_type = 'FILL_CONFIRMED')
      AND data->>'alpaca_order_id' = $1
    ORDER BY recorded_at ASC
    LIMIT 1
"""


async def _poll_for(
    pool: asyncpg.Pool,
    *,
    label: str,
    predicate,  # async callable(conn) -> (done: bool, payload: Any)
    timeout_sec: float = POLL_TIMEOUT_SEC,
) -> tuple[bool, Any]:
    """Poll until ``predicate`` returns ``(True, payload)`` or timeout fires."""
    started = time.monotonic()
    last_payload: Any = None
    while time.monotonic() - started < timeout_sec:
        async with pool.acquire() as conn:
            done, payload = await predicate(conn)
        last_payload = payload
        if done:
            return True, payload
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return False, last_payload


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    if not os.getenv("ALPACA_KEY") or not os.getenv("ALPACA_SECRET"):
        print("FAILED — ALPACA_KEY/ALPACA_SECRET not set", file=sys.stderr)
        return 1
    market_open = _is_market_open()

    pool = await build_asyncpg_pool(db_url, max_size=4)
    log_handler = DBLogHandler(pool=pool, engine="pipeline_smoke", run_id=uuid.uuid4())
    broker = AlpacaPaperBrokerAdapter()
    # IEX feed for quotes — our Alpaca subscription tier permits IEX but
    # not SIP for recent quote data (bars on SIP are fine; that's a
    # different subscription dimension). SPY is heavily traded on IEX so
    # the quote is reliable for the smoke test's TP/SL anchoring purpose.
    data = AlpacaDataAdapter(feed="iex")

    trade_id = f"pipeline_smoke_{int(datetime.now(UTC).timestamp())}"
    tier1_cid = f"{trade_id}_tier1"
    ticker = TEST_TICKER
    qty = TEST_QTY
    result_payload: dict[str, Any] = {
        "trade_id": trade_id,
        "ticker": ticker,
        "qty": qty,
        "mode": "live" if market_open else "wire",
    }

    if market_open:
        print("=== LIVE MODE — market open. Full broker→monitor→tier2 round-trip. ===")
    else:
        print(f"=== {_wire_mode_banner()} ===")

    try:
        # 0. Cleanup any stale rows from prior runs (idempotency).
        deleted = await _cleanup_test_rows(pool, ticker)
        cancelled_prior = await _cancel_test_orders(broker, ticker)
        if deleted or cancelled_prior:
            print(f"cleanup: deleted {deleted} stale rows, cancelled {cancelled_prior} stale orders")

        # 1. Anchor TP/SL to the LIVE quote (no drift). Works in both modes —
        # Alpaca returns the last NBBO even when the session is closed.
        quote = await data.get_quote(ticker)
        mid = ((quote.bid + quote.ask) / 2).quantize(Decimal("0.01"))
        print(f"  quote: bid={quote.bid} ask={quote.ask} mid={mid}")

        if market_open:
            tp_price = (mid * (Decimal("1") + TP_OFFSET_PCT)).quantize(Decimal("0.01"))
            sl_price = (mid * (Decimal("1") - SL_OFFSET_PCT)).quantize(Decimal("0.01"))
            far_tp = (mid * (Decimal("1") + TP_OFFSET_PCT * 3)).quantize(Decimal("0.01"))
            print(
                f"submitting Tier 1 BUY bracket: {ticker} qty={qty} "
                f"entry≈{mid} TP={tp_price} SL={sl_price}"
            )

            # 2a. Submit the Tier 1 bracket via the engine's primitive.
            tier1_order = await broker.submit_tier1_only(
                ticker=ticker,
                qty=qty,
                side="buy",
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
                client_order_id=tier1_cid,
                engine_id=TEST_ENGINE,
            )
            result_payload["tier1_broker_order_id"] = tier1_order.broker_order_id
            print(f"  alpaca_order_id={tier1_order.broker_order_id}")

            # 3a. Insert the matching open_orders row so the monitor can find it.
            decision_data = _build_decision_data(
                ticker=ticker,
                qty=qty,
                entry_estimate=mid,
                tp_price=tp_price,
                sl_price=sl_price,
                far_tp=far_tp,
            )
            decision_data["decision"]["order_payloads"][0]["client_order_id"] = tier1_cid
            decision_data["decision"]["order_payloads"][1]["client_order_id"] = f"{trade_id}_tier2"
            async with pool.acquire() as conn:
                await conn.execute(
                    _INSERT_TIER1_SQL,
                    TEST_ENGINE,
                    trade_id,
                    ticker,
                    tier1_order.broker_order_id,
                    json.dumps(decision_data, default=str),
                )
            await log_handler.log(
                "PIPELINE_SMOKE_SUBMITTED",
                f"{ticker} tier1 broker={tier1_order.broker_order_id}",
                "INFO",
                {"trade_id": trade_id, **result_payload},
            )

            # 4a. Poll for the monitor to mark Tier 1 as filled.
            async def _tier1_filled(conn: Any) -> tuple[bool, Any]:
                r = await conn.fetchrow(_SELECT_TIER1_SQL, TEST_ENGINE, trade_id)
                if r is None:
                    return False, None
                return r["status"] == "filled", {
                    "status": r["status"],
                    "fill_price": str(r["fill_price"]) if r["fill_price"] is not None else None,
                    "filled_at": r["filled_at"].isoformat() if r["filled_at"] else None,
                }

            tier1_filled, tier1_payload = await _poll_for(
                pool, label="tier1_fill", predicate=_tier1_filled
            )
            result_payload["tier1_fill"] = tier1_payload
            if not tier1_filled:
                print(
                    f"FAILED — Tier 1 didn't reach 'filled' after {POLL_TIMEOUT_SEC}s "
                    f"(last: {tier1_payload}). Is the trade monitor running?",
                    file=sys.stderr,
                )
                return 1
            print(f"  Tier 1 filled @ {tier1_payload['fill_price']}")

            # 5a. Poll for the monitor to submit a Tier 2 row.
            async def _tier2_submitted(conn: Any) -> tuple[bool, Any]:
                r = await conn.fetchrow(_SELECT_TIER2_SQL, TEST_ENGINE, trade_id)
                if r is None or r["alpaca_order_id"] is None:
                    return False, None
                return True, {"alpaca_order_id": r["alpaca_order_id"], "status": r["status"]}

            tier2_seen, tier2_payload = await _poll_for(
                pool, label="tier2_submitted", predicate=_tier2_submitted
            )
            result_payload["tier2"] = tier2_payload
            if not tier2_seen:
                print(
                    f"FAILED — Tier 2 row not seen in open_orders after {POLL_TIMEOUT_SEC}s. "
                    "Monitor either didn't react or submission errored.",
                    file=sys.stderr,
                )
                return 1
            print(f"  Tier 2 submitted: alpaca_order_id={tier2_payload['alpaca_order_id']}")

            await log_handler.log(
                "PIPELINE_SMOKE_PASSED",
                f"{ticker} live mode tier1→fill→tier2 round-trip ok",
                "INFO",
                result_payload,
            )
            print("Pipeline smoke test PASSED (live mode).")
            return 0

        # ───────── WIRE MODE ─────────
        # Submit a SIMPLE LIMIT BUY at mid * 0.5 — far below market so it
        # cannot fill even if the session opens during the test window.
        # We only need the broker ack + the monitor's first EVENT_* row to
        # confirm the stream-to-pool path is healthy.
        limit_price = (mid * WIRE_LIMIT_PRICE_PCT).quantize(Decimal("0.01"))
        print(f"submitting SIMPLE LIMIT BUY: {ticker} qty={qty} limit={limit_price} (mid * {WIRE_LIMIT_PRICE_PCT})")

        # 2b. Submit via the tpcore broker's generic place_order primitive.
        order = Order(
            client_order_id=tier1_cid,
            symbol=ticker,
            side=OrderSide.BUY,
            qty=Decimal(qty),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            order_class=OrderClass.SIMPLE,
            engine_id=TEST_ENGINE,
        )
        placed = await broker.place_order(order)
        result_payload["tier1_broker_order_id"] = placed.broker_order_id
        print(f"  alpaca_order_id={placed.broker_order_id} status={placed.status.value}")

        # 3b. Insert the matching open_orders row so the monitor can attribute
        # the incoming trade_update event back to us. Minimal decision_data —
        # wire mode doesn't exercise the tier2 cascade.
        wire_decision_data = {
            "decision": {
                "ticker": ticker,
                "qty": qty,
                "tier1_qty": qty,
                "tier2_qty": 0,  # no cascade in wire mode
                "notional_usd": str(limit_price * qty),
                "constructed_at": datetime.now(UTC).isoformat(),
                "mode": "wire",
            },
            "assessment": {
                "ticker": ticker,
                "as_of": datetime.now(UTC).date().isoformat(),
                "phase": "ACTIVE",
                "entry_price": str(limit_price),
                "notes": "pipeline_smoke_test wire-mode synthetic decision",
            },
        }
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_TIER1_SQL,
                TEST_ENGINE,
                trade_id,
                ticker,
                placed.broker_order_id,
                json.dumps(wire_decision_data, default=str),
            )
        await log_handler.log(
            "PIPELINE_SMOKE_SUBMITTED",
            f"{ticker} wire-mode limit broker={placed.broker_order_id}",
            "INFO",
            {"trade_id": trade_id, **result_payload},
        )

        # 4b. Poll application_log for ANY trade_monitor EVENT_* row tagged
        # with our alpaca_order_id. That proves: stream up, _lookup_open_order
        # ran, _db_log.log fired — i.e. the pool-acquire path that was
        # crashing pre-fix is healthy.
        async def _wire_event_seen(conn: Any) -> tuple[bool, Any]:
            r = await conn.fetchrow(_SELECT_WIRE_EVENT_SQL, placed.broker_order_id)
            if r is None:
                return False, None
            return True, {
                "event_type": r["event_type"],
                "recorded_at": r["recorded_at"].isoformat(),
            }

        event_seen, event_payload = await _poll_for(
            pool, label="wire_event", predicate=_wire_event_seen,
        )
        result_payload["wire_event"] = event_payload
        if not event_seen:
            print(
                f"FAILED — trade_monitor never logged an EVENT_* row for "
                f"alpaca_order_id={placed.broker_order_id} after "
                f"{POLL_TIMEOUT_SEC}s. Stream or pool path is broken.",
                file=sys.stderr,
            )
            return 1
        print(f"  trade_monitor logged {event_payload['event_type']} at {event_payload['recorded_at']}")

        await log_handler.log(
            "PIPELINE_SMOKE_PASSED",
            f"{ticker} wire mode broker→monitor event wire ok",
            "INFO",
            result_payload,
        )
        print("Pipeline smoke test PASSED (wire mode).")
        return 0
    finally:
        # 6. Always clean up: cancel any test orders + drop the smoke rows.
        try:
            cancelled = await _cancel_test_orders(broker, ticker)
            deleted = await _cleanup_test_rows(pool, ticker)
            print(f"cleanup: cancelled {cancelled} order(s), deleted {deleted} smoke row(s)")
        except Exception as exc:  # pragma: no cover
            logger.warning("cleanup.unexpected error=%s", exc)
        await pool.close()


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
