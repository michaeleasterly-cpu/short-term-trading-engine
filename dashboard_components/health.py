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


def classify_validation(
    rows: list[dict[str, Any]],
) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Data validation suite: green if zero failed rows in last 7 days,
    amber if any source had ≤2 failed runs, red if any source had ≥3.
    Each source is a row in the detail table."""
    if not rows:
        return "amber", "No validation runs in last 7 days", []

    detail: list[tuple[str, str, str]] = []
    worst = "green"
    for r in rows:
        n_failed = int(r["n_failed"])
        n_runs = int(r["n_runs"])
        source = r["source"].replace("validation.", "")
        if n_failed == 0:
            color = "green"
            text = f"{n_runs} runs, all passed"
        elif n_failed <= 2:
            color = "amber"
            text = f"{n_failed}/{n_runs} runs failed"
            worst = "amber" if worst == "green" else worst
        else:
            color = "red"
            text = f"{n_failed}/{n_runs} runs failed"
            worst = "red"
        detail.append((source, color, text))

    if worst == "green":
        summary = f"All {len(rows)} validation source(s) clean (last 7d)"
    elif worst == "amber":
        summary = "Some validation failures (last 7d) — see detail"
    else:
        summary = "Repeated validation failures (last 7d) — see detail"
    return worst, summary, detail
