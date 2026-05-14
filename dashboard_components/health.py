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
    "reconcile",
    "coverage_fill",
    "cross_ref_cleanup",
    "fundamentals_refresh",
    "tier_refresh",
    "classify_tickers",
    "catalyst_refresh",
    "sec_filings",
    "macro_indicators",
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
    tier_le_2_non_etf_count: int | None = None,
) -> tuple[str, str]:
    """Universe coverage integrity — fraction missing bars / fundamentals.

    Denominators are SPLIT because the two gaps mean different things:

    * **Bars**: denominator = every T1+T2 ticker (ETFs are expected to
      have bars too). Tight band: green < 2%, amber 2-5%, red > 5%.

    * **Fundamentals**: denominator = T1+T2 **stocks only** (excluding
      ETFs). ETFs legitimately lack ``fundamentals_quarterly`` rows
      because FMP doesn't cover them, so counting them as "missing"
      produced a permanent false-red. With the ETF correction the
      thresholds become tight again: green < 5%, amber 5-15%, red >
      15%.

    ``tier_le_2_non_etf_count`` is the new arg (2026-05-14, post
    ``platform.ticker_classifications`` rollout). When None (legacy
    callers / tests pre-rollout) the function falls back to the loose
    threshold against the full denominator. Once every caller passes
    the new count, the fallback can be removed.
    """
    if tier_le_2_total <= 0:
        return "amber", "No tier ≤ 2 universe to measure against"
    bar_pct = bar_gap_count / tier_le_2_total

    # Bars: tight thresholds against the full T1+T2 denominator.
    if bar_pct < 0.02:
        bar_color = "green"
    elif bar_pct < 0.05:
        bar_color = "amber"
    else:
        bar_color = "red"

    # Fundamentals: denominator is non-ETF count when available.
    if tier_le_2_non_etf_count is not None and tier_le_2_non_etf_count > 0:
        fund_denom = tier_le_2_non_etf_count
        fund_pct = fund_gap_count / fund_denom
        if fund_pct < 0.05:
            fund_color = "green"
        elif fund_pct < 0.15:
            fund_color = "amber"
        else:
            fund_color = "red"
        fund_label = f"{fund_gap_count}/{fund_denom} stocks"
    else:
        # Legacy fallback — denominator is everything including ETFs.
        fund_denom = tier_le_2_total
        fund_pct = fund_gap_count / fund_denom
        if fund_pct < 0.80:
            fund_color = "green"
        elif fund_pct < 0.95:
            fund_color = "amber"
        else:
            fund_color = "red"
        fund_label = f"{fund_gap_count}/{fund_denom} (incl. ETFs)"

    rank = {"green": 0, "amber": 1, "red": 2}
    color = bar_color if rank[bar_color] >= rank[fund_color] else fund_color

    summary = (
        f"Bars: {bar_gap_count}/{tier_le_2_total} missing recent ({bar_pct:.1%}) · "
        f"Fundamentals: {fund_label} missing ({fund_pct:.1%})"
    )
    return color, summary


def classify_daemons(daemons: list[dict[str, Any]]) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Roll-up of the three platform daemons (trade_monitor, data_operations,
    allocator). Each daemon entry:

      {'name': str, 'installed': bool, 'last_run_at': datetime | None,
       'last_exit': int | None, 'kind': 'persistent' | 'scheduled',
       'next_run_hint': str | None}

    Severity:
      red   — any required daemon NOT installed
      amber — a scheduled daemon's last_exit was non-zero, OR persistent
              daemon hasn't logged in 24h
      green — all installed + last runs healthy
    """
    if not daemons:
        return "red", "No daemons configured", []
    detail: list[tuple[str, str, str]] = []
    worst = "green"
    not_installed = 0
    for d in daemons:
        name = d["name"]
        if not d.get("installed"):
            color = "red"
            text = "not installed — run scripts/install_all_daemons.sh"
            worst = "red"
            not_installed += 1
        elif d.get("kind") == "persistent":
            # trade_monitor — log file mtime is the heartbeat
            age = d.get("last_log_age_sec")
            if age is None:
                color, text = "amber", "installed; no log activity yet"
                worst = "amber" if worst == "green" else worst
            elif age > 24 * 3600:
                color, text = "amber", f"log silent for {age/3600:.0f}h"
                worst = "amber" if worst == "green" else worst
            else:
                color, text = "green", f"running; log active {age/60:.0f}m ago"
        else:
            # Scheduled daemon — surface last exit + next-run hint
            exit_code = d.get("last_exit")
            last_run = d.get("last_run_at")
            hint = d.get("next_run_hint") or ""
            if last_run is None:
                color, text = "amber", f"installed, never run yet — next: {hint}"
                worst = "amber" if worst == "green" else worst
            elif exit_code != 0:
                color, text = "amber", f"last exit {exit_code}; next: {hint}"
                worst = "amber" if worst == "green" else worst
            else:
                color, text = "green", f"OK last run; next: {hint}"
        detail.append((name, color, text))
    if not_installed:
        summary = f"{not_installed}/{len(daemons)} daemon(s) not installed"
    elif worst == "amber":
        summary = "Daemons installed; one or more need attention"
    else:
        summary = f"All {len(daemons)} daemons installed and healthy"
    return worst, summary, detail


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


def classify_catalyst(state: dict[str, Any]) -> tuple[str, str]:
    """Catalyst-events coverage + freshness row.

    Mirrors ``validation.catalyst_events_freshness``: green when newest
    event ≤ 90d AND ≥ 20% of T1+T2 stocks have an event in last 180d;
    amber when 10-20% coverage; red when stale or below 10%.

    The dashboard row and the validation check use the same predicates
    so the two never disagree on what's healthy.
    """
    addressable = int(state.get("addressable") or 0)
    covered = int(state.get("covered") or 0)
    newest_event = state.get("newest_event")
    total = int(state.get("total_rows") or 0)
    last_refresh = state.get("last_refresh")

    if total == 0:
        return "red", "catalyst_events is empty"

    # Age check.
    today = datetime.now(UTC).date()
    if newest_event is None:
        return "red", f"{total} rows but no event_date"
    age_days = (today - newest_event).days

    refresh_age_d = (
        (datetime.now(UTC) - last_refresh).days if last_refresh else None
    )

    # Coverage check.
    if addressable == 0:
        # No stocks to measure against — neutral; freshness alone decides.
        coverage_pct = None
        coverage_color = "green"
    else:
        coverage_pct = covered / addressable
        if coverage_pct < 0.10:
            coverage_color = "red"
        elif coverage_pct < 0.20:
            coverage_color = "amber"
        else:
            coverage_color = "green"

    # Freshness check (newest event date).
    if age_days > 180:
        freshness_color = "red"
    elif age_days > 90:
        freshness_color = "amber"
    else:
        freshness_color = "green"

    rank = {"green": 0, "amber": 1, "red": 2}
    color = freshness_color if rank[freshness_color] >= rank[coverage_color] else coverage_color

    pct_str = f"{coverage_pct:.1%}" if coverage_pct is not None else "n/a"
    refresh_str = f"refreshed {refresh_age_d}d ago" if refresh_age_d is not None else "refresh time unknown"
    summary = (
        f"{covered}/{addressable} T1+T2 stocks ({pct_str}) covered in last 180d · "
        f"newest event {age_days}d ago · "
        f"{refresh_str}"
    )
    return color, summary


def classify_forensics(state: dict[str, Any]) -> tuple[str, str]:
    """Forensics open-triggers roll-up.

    Forensics writes a trigger row whenever an AAR pattern (drawdown,
    loss cluster, outlier loss) needs an operator-driven Sprint Dossier.
    ``state`` is the dict produced by ``_q_forensics``.

    Severity (no triggers is the happy state — most days):

    * **green**  — no open triggers.
    * **amber**  — at least one open trigger, oldest ≤ 7 days. Operator
      should review and resolve via a Sprint Dossier.
    * **red**    — at least one open trigger older than 14 days. Stale
      triggers mean an operator hasn't followed up on a real warning.
    """
    by_kind = state.get("by_kind") or []
    if not by_kind:
        return "green", "No open triggers"
    total = sum(int(r["open_count"]) for r in by_kind)
    oldest_at = min((r["oldest_open_at"] for r in by_kind if r["oldest_open_at"]), default=None)
    if oldest_at is None:
        return "amber", f"{total} open trigger(s)"
    now = datetime.now(UTC)
    if oldest_at.tzinfo is None:
        oldest_at = oldest_at.replace(tzinfo=UTC)
    age_days = (now - oldest_at).days
    summary_kinds = ", ".join(f"{r['kind']}={r['open_count']}" for r in by_kind)
    if age_days >= 14:
        return "red", f"{total} open ({summary_kinds}) — oldest {age_days}d unresolved"
    return "amber", f"{total} open ({summary_kinds}) — oldest {age_days}d"


def classify_cross_ref(
    findings: list[dict[str, Any]],
) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Cross-table integrity roll-up.

    ``findings`` is a list of ``{check, table, count}`` dicts produced by
    the ``_q_cross_ref`` async fetch. Any count > 0 is red; the per-row
    detail makes each violation actionable.
    """
    if not findings:
        return "green", "Every cross-reference check clean", []
    detail: list[tuple[str, str, str]] = []
    worst = "green"
    n_red = 0
    for f in findings:
        n = int(f["count"])
        label = f"{f['table']}.{f['check']}"
        if n == 0:
            detail.append((label, "green", "0"))
        else:
            detail.append((label, "red", f"{n:,} rows"))
            worst = "red"
            n_red += 1
    if worst == "green":
        summary = f"All {len(findings)} cross-reference checks clean"
    else:
        summary = f"{n_red}/{len(findings)} cross-reference checks FAILED — see detail"
    return worst, summary, detail


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
