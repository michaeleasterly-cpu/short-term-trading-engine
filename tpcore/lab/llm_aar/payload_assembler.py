"""Assemble the bounded AAR payload the LLM-AAR critic sees — spec §2.2.

Deterministic input — reads ``platform.aar_events`` via the existing
``tpcore.aar.AARReader`` + augments with per-engine aggregations the
critic LLM consumes. The LLM never sees raw DB rows; only the bounded
``EnginePerformanceWindow`` shape.

Engine-FREE: stdlib + structlog + tpcore.aar + tpcore.engine_profile.

The assembler is pure-Python except for the asyncpg pool dependency
(needed to fetch jsonb fields the AARReader's lightweight AARRow doesn't
expose: confidence_at_entry / slippage_bps / rule_compliance).

Per spec §2.3, the assembler fails loud on payload-byte overflow (cap
``MAX_AAR_PAYLOAD_BYTES = 256 KiB``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import structlog

from tpcore.lab.llm_aar import (
    AAR_CRITIC_WINDOW_SESSIONS,
    MAX_AAR_PAYLOAD_BYTES,
)
from tpcore.lab.llm_aar.models import (
    AARRowSummary,
    EnginePerformanceWindow,
    HoldBucket,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)


# SQL: pull the full aar_data jsonb (the AARReader only surfaces the
# lightweight AARRow slice; we need confidence_at_entry, slippage_bps,
# rule_compliance, exit_ts, entry_ts, exit_reason from the jsonb).
_SELECT_AAR_DATA_SQL = """
    SELECT engine, trade_id, ticker, aar_data, recorded_at
    FROM platform.aar_events
    ORDER BY engine, recorded_at ASC
"""


# Engines excluded from critic emission per persona §2 + spec §2.1.
EXCLUDED_ENGINES: frozenset[str] = frozenset({"canary"})
"""canary is the heartbeat engine (non-graduating); no AAR findings emitted."""


def _bucket_hold_sessions(hold_sessions: int | None) -> HoldBucket:
    """Map hold-session count to one of the 5 buckets (spec §2.2)."""
    if hold_sessions is None:
        # Treat unknown as the shortest bucket; conservative default.
        return "0-1d"
    if hold_sessions <= 1:
        return "0-1d"
    if hold_sessions <= 3:
        return "1-3d"
    if hold_sessions <= 7:
        return "3-7d"
    if hold_sessions <= 21:
        return "7-21d"
    return "21d+"


def _parse_ts(raw: object) -> datetime | None:
    """Tolerant ISO-8601 parse — handles ``Z`` suffix and naive strings.

    Duplicated from tpcore.aar.reader._parse_ts to avoid the private
    import per docs/STYLE_GUIDE.md.
    """
    from datetime import UTC
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


def _to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    """Tolerant Decimal cast — None / bad strings → default."""
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return default


def _percentile(values: list[float], pct: float) -> float | None:
    """Compute a simple linear-interp percentile.

    Pure-Python — no numpy dependency in this layer. Sorts in-place.
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = pct * (len(s) - 1)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    d = k - f
    return s[f] + d * (s[c] - s[f])


async def assemble_aar_payload(
    pool: asyncpg.Pool,
    *,
    as_of_session: date,
    window_sessions: int = AAR_CRITIC_WINDOW_SESSIONS,
) -> tuple[EnginePerformanceWindow, ...]:
    """Build the bounded per-engine AAR payload for the LLM-AAR critic.

    Args:
        pool: asyncpg connection pool.
        as_of_session: anchor session date; the 90-session window runs
            backwards from here.
        window_sessions: window length in calendar days (NOT trading days;
            simpler bound — 90 calendar days covers ~63 trading sessions
            which is sufficient for pattern recognition).

    Returns:
        Tuple of EnginePerformanceWindow, one per non-excluded engine
        that has at least one AAR. Sorted by engine name for determinism.

    Raises:
        ValueError: if the assembled payload exceeds MAX_AAR_PAYLOAD_BYTES.
            Fail-loud per spec §2.3 — bounded payload is a structural
            invariant, not a soft target.
    """
    window_cutoff = as_of_session - timedelta(days=window_sessions)

    by_engine: dict[str, list[dict[str, object]]] = defaultdict(list)

    async with pool.acquire() as conn:
        records = await conn.fetch(_SELECT_AAR_DATA_SQL)

    for r in records:
        engine = r["engine"]
        if engine in EXCLUDED_ENGINES:
            continue
        aar_data = r["aar_data"]
        if isinstance(aar_data, str):
            try:
                aar_data = json.loads(aar_data)
            except (ValueError, TypeError):
                continue
        if not isinstance(aar_data, dict):
            continue
        # Augment the AAR dict with metadata we'll need downstream.
        aar_data["_recorded_at"] = r["recorded_at"]
        aar_data["_ticker"] = r["ticker"]
        by_engine[engine].append(aar_data)

    windows: list[EnginePerformanceWindow] = []
    for engine in sorted(by_engine):
        aars = by_engine[engine]
        if not aars:
            continue
        windows.append(
            _build_window(
                engine=engine,
                aars=aars,
                as_of_session=as_of_session,
                window_cutoff=window_cutoff,
                window_sessions=window_sessions,
            )
        )

    # Fail-loud on payload-byte overflow.
    payload = tuple(windows)
    serialised = json.dumps(
        [w.model_dump(mode="json") for w in payload], default=str
    )
    payload_bytes = len(serialised.encode("utf-8"))
    if payload_bytes > MAX_AAR_PAYLOAD_BYTES:
        raise ValueError(
            f"assembled AAR payload {payload_bytes} bytes exceeds cap "
            f"{MAX_AAR_PAYLOAD_BYTES} (spec §2.3). engines={len(payload)}; "
            f"consider tightening the window or recent_aars cap."
        )
    log.info(
        "aar_critic.payload.assembled",
        engines=len(payload),
        payload_bytes=payload_bytes,
        as_of_session=str(as_of_session),
    )
    return payload


def _build_window(
    *,
    engine: str,
    aars: list[dict[str, object]],
    as_of_session: date,
    window_cutoff: date,
    window_sessions: int,
) -> EnginePerformanceWindow:
    """Build one EnginePerformanceWindow from the raw AAR jsonb list."""
    pnl_total = Decimal("0")
    win_total = 0
    pnl_window = Decimal("0")
    win_window = 0
    trades_window = 0

    exit_reason_count: dict[str, int] = defaultdict(int)
    exit_reason_pnl: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    hold_count: dict[HoldBucket, int] = defaultdict(int)
    hold_pnl: dict[HoldBucket, Decimal] = defaultdict(lambda: Decimal("0"))
    slippage_values: list[float] = []
    rule_compliance_count = 0
    rule_compliance_total = 0

    recent_for_summary: list[tuple[datetime, dict[str, object]]] = []

    for aar in aars:
        pnl_net = _to_decimal(aar.get("pnl_net"))
        exit_ts = _parse_ts(aar.get("exit_ts"))
        entry_ts = _parse_ts(aar.get("entry_ts"))
        exit_reason_value = str(aar.get("exit_reason", "other"))

        pnl_total += pnl_net
        if pnl_net > 0:
            win_total += 1

        in_window = exit_ts is not None and exit_ts.date() >= window_cutoff
        if in_window:
            trades_window += 1
            pnl_window += pnl_net
            if pnl_net > 0:
                win_window += 1
            exit_reason_count[exit_reason_value] += 1
            exit_reason_pnl[exit_reason_value] += pnl_net

            hold_sessions: int | None = None
            if entry_ts is not None and exit_ts is not None:
                hold_sessions = max(0, (exit_ts.date() - entry_ts.date()).days)
            bucket = _bucket_hold_sessions(hold_sessions)
            hold_count[bucket] += 1
            hold_pnl[bucket] += pnl_net

            slippage_raw = aar.get("slippage_bps")
            if slippage_raw is not None:
                try:
                    slippage_values.append(float(str(slippage_raw)))
                except (ValueError, TypeError):
                    pass

            rule_total_value = aar.get("rule_compliance")
            if rule_total_value is not None:
                rule_compliance_total += 1
                if rule_total_value:
                    rule_compliance_count += 1

            if exit_ts is not None:
                recent_for_summary.append((exit_ts, aar))

    # Recent AAR summaries (last 20 by exit_ts).
    recent_for_summary.sort(key=lambda kv: kv[0], reverse=True)
    recent_for_summary = recent_for_summary[:20]
    recent_summaries: list[AARRowSummary] = []
    for exit_ts, aar in recent_for_summary:
        entry_ts_p = _parse_ts(aar.get("entry_ts"))
        hold = (
            (exit_ts.date() - entry_ts_p.date()).days
            if entry_ts_p is not None else None
        )
        recent_summaries.append(
            AARRowSummary(
                ticker=str(aar.get("_ticker", "?")),
                entry_session=entry_ts_p.date() if entry_ts_p is not None else None,
                exit_session=exit_ts.date(),
                pnl_net_usd=_to_decimal(aar.get("pnl_net")),
                exit_reason=str(aar.get("exit_reason", "other"))[:64],
                hold_sessions=hold,
            )
        )

    trade_count_total = len(aars)
    win_rate_total = (
        win_total / trade_count_total if trade_count_total > 0 else 0.0
    )
    win_rate_window = (
        win_window / trades_window if trades_window > 0 else 0.0
    )
    rule_compliance_rate = (
        rule_compliance_count / rule_compliance_total
        if rule_compliance_total > 0 else 1.0
    )

    slippage_p50 = _percentile(slippage_values, 0.5)
    slippage_p95 = _percentile(slippage_values, 0.95)

    # Ensure all 5 buckets are present (zero-fill missing).
    all_buckets = cast(
        list[HoldBucket],
        ["0-1d", "1-3d", "3-7d", "7-21d", "21d+"],
    )
    hold_count_full = {b: hold_count.get(b, 0) for b in all_buckets}
    hold_pnl_full = {b: hold_pnl.get(b, Decimal("0")) for b in all_buckets}

    return EnginePerformanceWindow(
        engine=engine,
        as_of_session=as_of_session,
        trade_count_total=trade_count_total,
        trade_count_window=trades_window,
        pnl_net_total_usd=pnl_total,
        pnl_net_window_usd=pnl_window,
        win_rate_window=win_rate_window,
        win_rate_total=win_rate_total,
        exit_reason_distribution=dict(exit_reason_count),
        exit_reason_pnl_by_reason_usd=dict(exit_reason_pnl),
        hold_duration_buckets=hold_count_full,
        pnl_per_hold_bucket_usd=hold_pnl_full,
        slippage_bps_p50=slippage_p50,
        slippage_bps_p95=slippage_p95,
        rule_compliance_rate=rule_compliance_rate,
        recent_aars=tuple(recent_summaries),
    )


__all__ = [
    "EXCLUDED_ENGINES",
    "assemble_aar_payload",
]
