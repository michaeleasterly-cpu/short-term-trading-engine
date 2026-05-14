"""ForensicsService — detect trigger conditions across engine AARs.

Three trigger kinds, one detector each:

* ``outlier_loss``  — a single trade with ``pnl_net`` below
  ``mean - OUTLIER_SIGMA * stdev`` of the engine's historical
  distribution. Requires ≥ ``MIN_AARS_FOR_OUTLIER`` samples; below that,
  σ is too noisy to call anything an outlier.
* ``loss_cluster`` — ``LOSS_CLUSTER_K`` consecutive trades with
  ``pnl_net < 0``. Fires on the K-th loss; if losses continue,
  re-fires only when the streak grows past the previous fire point.
* ``drawdown_period`` — the per-engine equity curve (cumulative
  ``pnl_net`` over time) is in drawdown (below peak) by at least
  ``DRAWDOWN_PCT_THRESHOLD`` for ≥ ``DRAWDOWN_DAYS_THRESHOLD`` consecutive
  days. Computed from AAR ``exit_ts`` dates, not session counts.

Idempotency: each trigger carries a deterministic ``fingerprint`` in
``payload``; the inserter skips rows that already exist with the same
``(trigger_kind, payload->>'fingerprint')``. Re-running daily is safe.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from tpcore.aar import AARReader, AARRow

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Thresholds — change in one place so the spec doc and code agree.
# ────────────────────────────────────────────────────────────────────────

OUTLIER_SIGMA = Decimal("3.0")
MIN_AARS_FOR_OUTLIER = 5
LOSS_CLUSTER_K = 3
DRAWDOWN_PCT_THRESHOLD = Decimal("0.10")  # 10% peak-to-trough
DRAWDOWN_DAYS_THRESHOLD = 14              # consecutive days in DD


class TriggerKind(StrEnum):
    OUTLIER_LOSS = "outlier_loss"
    LOSS_CLUSTER = "loss_cluster"
    DRAWDOWN_PERIOD = "drawdown_period"


@dataclass(frozen=True)
class ForensicsTrigger:
    """One detection result, ready to insert."""

    trigger_kind: TriggerKind
    engine: str
    fingerprint: str
    payload: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# Detectors — pure functions over a sorted list of AARRecord for one engine
# ────────────────────────────────────────────────────────────────────────


def detect_outlier_losses(aars: list[AARRow]) -> list[ForensicsTrigger]:
    """Flag trades with ``pnl_net`` below ``mean - OUTLIER_SIGMA * stdev``."""
    if len(aars) < MIN_AARS_FOR_OUTLIER:
        return []
    pnls = [float(a.pnl_net) for a in aars]
    mean = statistics.fmean(pnls)
    stdev = statistics.pstdev(pnls)
    if stdev == 0:
        return []
    threshold = mean - float(OUTLIER_SIGMA) * stdev
    triggers: list[ForensicsTrigger] = []
    for a in aars:
        if float(a.pnl_net) >= threshold:
            continue
        fp = f"{a.engine}|{a.trade_id}"
        triggers.append(
            ForensicsTrigger(
                trigger_kind=TriggerKind.OUTLIER_LOSS,
                engine=a.engine,
                fingerprint=fp,
                payload={
                    "engine": a.engine,
                    "trade_id": a.trade_id,
                    "ticker": a.ticker,
                    "pnl_net": str(a.pnl_net),
                    "mean": f"{mean:.4f}",
                    "stdev": f"{stdev:.4f}",
                    "threshold": f"{threshold:.4f}",
                    "exit_ts": a.exit_ts.isoformat(),
                    "fingerprint": fp,
                },
            )
        )
    return triggers


def detect_loss_cluster(aars: list[AARRow]) -> list[ForensicsTrigger]:
    """Flag every K-th consecutive losing trade."""
    if len(aars) < LOSS_CLUSTER_K:
        return []
    streak: list[AARRow] = []
    triggers: list[ForensicsTrigger] = []
    for a in aars:
        if a.pnl_net < 0:
            streak.append(a)
            if len(streak) >= LOSS_CLUSTER_K and len(streak) % LOSS_CLUSTER_K == 0:
                last = streak[-1]
                fp = f"{a.engine}|cluster|{last.trade_id}|{len(streak)}"
                triggers.append(
                    ForensicsTrigger(
                        trigger_kind=TriggerKind.LOSS_CLUSTER,
                        engine=a.engine,
                        fingerprint=fp,
                        payload={
                            "engine": a.engine,
                            "streak_length": len(streak),
                            "trade_ids": [s.trade_id for s in streak[-len(streak):]],
                            "total_loss": str(sum((s.pnl_net for s in streak), Decimal("0"))),
                            "ended_at": last.exit_ts.isoformat(),
                            "fingerprint": fp,
                        },
                    )
                )
        else:
            streak = []
    return triggers


def detect_drawdown_period(aars: list[AARRow]) -> list[ForensicsTrigger]:
    """Flag drawdown ≥ pct threshold sustained ≥ days threshold.

    Drawdown is computed off the cumulative pnl_net "equity curve",
    starting at 0. ``pct`` here is the absolute pnl drop divided by
    peak-equity-plus-bootstrap; if peak ≤ 0 (we've been net negative
    from trade one), the trigger is skipped because the % calc is
    meaningless. The detector returns at most one trigger per
    (engine, drawdown-start-date) pair so a long sustained drawdown
    fires once, not every day.
    """
    if not aars:
        return []
    equity = Decimal("0")
    peak = Decimal("0")
    peak_date: date | None = None
    dd_start: date | None = None
    triggers: list[ForensicsTrigger] = []
    fired_for_peak: bool = False
    for a in aars:
        equity += a.pnl_net
        today = a.exit_ts.astimezone(UTC).date()
        if equity > peak:
            peak = equity
            peak_date = today
            dd_start = None
            fired_for_peak = False
            continue
        if dd_start is None and equity < peak:
            dd_start = today
        if dd_start is None or peak <= 0 or fired_for_peak:
            continue
        days_in_dd = (today - dd_start).days
        dd_pct = (peak - equity) / peak
        if days_in_dd >= DRAWDOWN_DAYS_THRESHOLD and dd_pct >= DRAWDOWN_PCT_THRESHOLD:
            fp = f"{a.engine}|dd|{peak_date}|{dd_start}"
            triggers.append(
                ForensicsTrigger(
                    trigger_kind=TriggerKind.DRAWDOWN_PERIOD,
                    engine=a.engine,
                    fingerprint=fp,
                    payload={
                        "engine": a.engine,
                        "peak_equity": str(peak),
                        "peak_date": peak_date.isoformat() if peak_date else None,
                        "trough_equity": str(equity),
                        "drawdown_pct": f"{float(dd_pct):.4f}",
                        "days_in_drawdown": days_in_dd,
                        "fingerprint": fp,
                    },
                )
            )
            fired_for_peak = True
    return triggers


# ────────────────────────────────────────────────────────────────────────
# Service — DB I/O wrapping the pure detectors
# ────────────────────────────────────────────────────────────────────────


_EXISTS_SQL = """
    SELECT 1
    FROM platform.forensics_triggers
    WHERE trigger_kind = $1
      AND payload->>'fingerprint' = $2
    LIMIT 1
"""

_INSERT_SQL = """
    INSERT INTO platform.forensics_triggers (trigger_kind, payload, fired_at)
    VALUES ($1, $2::jsonb, $3)
    RETURNING id
"""


class ForensicsService:
    """Run all detectors against ``platform.aar_events`` and persist new triggers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._reader = AARReader(pool)

    async def fetch_aars(self) -> dict[str, list[AARRow]]:
        """Group AARs by engine, sorted by exit_ts within each engine."""
        return await self._reader.fetch_all_grouped()

    @staticmethod
    def detect_all(aars: list[AARRow]) -> list[ForensicsTrigger]:
        return [
            *detect_outlier_losses(aars),
            *detect_loss_cluster(aars),
            *detect_drawdown_period(aars),
        ]

    async def persist_trigger(self, trigger: ForensicsTrigger) -> int | None:
        """INSERT a trigger, skipping if its fingerprint already fired.

        On insert, also writes a Sprint Dossier markdown file under
        ``docs/sprints/`` and records the path in the trigger's payload
        (``dossier_path``). The dossier write is best-effort: filesystem
        failure is logged but doesn't roll back the DB row.
        """
        now = datetime.now(UTC)
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                _EXISTS_SQL, trigger.trigger_kind.value, trigger.fingerprint
            )
            if exists:
                return None
            trigger_id = await conn.fetchval(
                _INSERT_SQL,
                trigger.trigger_kind.value,
                json.dumps(trigger.payload, default=str),
                now,
            )
        # Best-effort dossier write — failure here doesn't unwind the row.
        try:
            from tpcore.forensics.dossier import write_dossier

            path = write_dossier(trigger=trigger, trigger_id=int(trigger_id), fired_at=now)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE platform.forensics_triggers
                       SET payload = payload || jsonb_build_object('dossier_path', $1::text)
                     WHERE id = $2
                    """,
                    str(path),
                    int(trigger_id),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "forensics.dossier_write_failed",
                trigger_id=int(trigger_id) if trigger_id else None,
                error=str(exc),
            )
        return int(trigger_id)

    async def run(self) -> dict[str, int]:
        """Detect across all engines, persist new triggers, return counts.

        Counts dict is ``{trigger_kind: new_rows_inserted}`` — re-running
        shortly after will return zeroes (idempotent via fingerprint).

        Error handling: each engine and each trigger persist is isolated.
        A single bad engine (malformed AAR blob) or a single failed
        INSERT (DB connection blip) doesn't stop the rest of the run —
        the failure is logged and the loop continues. The service never
        raises; if the initial AAR fetch itself fails, we log and return
        zero-counts so the data-operations pipeline isn't blocked by a
        diagnostic step.
        """
        counts: dict[str, int] = {k.value: 0 for k in TriggerKind}
        try:
            by_engine = await self.fetch_aars()
        except Exception as exc:  # noqa: BLE001
            logger.warning("forensics.fetch_failed", error=str(exc))
            return counts

        for engine, aars in by_engine.items():
            try:
                triggers = self.detect_all(aars)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "forensics.detect_failed",
                    engine=engine,
                    aar_count=len(aars),
                    error=str(exc),
                )
                continue

            for trigger in triggers:
                try:
                    inserted = await self.persist_trigger(trigger)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "forensics.persist_failed",
                        engine=engine,
                        trigger_kind=trigger.trigger_kind.value,
                        fingerprint=trigger.fingerprint,
                        error=str(exc),
                    )
                    continue
                if inserted is not None:
                    counts[trigger.trigger_kind.value] += 1
                    logger.info(
                        "forensics.trigger_fired",
                        engine=engine,
                        trigger_kind=trigger.trigger_kind.value,
                        fingerprint=trigger.fingerprint,
                    )
        return counts


__all__ = [
    "DRAWDOWN_DAYS_THRESHOLD",
    "DRAWDOWN_PCT_THRESHOLD",
    "ForensicsService",
    "ForensicsTrigger",
    "LOSS_CLUSTER_K",
    "MIN_AARS_FOR_OUTLIER",
    "OUTLIER_SIGMA",
    "TriggerKind",
    "detect_drawdown_period",
    "detect_loss_cluster",
    "detect_outlier_losses",
]
