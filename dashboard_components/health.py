"""Streamlit-free classifier helpers for the dashboard's platform-health panel.

Live in their own module so unit tests can import them without pulling in
``streamlit`` (which is in the ``dashboard`` optional dep group, not the
``dev`` group CI installs). Pure functions: input is a primitive value,
output is a ``(color, text)`` tuple plus optional detail rows. All severity
decisions live here so they're testable.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

# Stages emitted by ``scripts/ops.py --update``. Kept in sync with the
# orchestrator order so the dashboard knows whether the latest run was
# complete (every stage present) or partial. If a stage is added/removed
# in ``cmd_update``, update this tuple in lockstep.
OPS_UPDATE_STAGES: tuple[str, ...] = (
    "daily_bars",
    "corporate_actions",
    "coverage_fill",
    "fundamentals_refresh",
    "data_validation",
    "universe_prescreener",
    "universe_simulation",
)


def _age_seconds(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


def update_required_banner(
    latest_bar_date: date | None,
    now_utc: datetime | None = None,
    *,
    publication_grace_hours: float = 2.0,
) -> tuple[str, str] | None:
    """Decide whether the operator needs to run a daily update right now.

    Returns ``(severity, message)`` or ``None`` (hide the banner). Severity
    is ``"warn"`` (yellow — most recent session is closed but FMP may still
    be publishing) or ``"required"`` (red — session closed long enough ago
    that bars should be present).

    Time-zone behaviour: everything is computed in UTC. The most-recent
    closed NYSE session is the source of truth — whether the operator is
    in Manila or NYC doesn't matter. The message references "ET" since
    that's the exchange's clock, not the operator's.

    Examples
    --------
    * Manila 09:00 (UTC 01:00) Mon = ET 21:00 Sun → session closed ~5h ago,
      bars from previous Friday → ``"required"``.
    * NYC 17:00 ET = UTC 21:00 → just closed → within grace → ``"warn"``.
    * NYC 09:00 ET on a session day, bars from the prior session → no banner
      (today's session hasn't closed yet, so bars-up-to-yesterday is correct).
    """
    if latest_bar_date is None:
        return ("required", "No bars in prices_daily — run daily update before trading.")
    now_utc = now_utc or datetime.now(UTC)

    # Find the most recent NYSE close at or before now. Lazy import keeps
    # this module dependency-light when classifiers are used in isolation.
    from tpcore.calendar import previous_close

    last_close_utc = previous_close(now_utc)
    last_session_date = last_close_utc.date()

    # If our bars cover the most recently closed session, we're current.
    # ``latest_bar_date`` represents the trading date of the bar — and that
    # date equals ``last_session_date`` once the bars for the just-closed
    # session have been ingested.
    if latest_bar_date >= last_session_date:
        return None

    hours_since_close = (now_utc - last_close_utc).total_seconds() / 3600.0
    behind_days = (last_session_date - latest_bar_date).days
    if hours_since_close < publication_grace_hours:
        return (
            "warn",
            f"Session closed {hours_since_close:.1f}h ago (ET); bars from "
            f"{latest_bar_date.isoformat()}. Today's bars may not be "
            "published yet — wait or run the daily update.",
        )
    return (
        "required",
        f"Most recent NYSE close was {hours_since_close:.1f}h ago "
        f"({last_session_date.isoformat()}); bars are from "
        f"{latest_bar_date.isoformat()} ({behind_days}d behind). "
        "**Update required before trading.**",
    )


def classify_bars(latest_date: date | None) -> tuple[str, str]:
    """Bars freshness: green if within 1 trading day; amber 2-3; red ≥4."""
    if latest_date is None:
        return "red", "No bars in prices_daily"
    age = (datetime.now(UTC).date() - latest_date).days
    if age <= 1:
        color = "green"
    elif age <= 3:
        color = "amber"
    else:
        color = "red"
    return color, f"Latest bar: {latest_date.isoformat()} ({age}d ago)"


def classify_fundamentals(latest_at: datetime | None) -> tuple[str, str]:
    """Fundamentals refresh: green ≤8d, amber ≤14d, red >14d. The Sunday
    cron is the source; one missed Sunday is amber, two is red."""
    if latest_at is None:
        return "red", "No fundamentals rows"
    secs = _age_seconds(latest_at)
    if secs is None:
        return "red", "Unknown age"
    days = secs / 86400
    if days <= 8:
        color = "green"
    elif days <= 14:
        color = "amber"
    else:
        color = "red"
    return color, f"Last refresh: {days:.1f}d ago"


def classify_corp_actions(latest_at: datetime | None) -> tuple[str, str]:
    """Corporate actions: green ≤2d, amber ≤7d, red >7d. Splits/dividends
    are infrequent so we tolerate a multi-day gap before alarming."""
    if latest_at is None:
        return "amber", "No corporate actions ingested"
    secs = _age_seconds(latest_at)
    if secs is None:
        return "amber", "Unknown age"
    days = secs / 86400
    if days <= 2:
        color = "green"
    elif days <= 7:
        color = "amber"
    else:
        color = "red"
    return color, f"Latest ingest: {days:.1f}d ago"


def classify_universe(latest_date: date | None, today_count: int) -> tuple[str, str]:
    """Universe pre-screener: green if today's row count is healthy; red
    otherwise. 'Healthy' is heuristic — momentum universe is normally
    1,000-1,500 names; <500 implies the prescreener failed or the
    liquidity_tiers table is degraded."""
    today = datetime.now(UTC).date()
    if latest_date is None:
        return "red", "Never populated"
    if latest_date < today:
        age = (today - latest_date).days
        return "amber", f"Stale: latest as_of {latest_date.isoformat()} ({age}d ago)"
    if today_count < 500:
        return "red", f"Today: only {today_count} candidates (<500)"
    return "green", f"Today: {today_count} candidates"


def classify_update_run(
    update_run: dict[str, Any],
) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Last ops --update run: green if every stage completed; amber if
    any stage missing (run incomplete); red if any stage FAILED."""
    stages = update_run.get("stages") or {}
    if not stages:
        return "red", "No recent run found", []

    rows: list[tuple[str, str, str]] = []
    n_failed = 0
    n_complete = 0
    for stage_name in OPS_UPDATE_STAGES:
        entry = stages.get(stage_name)
        if entry is None:
            rows.append((stage_name, "amber", "not in latest run"))
            continue
        if entry["event_type"] == "INGESTION_FAILED":
            n_failed += 1
            data = entry["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            reason = (
                (data.get("reason") if isinstance(data, dict) else None)
                or (data.get("error") if isinstance(data, dict) else None)
                or "failed"
            )
            rows.append((stage_name, "red", f"FAILED — {reason}"))
        else:
            n_complete += 1
            rows.append((stage_name, "green", "OK"))

    started_at = update_run.get("started_at")
    started_str = ""
    if started_at is not None:
        secs = _age_seconds(started_at)
        if secs is not None:
            hours = secs / 3600
            if hours < 1:
                started_str = f"{int(secs / 60)}m ago"
            elif hours < 48:
                started_str = f"{hours:.1f}h ago"
            else:
                started_str = f"{hours / 24:.1f}d ago"

    if n_failed > 0:
        color = "red"
        summary = (
            f"Last run {started_str} — {n_failed} stage(s) FAILED, "
            f"{n_complete}/{len(OPS_UPDATE_STAGES)} OK"
        )
    elif n_complete < len(OPS_UPDATE_STAGES):
        color = "amber"
        missing = len(OPS_UPDATE_STAGES) - n_complete
        summary = (
            f"Last run {started_str} — {n_complete}/{len(OPS_UPDATE_STAGES)} OK "
            f"({missing} missing)"
        )
    else:
        color = "green"
        summary = (
            f"Last run {started_str} — {len(OPS_UPDATE_STAGES)}/{len(OPS_UPDATE_STAGES)} stages OK"
        )
    return color, summary, rows


def classify_coverage_gaps(
    bar_gap_count: int,
    fund_gap_count: int,
    tier_le_2_total: int,
) -> tuple[str, str]:
    """Universe coverage integrity — fraction of tier ≤ 2 tickers that
    are missing recent bars and/or any fundamentals row at all.

    Severity:
      * green   — both gaps under 2% of the tier-≤-2 universe
      * amber   — either gap between 2% and 5%
      * red     — either gap above 5%

    The thresholds tolerate the SPAC unit + IPO long-tail (small,
    expected) without masking a structural ingestion failure (large).
    """
    if tier_le_2_total <= 0:
        return "amber", "No tier ≤ 2 universe to measure against"
    bar_pct = bar_gap_count / tier_le_2_total
    fund_pct = fund_gap_count / tier_le_2_total
    worst_pct = max(bar_pct, fund_pct)
    if worst_pct < 0.02:
        color = "green"
    elif worst_pct < 0.05:
        color = "amber"
    else:
        color = "red"
    summary = (
        f"Bars: {bar_gap_count}/{tier_le_2_total} missing recent ({bar_pct:.1%}) · "
        f"Fundamentals: {fund_gap_count}/{tier_le_2_total} missing rows ({fund_pct:.1%})"
    )
    return color, summary


def classify_open_orders(
    pending_count: int,
    stale_24h_count: int,
) -> tuple[str, str]:
    """Open-order liveness check.

    Orphan ``pending`` rows older than 24h indicate the engine's view of
    its own state has diverged from the broker — exactly the failure
    class that produces "cannot open a short sell while a long buy order
    is open" stateful crashes. Any stale row is amber; multiples are red.
    """
    if pending_count == 0:
        return "green", "No pending open orders"
    if stale_24h_count == 0:
        return "green", f"{pending_count} pending (all <24h old)"
    if stale_24h_count == 1:
        return "amber", f"{pending_count} pending — 1 older than 24h"
    return "red", f"{pending_count} pending — {stale_24h_count} older than 24h"


def classify_validation(
    rows: list[dict[str, Any]],
) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Data validation suite — latest run per source.

    Each row carries the most recent ``data_quality_log`` entry for its
    source, plus the boolean flags from that row. Green when latest is
    clean; red when latest failed. No 7-day aggregate — stale aggregates
    lie. Each source is a row in the detail table."""
    if not rows:
        return "amber", "No validation runs recorded", []

    detail: list[tuple[str, str, str]] = []
    worst = "green"
    for r in rows:
        source = r["source"].replace("validation.", "")
        failed = bool(r.get("stale")) or (
            r.get("confidence") is not None and r["confidence"] < 1.0
        )
        conf = r.get("confidence")
        if not failed:
            color = "green"
            text = "latest run: passed"
        else:
            color = "red"
            text = (
                f"latest run: FAILED (confidence "
                f"{conf:.0%})" if conf is not None else "latest run: FAILED"
            )
            worst = "red"
        detail.append((source, color, text))

    if worst == "green":
        summary = f"Latest run: all {len(rows)} check(s) passed"
    else:
        n_failed = sum(1 for _, c, _ in detail if c == "red")
        summary = f"Latest run: {n_failed}/{len(rows)} check(s) FAILED — see detail"
    return worst, summary, detail
