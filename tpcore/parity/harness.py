"""Live/paper parity harness.

Submits the *same* order to both paper and live Alpaca endpoints and
records the fill drift to ``platform.data_quality_log`` (``kind='parity_drift'``;
Plan 2 consolidation). A growing drift is an early signal of broker-side
behavior changes or strategy assumptions that no longer hold.

Operational rules:

* The harness is non-blocking on the live side. If the live submission
  fails (network, auth, rejected) the paper trade still proceeds and
  ``submit_pair`` returns a record with ``live_fill_price=None`` and a
  log line explaining why.
* The live order is intentionally tiny — the caller can pass a custom
  ``live_qty`` (default 1 share) to limit capital at risk while still
  measuring fill behavior. The harness never auto-amplifies size.
* The drift formula matches Sigma's ``ExecutionQualityWriter`` slippage
  convention: positive drift = live worse than paper for the buyer
  (live filled higher), negative = live better.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.interfaces.broker import BrokerExecutionInterface, Order, OrderSide

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

DEFAULT_LIVE_QTY = Decimal("1")
DEFAULT_FILL_TIMEOUT_SECONDS = 30


class ParityDriftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    paper_fill_price: Decimal | None
    live_fill_price: Decimal | None
    drift_bps: Decimal | None
    paper_filled_at: datetime | None
    live_filled_at: datetime | None
    timestamp: datetime
    # B7 — snapshot of the ticker's most-recent spread observation at
    # the moment of submission. NULL when no row exists yet (new
    # ticker, fresh universe). ``spread_observed_at`` tells callers
    # how stale the snapshot is.
    spread_at_order_pct: Decimal | None = None
    spread_observed_at: datetime | None = None


@dataclass
class _SubmissionOutcome:
    fill_price: Decimal | None
    filled_at: datetime | None
    error: str | None


class LivePaperParityHarness:
    """Submit identical orders to two brokers and log the resulting drift."""

    def __init__(
        self,
        paper_broker: BrokerExecutionInterface,
        live_broker: BrokerExecutionInterface,
        db_pool,
        *,
        live_qty: Decimal = DEFAULT_LIVE_QTY,
        fill_timeout_seconds: int = DEFAULT_FILL_TIMEOUT_SECONDS,
    ) -> None:
        self._paper = paper_broker
        self._live = live_broker
        self._pool = db_pool
        self._live_qty = live_qty
        self._fill_timeout_seconds = fill_timeout_seconds

    async def submit_pair(self, order: Order) -> ParityDriftRecord:
        """Submit ``order`` to both brokers and persist the drift.

        ``order`` is the engine's intended trade. The paper submission uses
        ``order`` as-is; the live submission is a clone with ``qty=live_qty``
        (default 1 share) so the parity test consumes minimal real capital.

        Before submitting we snapshot the ticker's most-recent
        ``spread_observations`` row — when the row lands in
        ``data_quality_log`` (kind='parity_drift') later, downstream drift
        analysis can see what the quoted spread looked like at submission
        time, not just the fill price.

        Returns a record even when the live side fails — failure populates
        ``live_fill_price=None`` and a log entry explains why.
        """
        spread_at_order_pct, spread_observed_at = await self._latest_spread_for(order.symbol)
        live_order = self._build_live_clone(order)
        paper_outcome, live_outcome = await asyncio.gather(
            self._submit_and_wait(self._paper, order, label="paper"),
            self._submit_and_wait(self._live, live_order, label="live"),
        )
        record = self._build_record(
            order,
            paper_outcome=paper_outcome,
            live_outcome=live_outcome,
            spread_at_order_pct=spread_at_order_pct,
            spread_observed_at=spread_observed_at,
        )
        await self._persist(record)
        return record

    async def _latest_spread_for(self, ticker: str) -> tuple[Decimal | None, datetime | None]:
        """Most-recent ``spread_observations`` row for ``ticker``, or (None, None)."""
        if self._pool is None:
            return None, None
        sql = """
            SELECT spread_pct, observed_at
            FROM platform.spread_observations
            WHERE ticker = $1
            ORDER BY observed_at DESC
            LIMIT 1
        """
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, ticker)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "tpcore.parity.spread_lookup_failed",
                ticker=ticker,
                error=str(exc),
            )
            return None, None
        if row is None:
            return None, None
        return Decimal(str(row["spread_pct"])), row["observed_at"]

    def _build_live_clone(self, paper_order: Order) -> Order:
        """Clone ``paper_order`` with the live-side mini quantity."""
        return paper_order.model_copy(update={"qty": self._live_qty})

    async def _submit_and_wait(
        self,
        broker: BrokerExecutionInterface,
        order: Order,
        *,
        label: str,
    ) -> _SubmissionOutcome:
        try:
            placed = await broker.place_order(order)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "tpcore.parity.submit_failed",
                side=label,
                client_order_id=order.client_order_id,
                error=str(exc),
            )
            return _SubmissionOutcome(fill_price=None, filled_at=None, error=str(exc))

        # If the broker returns avg_fill_price immediately (e.g. paper IOC), skip the wait.
        if placed.avg_fill_price is not None and placed.filled_at is not None:
            return _SubmissionOutcome(
                fill_price=placed.avg_fill_price,
                filled_at=placed.filled_at,
                error=None,
            )

        # Otherwise poll the broker for up to fill_timeout seconds.
        deadline = asyncio.get_event_loop().time() + self._fill_timeout_seconds
        order_id = placed.broker_order_id or placed.client_order_id
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.0)
            try:
                refreshed = await broker.get_order(order_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "tpcore.parity.poll_failed",
                    side=label,
                    order_id=order_id,
                    error=str(exc),
                )
                return _SubmissionOutcome(fill_price=None, filled_at=None, error=str(exc))
            if refreshed.avg_fill_price is not None and refreshed.filled_at is not None:
                return _SubmissionOutcome(
                    fill_price=refreshed.avg_fill_price,
                    filled_at=refreshed.filled_at,
                    error=None,
                )
        return _SubmissionOutcome(
            fill_price=None,
            filled_at=None,
            error=f"timed out waiting for fill after {self._fill_timeout_seconds}s",
        )

    @staticmethod
    def _build_record(
        order: Order,
        *,
        paper_outcome: _SubmissionOutcome,
        live_outcome: _SubmissionOutcome,
        spread_at_order_pct: Decimal | None = None,
        spread_observed_at: datetime | None = None,
    ) -> ParityDriftRecord:
        drift_bps: Decimal | None = None
        if paper_outcome.fill_price is not None and live_outcome.fill_price is not None:
            paper = paper_outcome.fill_price
            live = live_outcome.fill_price
            if paper > 0:
                # Convention: positive bps = live worse than paper from the
                # buyer's perspective (live filled at a higher price). For
                # SELL orders we flip so positive bps is consistently
                # "worse for our P&L".
                if order.side is OrderSide.BUY:
                    diff = live - paper
                else:
                    diff = paper - live
                drift_bps = (diff / paper * Decimal("10000")).quantize(Decimal("0.01"))
        return ParityDriftRecord(
            client_order_id=order.client_order_id,
            paper_fill_price=paper_outcome.fill_price,
            live_fill_price=live_outcome.fill_price,
            drift_bps=drift_bps,
            paper_filled_at=paper_outcome.filled_at,
            live_filled_at=live_outcome.filled_at,
            timestamp=datetime.now(UTC),
            spread_at_order_pct=spread_at_order_pct,
            spread_observed_at=spread_observed_at,
        )

    async def _persist(self, record: ParityDriftRecord) -> None:
        """Persist the drift record to ``platform.data_quality_log``
        (``kind='parity_drift'``; Plan 2 consolidation — the dedicated
        ``parity_drift_log`` table was dropped in migration 0300).

        All ParityDriftRecord fields live in the jsonb ``notes``; the
        ``timestamp`` column carries the submission time so the weekly summary
        can window on it. The typed metric columns stay NULL (validation-only)."""
        if self._pool is None:
            return
        from tpcore.quality.data_quality import KIND_PARITY_DRIFT, write_row

        notes = {
            "client_order_id": record.client_order_id,
            "paper_fill_price": record.paper_fill_price,
            "live_fill_price": record.live_fill_price,
            "drift_bps": record.drift_bps,
            "paper_filled_at": record.paper_filled_at,
            "live_filled_at": record.live_filled_at,
            "spread_at_order_pct": record.spread_at_order_pct,
            "spread_observed_at": record.spread_observed_at,
        }
        try:
            await write_row(
                self._pool,
                kind=KIND_PARITY_DRIFT,
                source=record.client_order_id,
                timestamp=record.timestamp,
                notes=notes,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "tpcore.parity.persist_failed",
                client_order_id=record.client_order_id,
                error=str(exc),
            )

    # ───────────────────────────────────────────────────────────────────────
    # Backwards-compat alias — older callers may have stubbed `submit_parallel`.
    # ───────────────────────────────────────────────────────────────────────
    async def submit_parallel(self, order: Order) -> ParityDriftRecord:
        """Deprecated alias for :meth:`submit_pair`."""
        return await self.submit_pair(order)


# ─── Weekly summary helper ────────────────────────────────────────────────


@dataclass
class DriftSummary:
    engine_id: str | None
    n_records: int
    avg_drift_bps: float
    p95_drift_bps: float


async def weekly_drift_summary(
    pool: asyncpg.Pool,
    *,
    engine_id: str | None = None,
    days: int = 7,
) -> DriftSummary:
    """Aggregate parity-drift rows from the last ``days`` days.

    Plan 2: parity drift rows live in ``platform.data_quality_log``
    (``kind='parity_drift'``); ``drift_bps`` + ``client_order_id`` are read from
    the jsonb ``notes``. The schema doesn't store engine_id directly; we infer
    by matching the ``client_order_id`` prefix (``sigma_``, ``reversion_``,
    ``vector_``). Pass ``engine_id=None`` for an all-engines summary.
    """
    where = "kind = 'parity_drift' AND timestamp >= now() - $1::interval"
    params: list = [f"{days} days"]
    if engine_id:
        where += " AND notes->>'client_order_id' LIKE $2"
        params.append(f"{engine_id}_%")
    sql = f"""
        SELECT
            count(*) AS n,
            COALESCE(avg((notes->>'drift_bps')::numeric), 0)::float AS avg_bps,
            COALESCE(
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY (notes->>'drift_bps')::numeric),
                0)::float AS p95_bps
        FROM platform.data_quality_log
        WHERE {where} AND notes->>'drift_bps' IS NOT NULL
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return DriftSummary(
        engine_id=engine_id,
        n_records=int(row["n"]) if row else 0,
        avg_drift_bps=float(row["avg_bps"]) if row else 0.0,
        p95_drift_bps=float(row["p95_bps"]) if row else 0.0,
    )


__all__ = [
    "DEFAULT_FILL_TIMEOUT_SECONDS",
    "DEFAULT_LIVE_QTY",
    "DriftSummary",
    "LivePaperParityHarness",
    "ParityDriftRecord",
    "weekly_drift_summary",
]
