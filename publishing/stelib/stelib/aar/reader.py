"""AARReader — shared read-side for ``platform.aar_events``.

Both Forensics and Allocator walk the same table. Putting the deserialize
+ ordering logic here keeps the two services from drifting on which AAR
fields they read or how they parse timestamps.

The reader returns lightweight :class:`AARRow` records (the subset of
fields downstream services actually use). Full ``AfterActionReport``
rehydration is left to whoever needs validation — the row reader does
not pydantic-parse, so a malformed jsonb blob can be skipped without
aborting the whole pull.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

_SELECT_BY_ENGINE_SQL = """
    SELECT engine, trade_id, ticker, aar_data, recorded_at
    FROM platform.aar_events
    WHERE engine = $1
    ORDER BY recorded_at ASC
"""

_SELECT_ALL_SQL = """
    SELECT engine, trade_id, ticker, aar_data, recorded_at
    FROM platform.aar_events
    ORDER BY engine, recorded_at ASC
"""


@dataclass(frozen=True)
class AARRow:
    """Minimal AAR slice: only the fields shared services need."""

    engine: str
    trade_id: str
    ticker: str
    pnl_net: Decimal
    exit_ts: datetime
    entry_ts: datetime | None
    exit_reason: str | None


def _parse_ts(raw: object) -> datetime | None:
    """Tolerant ISO-8601 parse — handles ``Z`` suffix and naive strings."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _row_to_aar(record: object) -> AARRow | None:
    """Convert a SELECT row into an :class:`AARRow`. Returns None on bad data."""
    aar_data = record["aar_data"]  # type: ignore[index]
    if isinstance(aar_data, str):
        try:
            aar_data = json.loads(aar_data)
        except (ValueError, TypeError):
            return None
    if not isinstance(aar_data, dict):
        return None
    pnl_raw = aar_data.get("pnl_net")
    exit_raw = aar_data.get("exit_ts")
    if pnl_raw is None or exit_raw is None:
        return None
    try:
        pnl_net = Decimal(str(pnl_raw))
    except (ValueError, ArithmeticError):
        return None
    exit_ts = _parse_ts(exit_raw)
    if exit_ts is None:
        return None
    return AARRow(
        engine=record["engine"],  # type: ignore[index]
        trade_id=record["trade_id"],  # type: ignore[index]
        ticker=record["ticker"],  # type: ignore[index]
        pnl_net=pnl_net,
        exit_ts=exit_ts,
        entry_ts=_parse_ts(aar_data.get("entry_ts")),
        exit_reason=aar_data.get("exit_reason"),
    )


class AARReader:
    """Read-side over ``platform.aar_events`` shared by Forensics + Allocator."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def fetch_by_engine(self, engine: str) -> list[AARRow]:
        async with self._pool.acquire() as conn:
            records = await conn.fetch(_SELECT_BY_ENGINE_SQL, engine)
        out: list[AARRow] = []
        for r in records:
            aar = _row_to_aar(r)
            if aar is not None:
                out.append(aar)
        return out

    async def fetch_all_grouped(self) -> dict[str, list[AARRow]]:
        """Return ``{engine: [AARRow, ...sorted by exit_ts]}``."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(_SELECT_ALL_SQL)
        by_engine: dict[str, list[AARRow]] = {}
        for r in records:
            aar = _row_to_aar(r)
            if aar is None:
                continue
            by_engine.setdefault(aar.engine, []).append(aar)
        for aars in by_engine.values():
            aars.sort(key=lambda a: a.exit_ts)
        return by_engine


__all__ = ["AARReader", "AARRow"]
