"""Database log handler — writes scheduler events to ``platform.application_log``.

Design notes:

* Retention is enforced *on every write*: after each INSERT the handler
  issues a DELETE for rows older than ``retention_days``. No separate cron
  is needed; the table stays bounded for free. Default is 7 days, which
  covers daily operational checks without hoarding history.

* The handler swallows every database exception. Operational logging must
  never bring down a trading run — if the DB write fails, we log to
  structlog (stdout / Railway) and move on. Stdout is the live view,
  the DB is the queryable archive; both work, but the DB is best-effort.

* JSONB encoding is done in Python with ``json.dumps(default=str)`` so
  ``Decimal`` and ``datetime`` payloads survive without a per-connection
  codec. The SQL casts the text param to ``jsonb`` (NULL passes through).
"""
from __future__ import annotations

import json
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""

_RETENTION_SQL = """
DELETE FROM platform.application_log
WHERE recorded_at < $1
"""


class DBLogHandler:
    """Async writer for ``platform.application_log`` with auto-retention.

    The handler is bound to a single (engine, run_id) so call sites stay
    terse — every event inherits those tags. ``retention_days`` controls
    the rolling window; rows older than that are deleted on each write.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        engine: str,
        run_id: uuid.UUID,
        retention_days: int = 7,
    ) -> None:
        if pool is None:
            raise ValueError("DBLogHandler requires a connection pool")
        if retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        self._pool = pool
        self._engine = engine
        self._run_id = run_id
        self._retention_days = retention_days

    async def log(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Insert one row and prune anything past the retention window.

        Errors are logged to structlog and swallowed — never raised to
        the caller. The trading run continues even if the audit DB is
        unhealthy; the structlog/stdout fallback is still captured by
        Railway's log pipeline.
        """
        data_json = json.dumps(data, default=str) if data is not None else None
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    _INSERT_SQL,
                    self._engine,
                    self._run_id,
                    event_type,
                    severity,
                    message,
                    data_json,
                )
                await conn.execute(_RETENTION_SQL, cutoff)
        except Exception as exc:  # pragma: no cover - DB failure path
            logger.warning(
                "tpcore.logging.db_write_failed",
                engine=self._engine,
                run_id=str(self._run_id),
                event_type=event_type,
                error=str(exc),
            )

    # ────────────────────────────────────────────────────────────────────
    # Convenience methods — one per event_type the schedulers emit.
    # ────────────────────────────────────────────────────────────────────

    async def startup(self, commit_sha: str | None = None) -> None:
        await self.log(
            "STARTUP",
            f"{self._engine} scheduler run starting",
            severity="INFO",
            data={"commit_sha": commit_sha} if commit_sha else None,
        )

    async def shutdown(self, duration_ms: int, exit_code: int) -> None:
        severity = "INFO" if exit_code == 0 else "ERROR"
        await self.log(
            "SHUTDOWN",
            f"{self._engine} scheduler run finished (exit_code={exit_code})",
            severity=severity,
            data={"duration_ms": duration_ms, "exit_code": exit_code},
        )

    async def scan_complete(self, candidates: int, duration_ms: int) -> None:
        await self.log(
            "SCAN_COMPLETE",
            f"scan produced {candidates} candidate(s)",
            severity="INFO",
            data={"candidates": candidates, "duration_ms": duration_ms},
        )

    async def signal(
        self,
        ticker: str,
        score: float,
        direction: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        """Emit a SIGNAL event to platform.application_log.

        Args:
            ticker: symbol the signal fired for.
            score: setup score (0-100 or engine-specific).
            direction: optional 'LONG'/'SHORT'.
            extra_data: optional dict merged into the data payload —
                used to attach FilterDiagnostics or other per-signal
                metadata without changing the method signature again.
                Shallow merged on top of the base keys; if a caller
                passes 'ticker' / 'score' / 'direction' in extra_data
                those override the explicit args.
        """
        data: dict[str, Any] = {
            "ticker": ticker, "score": score, "direction": direction,
        }
        if extra_data:
            data.update(extra_data)
        await self.log(
            "SIGNAL",
            f"signal {ticker} score={score:.2f}"
            + (f" direction={direction}" if direction else ""),
            severity="INFO",
            data=data,
        )

    async def order_submitted(
        self, ticker: str, quantity: int, order_id: str | None = None
    ) -> None:
        await self.log(
            "ORDER_SUBMITTED",
            f"order submitted {ticker} qty={quantity}",
            severity="INFO",
            data={"ticker": ticker, "quantity": quantity, "order_id": order_id},
        )

    async def fill_confirmed(
        self,
        ticker: str,
        fill_price: Any | None = None,
        pnl: Any | None = None,
    ) -> None:
        await self.log(
            "FILL_CONFIRMED",
            f"fill confirmed {ticker}"
            + (f" px={fill_price}" if fill_price is not None else "")
            + (f" pnl={pnl}" if pnl is not None else ""),
            severity="INFO",
            data={"ticker": ticker, "fill_price": fill_price, "pnl": pnl},
        )

    async def error(self, exception: BaseException, context: str) -> None:
        await self.log(
            "ERROR",
            f"{context}: {exception}",
            severity="ERROR",
            data={
                "context": context,
                "exception_type": type(exception).__name__,
                "traceback": "".join(
                    traceback.format_exception(
                        type(exception), exception, exception.__traceback__
                    )
                ),
            },
        )


__all__ = ["DBLogHandler"]
