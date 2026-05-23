"""Paper-trading smoke test — one ~$100 bracket round-trip, no engine.

Proves the full pipeline before any engine submits its first live paper
order: ``platform.application_log`` → universe selection → execution
risk → ``AlpacaPaperBrokerAdapter`` → Alpaca paper account → cancel.

Steps
-----
1. Read the most recent ``UNIVERSE_SIMULATION`` row from
   ``platform.application_log`` (emitted by ``scripts/simulate_universe.py``).
2. Pick the first Sigma candidate whose latest close fits a 2-share
   $100 notional (>= ``SMA_WINDOW`` bars on file, ``close <= $100``).
3. Compute the bracket take-profit from the 20-day SMA (overridden to
   entry × 1.005 if the SMA is below entry — never accept an invalid
   bracket).
4. Submit through
   ``AlpacaPaperBrokerAdapter.submit_execution_decision()`` as a
   ``sigma.models.ExecutionDecision`` with two identical bracket
   payloads (tier1_qty=tier2_qty=1, sum to qty=2).
5. Log ``SMOKE_ORDER_SUBMITTED`` to ``platform.application_log``
   (engine='smoke_test', ``data.test_trade = true``).
6. Cancel every placed order via ``broker.cancel_order(...)``. Cancel
   failures are *warnings* — the order was submitted, that's the
   acceptance criterion.
7. Log ``SMOKE_ORDER_CANCELLED`` and print PASS / FAIL.

Idempotent — each run uses fresh UUID-suffixed ``client_order_id``s.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sigma.models import ExecutionDecision

from tpcore.alpaca.broker_adapter import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.logging.db_handler import DBLogHandler

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger("scripts.smoke_test")

NOTIONAL_USD = Decimal("100")
SL_PCT = Decimal("0.03")  # -3% stop-loss
SMA_WINDOW = 20

_LATEST_SIM_SQL = """
    SELECT data
    FROM platform.application_log
    WHERE event_type = 'UNIVERSE_SIMULATION'
    ORDER BY recorded_at DESC
    LIMIT 1
"""

_RECENT_BARS_SQL = """
    SELECT close
    FROM platform.prices_daily
    WHERE ticker = $1
    ORDER BY date DESC
    LIMIT $2
"""


def _quantize(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def _build_payload(
    *,
    ticker: str,
    qty: int,
    tp_price: Decimal,
    sl_price: Decimal,
    client_order_id: str,
) -> dict[str, Any]:
    """One BUY bracket-market payload in the shape the broker adapter expects."""
    return {
        "client_order_id": client_order_id,
        "symbol": ticker,
        "side": "buy",
        "qty": str(qty),
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(tp_price)},
        "stop_loss": {"stop_price": str(sl_price)},
    }


async def _pick_candidate(
    pool: asyncpg.Pool, sigma_candidates: list[str]
) -> tuple[str, Decimal, Decimal]:
    """Walk ``sigma_candidates`` in order, return the first that's price-affordable.

    Affordable = at least ``SMA_WINDOW`` bars on file AND
    ``last_close <= NOTIONAL_USD`` so qty=2 stays close to $100. Returns
    ``(ticker, last_close, sma20)``. Raises if none qualify.
    """
    async with pool.acquire() as conn:
        for ticker in sigma_candidates:
            rows = await conn.fetch(_RECENT_BARS_SQL, ticker, SMA_WINDOW)
            if len(rows) < SMA_WINDOW:
                continue
            closes = [Decimal(str(r["close"])) for r in rows]
            last_close = closes[0]
            if last_close > NOTIONAL_USD:
                continue
            sma20 = sum(closes, Decimal("0")) / Decimal(SMA_WINDOW)
            return ticker, last_close, sma20
    raise RuntimeError(
        f"No Sigma candidate priced <= ${NOTIONAL_USD} with >= {SMA_WINDOW} bars "
        f"(checked {len(sigma_candidates)})."
    )


async def _load_latest_simulation(pool: asyncpg.Pool) -> list[str]:
    """Return ``sigma_candidates`` from the most recent UNIVERSE_SIMULATION row."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LATEST_SIM_SQL)
    if row is None:
        raise RuntimeError(
            "No UNIVERSE_SIMULATION event in platform.application_log — "
            "run scripts/simulate_universe.py first."
        )
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    candidates = data.get("sigma_candidates") or []
    if not candidates:
        raise RuntimeError(
            "Latest UNIVERSE_SIMULATION row has 0 Sigma candidates — nothing to trade."
        )
    return list(candidates)


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("Smoke test FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    if not os.getenv("ALPACA_KEY") or not os.getenv("ALPACA_SECRET"):
        print("Smoke test FAILED — ALPACA_KEY/ALPACA_SECRET not set", file=sys.stderr)
        return 1

    pool = await build_asyncpg_pool(db_url, max_size=4)
    run_id = uuid.uuid4()
    log_handler = DBLogHandler(pool=pool, engine="smoke_test", run_id=run_id)

    try:
        # 1. Pull the latest universe simulation
        try:
            sigma_candidates = await _load_latest_simulation(pool)
        except RuntimeError as exc:
            print(f"Smoke test FAILED — {exc}", file=sys.stderr)
            await log_handler.log(
                "SMOKE_ORDER_FAILED", str(exc), "ERROR", {"reason": "no_simulation"}
            )
            return 1

        # 2. Pick a candidate that fits the $100 notional
        try:
            ticker, last_close, sma20 = await _pick_candidate(pool, sigma_candidates)
        except RuntimeError as exc:
            print(f"Smoke test FAILED — {exc}", file=sys.stderr)
            await log_handler.log(
                "SMOKE_ORDER_FAILED", str(exc), "ERROR", {"reason": "no_affordable_candidate"}
            )
            return 1

        # 3. Bracket params — TP = SMA(20) but never below entry × 1.005
        tp_price = _quantize(max(sma20, last_close * Decimal("1.005")))
        sl_price = _quantize(last_close * (Decimal("1") - SL_PCT))
        qty_total = 2
        tier1_qty = 1
        tier2_qty = 1
        notional = last_close * qty_total

        # 4. Build the decision — two identical bracket payloads (smoke test
        #    isn't validating Sigma's actual tier-split semantics, just the
        #    broker round-trip).
        suffix = uuid.uuid4().hex[:10]
        payloads = [
            _build_payload(
                ticker=ticker,
                qty=tier1_qty,
                tp_price=tp_price,
                sl_price=sl_price,
                client_order_id=f"smoke_t1_{suffix}",
            ),
            _build_payload(
                ticker=ticker,
                qty=tier2_qty,
                tp_price=tp_price,
                sl_price=sl_price,
                client_order_id=f"smoke_t2_{suffix}",
            ),
        ]
        decision = ExecutionDecision(
            ticker=ticker,
            qty=qty_total,
            tier1_qty=tier1_qty,
            tier2_qty=tier2_qty,
            notional_usd=notional,
            risk_amount_usd=notional * SL_PCT,
            order_payloads=payloads,
            constructed_at=datetime.now(UTC),
        )

        # 5. Submit
        broker = AlpacaPaperBrokerAdapter()
        try:
            placed = await broker.submit_execution_decision(decision)
        except Exception as exc:
            msg = f"submit_execution_decision raised: {exc}"
            print(f"Smoke test FAILED — {msg}", file=sys.stderr)
            await log_handler.log(
                "SMOKE_ORDER_FAILED",
                msg,
                "ERROR",
                {"reason": "submit_failed", "error": str(exc), "ticker": ticker},
            )
            return 1

        broker_ids = [o.broker_order_id for o in placed]
        await log_handler.log(
            "SMOKE_ORDER_SUBMITTED",
            f"{ticker} qty={qty_total} placed={len(placed)} broker_ids={broker_ids}",
            "INFO",
            {
                "test_trade": True,
                "ticker": ticker,
                "qty_total": qty_total,
                "last_close": str(last_close),
                "sma20": str(sma20),
                "tp_price": str(tp_price),
                "sl_price": str(sl_price),
                "notional_usd": str(notional),
                "broker_order_ids": broker_ids,
                "client_order_ids": [o.client_order_id for o in placed],
            },
        )
        print(
            f"Submitted {len(placed)} bracket(s) for {ticker} "
            f"(last_close={last_close}, TP={tp_price}, SL={sl_price}): {broker_ids}"
        )

        # 6. Cancel every placed order. Cancel failures are warnings only.
        cancel_failures: list[tuple[str, str]] = []
        for order in placed:
            if not order.broker_order_id:
                continue
            try:
                await broker.cancel_order(order.broker_order_id)
            except Exception as exc:
                cancel_failures.append((order.broker_order_id, str(exc)))
                logger.warning(
                    "smoke_test.cancel_failed broker_id=%s error=%s",
                    order.broker_order_id,
                    exc,
                )

        await log_handler.log(
            "SMOKE_ORDER_CANCELLED",
            f"{ticker} cancelled={len(placed) - len(cancel_failures)}/{len(placed)}",
            "WARNING" if cancel_failures else "INFO",
            {
                "ticker": ticker,
                "broker_order_ids": broker_ids,
                "cancel_failures": [
                    {"broker_id": bid, "error": err} for bid, err in cancel_failures
                ],
            },
        )

        if cancel_failures:
            print(
                f"Smoke test PASSED — {len(placed)} order(s) submitted; "
                f"{len(cancel_failures)} cancel failure(s) (warning only)"
            )
        else:
            print("Smoke test PASSED — order submitted and cancelled successfully")
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
