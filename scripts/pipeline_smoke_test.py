"""End-to-end live pipeline smoke test — engine → broker → monitor → AAR.

Prerequisites
-------------
* US market open (regular hours, 09:30–16:00 ET / 13:30–20:00 UTC).
* ``tpcore.trade_monitor`` running in a second terminal:
  ``DATABASE_URL=$DATABASE_URL_IPV4 ALPACA_KEY=... ALPACA_SECRET=... \
    ALPACA_PAPER=true python -m tpcore.trade_monitor``
* ``platform.open_orders`` migration applied (20260512_0000).

What it does
------------
1. Submits **one** Tier 1 BUY bracket on a known-liquid ticker (default
   SPY, 1 share) via ``AlpacaPaperBrokerAdapter.submit_tier1_only``.
   Wide TP / SL — we just want the entry leg to fill quickly.
2. Inserts the matching row in ``platform.open_orders`` with a Sigma-
   shaped ``decision_data`` carrying ``tier2_qty = 1`` so the monitor
   will react.
3. Polls ``platform.open_orders`` for up to 60 s, expecting:
   - the Tier 1 row to flip ``status = 'filled'`` (monitor saw the fill);
   - a Tier 2 row to appear (monitor submitted the follow-on).
4. Cancels everything for the test symbol at Alpaca and deletes both
   ``open_orders`` rows. The test is idempotent: re-running cleans up
   any stale rows before submitting a fresh probe.
5. Prints PASS / FAIL and exits 0 / 1.

What it doesn't do
------------------
* Wait for the Tier 2 to fill — Tier 2's far-target TP is well above
  the entry on purpose, so the AAR write step isn't exercised live
  here. The mocked-stream integration test
  (``tpcore/tests/test_trade_monitor.py::test_tier2_fill_writes_aar_and_bumps_risk_state``)
  covers that path deterministically.
* Submit through ``SigmaOrderManager`` — that path requires running
  setup_detection + the rest of the engine pipeline which is well
  outside the scope of a smoke. We exercise the broker → DB →
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
from tpcore.db import build_asyncpg_pool
from tpcore.logging.db_handler import DBLogHandler

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger("scripts.pipeline_smoke_test")

TEST_ENGINE = "sigma"  # use sigma so the monitor's tier2 dispatch fires
TEST_TICKER = "SPY"
TEST_QTY = 1
# Wide TP/SL so the bracket's exit legs don't fire during the smoke.
TP_OFFSET = Decimal("10.00")
SL_OFFSET = Decimal("10.00")
POLL_INTERVAL_SEC = 2.0
POLL_TIMEOUT_SEC = 60.0


def _is_market_open(now: datetime | None = None) -> bool:
    """US regular session 09:30–16:00 ET (13:30–20:00 UTC), Mon–Fri.

    Smoke-grade check — doesn't consult the trading calendar for half-days
    or holidays. Caller should still be willing to skip on a no-fill.
    """
    now = now or datetime.now(UTC)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= minutes <= 20 * 60


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
    """Shape that matches what ``sigma.order_manager`` persists."""
    return {
        "decision": {
            "ticker": ticker,
            "qty": qty * 2,            # total across tiers
            "tier1_qty": qty,
            "tier2_qty": qty,
            "notional_usd": str(entry_estimate * qty * 2),
            "risk_amount_usd": str(SL_OFFSET * qty),
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
    if not _is_market_open():
        print(
            "SKIPPED — US market closed. Re-run between 13:30 and 20:00 UTC on a weekday.",
            file=sys.stderr,
        )
        return 0

    pool = await build_asyncpg_pool(db_url, max_size=4)
    log_handler = DBLogHandler(pool=pool, engine="pipeline_smoke", run_id=uuid.uuid4())
    broker = AlpacaPaperBrokerAdapter()

    trade_id = f"pipeline_smoke_{int(datetime.now(UTC).timestamp())}"
    tier1_cid = f"{trade_id}_tier1"
    ticker = TEST_TICKER
    qty = TEST_QTY
    result_payload: dict[str, Any] = {"trade_id": trade_id, "ticker": ticker, "qty": qty}

    try:
        # 0. Cleanup any stale rows from prior runs (idempotency).
        deleted = await _cleanup_test_rows(pool, ticker)
        cancelled_prior = await _cancel_test_orders(broker, ticker)
        if deleted or cancelled_prior:
            print(f"cleanup: deleted {deleted} stale rows, cancelled {cancelled_prior} stale orders")

        # 1. Snapshot the latest close from platform.prices_daily as the
        # price reference for the bracket. (The broker's get_quote isn't
        # wired on the adapter and the daily close is plenty for sizing a
        # smoke bracket — we want wide TP/SL anyway.)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT close FROM platform.prices_daily WHERE ticker = $1 ORDER BY date DESC LIMIT 1",
                ticker,
            )
        if row is None:
            print(f"FAILED — no prices_daily row for {ticker}", file=sys.stderr)
            return 1
        last_close = Decimal(str(row["close"]))
        tp_price = (last_close + TP_OFFSET).quantize(Decimal("0.01"))
        sl_price = (last_close - SL_OFFSET).quantize(Decimal("0.01"))
        far_tp = (last_close + TP_OFFSET * 3).quantize(Decimal("0.01"))
        print(
            f"submitting Tier 1 BUY bracket: {ticker} qty={qty} "
            f"entry≈{last_close} TP={tp_price} SL={sl_price}"
        )

        # 2. Submit the Tier 1 bracket via the engine's primitive.
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

        # 3. Insert the matching open_orders row so the monitor can find it.
        decision_data = _build_decision_data(
            ticker=ticker,
            qty=qty,
            entry_estimate=last_close,
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

        # 4. Poll for the monitor to mark Tier 1 as filled.
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

        # 5. Poll for the monitor to submit a Tier 2 row.
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

        # All three legs proved: engine submission, monitor reaction, broker
        # round-trip on Tier 2. We don't wait for Tier 2 to fill — the
        # mocked-stream test covers the AAR write deterministically.
        await log_handler.log(
            "PIPELINE_SMOKE_PASSED",
            f"{ticker} tier1→fill→tier2 round-trip ok",
            "INFO",
            result_payload,
        )
        print("Pipeline smoke test PASSED.")
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
