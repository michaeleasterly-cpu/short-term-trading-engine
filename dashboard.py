"""Operator dashboard — single-page Streamlit UI for the trading platform.

Replaces the operator's daily pattern of running ~8 separate ``scripts/run_*.sh``
files with one local web UI. Read-mostly view of system state + action buttons
that dispatch the existing scripts.

Phases shipped in this file:
* Phase 1 — Skeleton (header, holdings table, equity curve from EQUITY_SNAPSHOT)
* Phase 2 — Action buttons (5 actions, two subprocess patterns)

Phases deferred to follow-up commits:
* Phase 3 — Per-ticker chart with entry/exit markers (streamlit-lightweight-charts-pro)
* Phase 4 — Credibility scorecards + signals + AARs feeds
* Phase 5 — Auto-refresh polish + keyboard shortcuts

Spec: docs/superpowers/specs/2026-05-13-operator-dashboard.md
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import streamlit as st

# Reuse existing tip-sheet query helpers + broker adapter — same data layer,
# same shape. Dashboard adds zero new business logic.
from dashboard_components.charts import render_ticker_chart
from dashboard_components.defect_register import classify_defect_register
from dashboard_components.escalation import (
    classify_cross_table_audit,
    classify_recent_escalations,
    classify_source_holds,
    classify_undispositioned,
)
from dashboard_components.health import (
    classify_bars,
    classify_catalyst,
    classify_corp_actions,
    classify_coverage_gaps,
    classify_daemons,
    classify_forensics,
    classify_fundamentals,
    classify_open_orders,
    classify_universe,
    classify_update_run,
    classify_validation,
    update_required_banner,
)
from scripts.generate_tip_sheet import (
    fetch_credibility,
    fetch_engine_holdings,
    fetch_recent_signals,
    fetch_recent_trades,
    fetch_today_recommendations,
)
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.backtest.credibility import MIN_LIVE_SCORE
from tpcore.backtest.overfitting import DSR_PASS_THRESHOLD
from tpcore.db import build_asyncpg_pool

# Phase 5 — auto-refresh (opt-in, off by default per spec).
try:
    from streamlit_autorefresh import st_autorefresh

    _AUTOREFRESH_AVAILABLE = True
except ImportError:
    _AUTOREFRESH_AVAILABLE = False


# Which engines get their own credibility scorecard. Order matters — most
# important / closest-to-graduation first.
SCORECARD_ENGINES = ("momentum", "sigma", "reversion", "vector")


REPO_ROOT = Path(__file__).resolve().parent
LOG_DIR = Path("/tmp")  # detached job logs live here


# ────────────────────────────────────────────────────────────────────────────
# Async bridge — Streamlit is sync; spin a fresh event loop per call.
# ────────────────────────────────────────────────────────────────────────────


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not url:
        st.error(
            "DATABASE_URL not set. Run via `scripts/run_dashboard.sh` (which sources .env), "
            "not `streamlit run dashboard.py` directly."
        )
        st.stop()
    return url


# ────────────────────────────────────────────────────────────────────────────
# Data fetchers — thin wrappers around existing helpers + new equity-snapshot reader
# ────────────────────────────────────────────────────────────────────────────


async def _fetch_account_state() -> dict:
    broker = AlpacaPaperBrokerAdapter()
    account = await broker.get_account()
    positions = await broker.get_positions()
    cash = getattr(account, "cash", None)
    return {
        "equity": float(account.equity) if account.equity else 0.0,
        "cash": float(cash) if cash is not None else 0.0,
        "n_positions": len(positions),
        "unrealized_pl": sum(float(p.unrealized_pl) for p in positions if p.unrealized_pl is not None),
        "fetched_at": datetime.now(UTC),
    }


async def _fetch_holdings_for_engine(engine: str) -> list[dict]:
    broker = AlpacaPaperBrokerAdapter()
    return await fetch_engine_holdings(broker, engine)


async def _fetch_ohlc(ticker: str, days: int = 90) -> list[dict]:
    """Read OHLC bars for ``ticker`` from platform.prices_daily."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date, open, high, low, close
                FROM platform.prices_daily
                WHERE ticker = $1 AND date >= CURRENT_DATE - ($2::int * INTERVAL '1 day')
                ORDER BY date ASC
                """,
                ticker,
                days,
            )
    finally:
        await pool.close()
    return [
        {
            "date": r["date"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rows
    ]


async def _fetch_closed_trades_for_ticker(ticker: str, days: int = 365) -> list[dict]:
    """Read closed trades (AARs) for a specific ticker — across all engines.

    The tip-sheet helper :func:`fetch_recent_trades` is engine-scoped; here
    we want any closed trade on the symbol, regardless of which engine
    opened it, so the operator can see the full trading history overlaid
    on the chart."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT aar_data
                FROM platform.aar_events
                WHERE ticker = $1
                  AND recorded_at >= NOW() - ($2::int * INTERVAL '1 day')
                ORDER BY recorded_at DESC
                """,
                ticker,
                days,
            )
    finally:
        await pool.close()
    out: list[dict] = []
    for r in rows:
        data = r["aar_data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        if not isinstance(data, dict):
            continue
        try:
            entry_ts = data.get("entry_ts")
            exit_ts = data.get("exit_ts")
            if entry_ts and exit_ts:
                # Strip time for the chart x-axis (daily candles).
                from datetime import datetime as _dt

                e = _dt.fromisoformat(entry_ts).date() if isinstance(entry_ts, str) else entry_ts
                x = _dt.fromisoformat(exit_ts).date() if isinstance(exit_ts, str) else exit_ts
                qty = float(data.get("qty", 0)) or 1.0
                entry_px = float(data.get("entry_price", 0))
                exit_px = float(data.get("exit_price", 0))
                pnl_pct = (exit_px - entry_px) / entry_px if entry_px else 0.0
                out.append(
                    {
                        "entry_date": e,
                        "entry_price": entry_px,
                        "exit_date": x,
                        "exit_price": exit_px,
                        "pnl_pct": pnl_pct,
                        "qty": qty,
                        "exit_reason": data.get("exit_reason", ""),
                    }
                )
        except Exception:
            continue
    return out


async def _fetch_active_entry_for_ticker(ticker: str) -> dict | None:
    """Find the entry date+price of a currently-held position.

    Cross-references the broker's most recent FILLED BUY order on the
    symbol — that's when the position was opened. Returns None if the
    ticker isn't currently held or the entry order can't be located."""
    broker = AlpacaPaperBrokerAdapter()
    positions = await broker.get_positions()
    pos = next((p for p in positions if p.symbol == ticker), None)
    if pos is None or int(pos.qty) <= 0:
        return None
    orders = await broker.list_recent_orders(limit=500)
    # Newest first; pick most recent FILLED buy on this symbol.
    for o in orders:
        if o.symbol != ticker:
            continue
        status_val = getattr(o.status, "value", str(o.status)).lower()
        side_val = getattr(o.side, "value", str(o.side)).lower()
        if status_val == "filled" and side_val == "buy":
            return {
                "entry_date": o.filled_at.date()
                if o.filled_at
                else o.submitted_at.date()
                if o.submitted_at
                else None,
                "entry_price": float(o.avg_fill_price) if o.avg_fill_price else float(pos.avg_entry_price),
                "qty": int(pos.qty),
            }
    # Fallback: no order history; use today's date with avg_entry_price.
    return {
        "entry_date": datetime.now(UTC).date(),
        "entry_price": float(pos.avg_entry_price) if pos.avg_entry_price else 0.0,
        "qty": int(pos.qty),
    }


async def _fetch_last_run_timestamps() -> dict:
    """Read 'last run' timestamps for every tracked workflow from the DB.

    Operator HCI: every action button shows when it last completed
    successfully, so the operator can see at a glance what's stale. None
    means 'never tracked / never run'."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    out: dict = {}
    try:
        async with pool.acquire() as conn:
            # Daily data update — ops.py emits ops.stage.complete with stage='daily_bars'.
            out["daily_update"] = await conn.fetchval(
                """
                SELECT MAX(recorded_at) FROM platform.application_log
                WHERE engine = 'ops' AND event_type = 'ops.stage.complete'
                """
            )
            # Force-rebalance — momentum scheduler emits SIGNAL events on rebalance days.
            out["force_rebalance"] = await conn.fetchval(
                """
                SELECT MAX(recorded_at) FROM platform.application_log
                WHERE engine = 'momentum' AND event_type = 'SIGNAL'
                """
            )
            # Credibility refresh — data_quality_log row written by search pipeline.
            out["credibility_refresh"] = await conn.fetchval(
                """
                SELECT MAX(timestamp) FROM platform.data_quality_log
                WHERE source LIKE 'backtest_credibility.%'
                """
            )
            # Liquidity-tier refresh — assign_liquidity_tiers.py writes to spread_observations
            # via the Corwin-Schultz pipeline; latest row tells us when last refreshed.
            try:
                out["tier_refresh"] = await conn.fetchval(
                    "SELECT MAX(observed_at) FROM platform.spread_observations"
                )
            except Exception:
                out["tier_refresh"] = None
    finally:
        await pool.close()
    return out


_WEEKDAY_NAMES = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_plist_schedule(plist_path: Path) -> dict | None:
    """Extract ``Hour``, ``Minute``, ``Weekday`` from a launchd plist's
    ``StartCalendarInterval`` block. Returns None on parse failure /
    persistent (no schedule) plists. Persistent agents (KeepAlive=true)
    are caught by the absence of StartCalendarInterval — caller decides
    how to render."""
    try:
        text = plist_path.read_text()
    except OSError:
        return None
    if "StartCalendarInterval" not in text:
        return None
    import re

    block = re.search(
        r"<key>StartCalendarInterval</key>\s*<dict>(.*?)</dict>",
        text,
        re.DOTALL,
    )
    if not block:
        return None
    inner = block.group(1)
    out: dict = {}
    for key in ("Hour", "Minute", "Weekday"):
        m = re.search(rf"<key>{key}</key>\s*<integer>(-?\d+)</integer>", inner)
        if m:
            out[key.lower()] = int(m.group(1))
    return out


def _format_schedule_hint(schedule: dict | None, persistent: bool) -> str:
    """Render a human hint showing local + UTC fire time.

    Reads the plist verbatim (local-time fields) and converts to UTC
    using the host's current offset. Always shows both so an operator
    in Manila can sanity-check 'next 05:30 local = 21:30 UTC' without
    doing math."""
    if persistent:
        return "persistent (KeepAlive)"
    if not schedule or "hour" not in schedule:
        return "schedule not parseable"
    hour = schedule["hour"]
    minute = schedule.get("minute", 0)
    weekday = schedule.get("weekday")  # 1=Mon..7=Sun (launchd convention)
    # Local time the daemon fires (per the plist's StartCalendarInterval).
    now_local = datetime.now().astimezone()
    fire_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    fire_utc = fire_local.astimezone(UTC)
    local_str = f"{hour:02d}:{minute:02d} local"
    utc_str = f"{fire_utc.hour:02d}:{fire_utc.minute:02d} UTC"
    if weekday:
        day = _WEEKDAY_NAMES[weekday] if 1 <= weekday <= 7 else f"weekday={weekday}"
        return f"{day} {local_str} (= {utc_str})"
    return f"daily {local_str} (= {utc_str})"


def _fetch_daemon_state() -> list[dict]:
    """Return [{'name', 'installed', 'kind', 'last_run_at',
    'last_exit', 'last_log_age_sec', 'next_run_hint'}] for each
    platform daemon. Local filesystem + ``launchctl list`` only — no
    DB. Pure sync because it's used outside the asyncio.gather batch.

    ``next_run_hint`` is parsed from the live plist (no hardcoded
    string) so it stays accurate when the install script changes the
    schedule (e.g., removing the Weekday filter from data-operations)."""
    home = Path.home()
    plist_dir = home / "Library" / "LaunchAgents"
    log_dir = home / "Library" / "Logs" / "short-term-trading-engine"
    specs = [
        ("engine_service", "persistent", "engine-service.log"),
        ("data_operations", "scheduled", "data-operations.log"),
        ("allocator", "scheduled", "allocator.log"),
    ]
    out: list[dict] = []
    for name, kind, log_basename in specs:
        plist = plist_dir / f"com.michael.trading.{name.replace('_', '-')}.plist"
        installed = plist.exists()
        schedule = _parse_plist_schedule(plist) if installed else None
        hint = _format_schedule_hint(schedule, persistent=(kind == "persistent"))
        log_path = log_dir / log_basename
        last_log_age = None
        last_exit = None
        last_run = None
        if log_path.exists():
            try:
                stat = log_path.stat()
                last_log_age = max(0.0, time.time() - stat.st_mtime)
                last_run = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            except OSError:
                pass
        # last_exit — best-effort grep for the agent's launchctl status
        if installed:
            try:
                rc = subprocess.run(
                    ["launchctl", "list", f"com.michael.trading.{name.replace('_', '-')}"],
                    capture_output=True, text=True, timeout=5,
                )
                # plutil-ish key=value output; "LastExitStatus" = N;
                for line in rc.stdout.splitlines():
                    if "LastExitStatus" in line:
                        # line shape: "    "LastExitStatus" = N;"
                        parts = line.strip().rstrip(";").split("=")
                        if len(parts) == 2:
                            try:
                                last_exit = int(parts[1].strip())
                            except ValueError:
                                pass
                        break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        out.append({
            "name": name,
            "kind": kind,
            "installed": installed,
            "next_run_hint": hint,
            "last_log_age_sec": last_log_age,
            "last_run_at": last_run,
            "last_exit": last_exit,
        })
    return out


async def _fetch_platform_health() -> dict:
    """Snapshot of the operational-health signals the operator needs before trading.

    Bundles five questions into one DB round-trip:

    1. *Are today's bars present?* — latest date in ``platform.prices_daily``.
    2. *Are fundamentals fresh?* — latest ``recorded_at`` row.
    3. *Are corporate actions current?* — latest ``recorded_at`` row.
    4. *Did the universe pre-screener write today's row?* — count + latest date
       in ``platform.universe_candidates`` for ``engine='momentum'``.
    5. *Did the last ``ops --update`` run finish cleanly?* — per-stage status
       derived from the most recent run's ``INGESTION_COMPLETE`` /
       ``INGESTION_FAILED`` events, plus the count of validation-suite
       failures in ``data_quality_log`` over the last 7 days.

    Returns a dict keyed by panel-row (``bars``, ``fundamentals``, etc.) with
    a ``status_color`` (``green``/``amber``/``red``/``unknown``) and a short
    operator-facing string. The renderer is intentionally dumb — all
    severity decisions live here so they're testable.
    """
    pool = await build_asyncpg_pool(_db_url(), max_size=6)
    out: dict = {}

    # Six independent queries run in parallel — each holds its own pooled
    # connection. Brings first-load from ~12s (serial on one connection)
    # to ~5s (limited by the slowest query, the 4s bounded bars scan).
    async def _q_bars() -> dict:
        async with pool.acquire() as conn:
            # Bound to last 10 days. ``MAX(date)`` over the full 20M-row
            # table takes ~15s — only indexes are on ``(ticker, date)``,
            # not ``(date)`` alone. With the range bound, the planner
            # range-scans cheaply. We lose the ability to detect "bars
            # are 11+ days stale" but that's already deep red.
            r = await conn.fetchrow(
                """
                SELECT MAX(date) AS latest_date,
                       COUNT(DISTINCT ticker) FILTER (
                           WHERE date >= CURRENT_DATE - INTERVAL '5 days'
                       ) AS recent_tickers
                FROM platform.prices_daily
                WHERE date > CURRENT_DATE - INTERVAL '10 days'
                """
            )
            return {
                "latest_date": r["latest_date"] if r else None,
                "recent_tickers": int(r["recent_tickers"]) if r else 0,
            }

    async def _q_fundamentals() -> dict:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT MAX(recorded_at) AS latest_at,
                       MAX(period_end_date) AS latest_period
                FROM platform.fundamentals_quarterly
                """
            )
            return {
                "latest_at": r["latest_at"] if r else None,
                "latest_period": r["latest_period"] if r else None,
            }

    async def _q_corp_actions() -> dict:
        async with pool.acquire() as conn:
            v = await conn.fetchval("SELECT MAX(recorded_at) FROM platform.corporate_actions")
            return {"latest_at": v}

    async def _q_universe() -> dict:
        """Per-engine universe-source health.

        Each engine picks its universe differently — momentum reads from
        ``platform.universe_candidates`` (populated daily by the
        prescreener stage); sigma + reversion derive from
        ``prices_daily`` distinct-ticker freshness implicitly; vector
        reads ``platform.liquidity_tiers`` (refreshed quarterly via the
        Corwin-Schultz tier refresh). Return data for every engine so
        the dashboard can render an honest per-engine row.
        """
        async with pool.acquire() as conn:
            # Momentum — prescreener-populated universe_candidates.
            mom = await conn.fetchrow(
                """
                SELECT MAX(as_of_date) AS latest_date,
                       COUNT(*) FILTER (WHERE as_of_date = CURRENT_DATE) AS today_count
                FROM platform.universe_candidates
                WHERE engine = 'momentum'
                """
            )
            # Vector — liquidity_tiers tier ≤ 2; freshness = newest last_updated.
            lt = await conn.fetchrow(
                """
                SELECT MAX(last_updated) AS newest, COUNT(*) AS n
                FROM platform.liquidity_tiers
                WHERE tier <= 2
                """
            )
            # Sigma / Reversion — implicit universe from prices_daily distinct
            # tickers in the last 90 days. Use the matview's tally so this
            # is cheap; freshness here means "are there enough live tickers
            # for the engines to scan?"
            implicit = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n_active,
                       MAX(latest_date) AS newest_bar
                FROM platform.prices_daily_tickers
                WHERE latest_date >= CURRENT_DATE - INTERVAL '90 days'
                """
            )
            return {
                # Legacy keys for back-compat with the existing
                # classify_universe call site:
                "latest_date": mom["latest_date"] if mom else None,
                "today_count": int(mom["today_count"]) if mom else 0,
                # New per-engine breakdown.
                "momentum": {
                    "source": "platform.universe_candidates",
                    "latest_date": mom["latest_date"] if mom else None,
                    "today_count": int(mom["today_count"]) if mom else 0,
                },
                "vector": {
                    "source": "platform.liquidity_tiers (tier ≤ 2)",
                    "newest_at": lt["newest"] if lt else None,
                    "ticker_count": int(lt["n"]) if lt else 0,
                },
                "sigma_reversion": {
                    "source": "prices_daily (distinct tickers, 90d freshness)",
                    "newest_bar": implicit["newest_bar"] if implicit else None,
                    "ticker_count": int(implicit["n_active"]) if implicit else 0,
                },
            }

    async def _q_update_run() -> dict:
        update_run: dict = {"run_id": None, "started_at": None, "stages": {}}
        async with pool.acquire() as conn:
            last_run = await conn.fetchrow(
                """
                SELECT run_id, MAX(recorded_at) AS started_at
                FROM platform.application_log
                WHERE engine = 'ops' AND event_type = 'STARTUP'
                GROUP BY run_id
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            if not last_run:
                return update_run
            update_run["run_id"] = last_run["run_id"]
            update_run["started_at"] = last_run["started_at"]
            stage_rows = await conn.fetch(
                """
                SELECT data->>'stage' AS stage, event_type, recorded_at, data
                FROM platform.application_log
                WHERE engine = 'ops'
                  AND run_id = $1
                  AND event_type IN ('INGESTION_COMPLETE', 'INGESTION_FAILED')
                ORDER BY recorded_at
                """,
                last_run["run_id"],
            )
            for r in stage_rows:
                stage = r["stage"]
                if not stage:
                    continue
                update_run["stages"][stage] = {
                    "event_type": r["event_type"],
                    "recorded_at": r["recorded_at"],
                    "data": r["data"],
                }
            update_run["shutdown_at"] = await conn.fetchval(
                """
                SELECT recorded_at FROM platform.application_log
                WHERE engine = 'ops' AND run_id = $1 AND event_type = 'SHUTDOWN'
                ORDER BY recorded_at DESC LIMIT 1
                """,
                last_run["run_id"],
            )
            return update_run

    async def _q_coverage_gaps() -> dict:
        """Universe coverage gaps — bars + fundamentals.

        ETF correction (2026-05-14): the fundamentals check now excludes
        tickers classified as ETFs in ``platform.ticker_classifications``.
        ETFs legitimately have no ``fundamentals_quarterly`` rows
        because FMP doesn't cover them; the dashboard was flagging this
        as red even though it's expected. The bars check still covers
        every T1+T2 ticker — ETFs should have bars even though they
        lack fundamentals.

        ``tier_le_2_total`` is the bars denominator (every T1+T2
        ticker). The new ``non_etf_count`` is the fundamentals
        denominator (stocks in T1+T2, the only tickers expected to have
        fundamentals). Both reported so the dashboard can render both
        percentages honestly.
        """
        async with pool.acquire() as conn:
            counts = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE COALESCE(tc.asset_class, 'stock') = 'stock'
                    ) AS non_etf_count
                FROM platform.liquidity_tiers lt
                LEFT JOIN platform.ticker_classifications tc USING (ticker)
                WHERE lt.tier <= 2
                """
            )
            bar_rows = await conn.fetch(
                """
                SELECT lt.ticker
                FROM platform.liquidity_tiers lt
                LEFT JOIN platform.prices_daily_tickers p
                  ON p.ticker = lt.ticker
                  AND p.latest_date >= CURRENT_DATE - INTERVAL '5 days'
                WHERE lt.tier <= 2 AND p.ticker IS NULL
                ORDER BY lt.ticker
                """
            )
            # Only stocks (or unclassified tickers, treated as stocks)
            # are expected to carry fundamentals.
            fund_rows = await conn.fetch(
                """
                SELECT lt.ticker
                FROM platform.liquidity_tiers lt
                LEFT JOIN platform.ticker_classifications tc USING (ticker)
                LEFT JOIN (
                    SELECT DISTINCT ticker FROM platform.fundamentals_quarterly
                ) f ON f.ticker = lt.ticker
                WHERE lt.tier <= 2
                  AND COALESCE(tc.asset_class, 'stock') = 'stock'
                  AND f.ticker IS NULL
                ORDER BY lt.ticker
                """
            )
        return {
            "tier_le_2_total": int(counts["total"] or 0),
            "tier_le_2_non_etf_count": int(counts["non_etf_count"] or 0),
            "missing_bars": [r["ticker"] for r in bar_rows],
            "missing_fundamentals": [r["ticker"] for r in fund_rows],
        }

    async def _q_open_orders() -> dict:
        # Pending open_orders, total + stale-24h count.
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE status NOT IN ('filled','canceled','cancelled','rejected','expired')
                    ) AS pending_count,
                    COUNT(*) FILTER (
                        WHERE status NOT IN ('filled','canceled','cancelled','rejected','expired')
                          AND updated_at < now() - INTERVAL '24 hours'
                    ) AS stale_24h
                FROM platform.open_orders
                """
            )
            sample = await conn.fetch(
                """
                SELECT engine, ticker, status, updated_at
                FROM platform.open_orders
                WHERE status NOT IN ('filled','canceled','cancelled','rejected','expired')
                  AND updated_at < now() - INTERVAL '24 hours'
                ORDER BY updated_at ASC
                LIMIT 10
                """
            )
        return {
            "pending_count": int(r["pending_count"] or 0) if r else 0,
            "stale_24h": int(r["stale_24h"] or 0) if r else 0,
            "stale_sample": [
                {"engine": x["engine"], "ticker": x["ticker"], "status": x["status"], "updated_at": x["updated_at"]}
                for x in sample
            ],
        }

    async def _q_cross_ref() -> list[dict]:
        """Latest cross_table_audit.* per source — the auditheal-
        persisted SoT (replaces the pre-session inline COUNT checks
        that drifted from tpcore/audit/cross_table.py)."""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT source, MAX(timestamp) AS t
                    FROM platform.data_quality_log
                    WHERE source LIKE 'cross_table_audit.%'
                    GROUP BY source
                )
                SELECT q.source, q.stale, q.confidence
                FROM platform.data_quality_log q
                JOIN latest l ON l.source = q.source AND l.t = q.timestamp
                ORDER BY q.source
                """
            )
        return [dict(r) for r in rows]

    async def _q_validation() -> list[dict]:
        # Show ONLY the latest run per source. A 7-day aggregate would
        # surface stale history as "current failures" — exactly the bug
        # that triggered the operator's frustration: AAPL split ratio
        # appeared red because the rolling window included rows from
        # before the underlying data was fixed.
        # Per-source: pick the newest timestamp, then read its row.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT source, MAX(timestamp) AS latest_at
                    FROM platform.data_quality_log
                    WHERE source LIKE 'validation.%'
                    GROUP BY source
                )
                SELECT q.source, q.timestamp AS latest_at,
                       q.stale, q.confidence, q.notes
                FROM platform.data_quality_log q
                JOIN latest l ON l.source = q.source AND l.latest_at = q.timestamp
                ORDER BY q.source
                """
            )
            return [
                {
                    "source": r["source"],
                    "latest_at": r["latest_at"],
                    # "n_failed" / "n_runs" preserved for backward-compat with
                    # the classifier signature: 1/1 when latest run failed,
                    # 0/1 when it passed.
                    "n_failed": 1 if (r["stale"] or (r["confidence"] is not None and r["confidence"] < 1.0)) else 0,
                    "n_runs": 1,
                    "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                    "stale": r["stale"],
                    "notes": r["notes"],
                }
                for r in rows
            ]

    async def _q_catalyst() -> dict:
        """Catalyst-events coverage + freshness against T1+T2 stock subset.

        Counts mirror what ``validation.earnings_events_freshness``
        checks — so the dashboard row stays in lockstep with the suite
        and the operator sees the same red conditions in both places.
        """
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                WITH addressable AS (
                    SELECT lt.ticker
                    FROM platform.liquidity_tiers lt
                    LEFT JOIN platform.ticker_classifications tc USING (ticker)
                    WHERE lt.tier <= 2
                      AND COALESCE(tc.asset_class, 'stock') = 'stock'
                )
                SELECT
                    (SELECT COUNT(*) FROM addressable) AS addressable,
                    (SELECT COUNT(DISTINCT a.ticker)
                     FROM addressable a
                     JOIN platform.earnings_events ce ON ce.ticker = a.ticker
                     WHERE ce.event_date >= CURRENT_DATE - INTERVAL '180 days'
                    ) AS covered,
                    (SELECT MAX(event_date) FROM platform.earnings_events) AS newest_event,
                    (SELECT MAX(recorded_at) FROM platform.earnings_events) AS last_refresh,
                    (SELECT COUNT(*) FROM platform.earnings_events) AS total_rows
                """
            )
        return {
            "addressable": int(r["addressable"] or 0),
            "covered": int(r["covered"] or 0),
            "newest_event": r["newest_event"],
            "last_refresh": r["last_refresh"],
            "total_rows": int(r["total_rows"] or 0),
        }

    async def _q_forensics() -> dict:
        """Open forensics triggers (resolved_at IS NULL) — by kind + by age."""
        async with pool.acquire() as conn:
            counts = await conn.fetch(
                """
                SELECT trigger_kind,
                       COUNT(*) AS open_count,
                       MIN(fired_at) AS oldest_open_at
                FROM platform.forensics_triggers
                WHERE resolved_at IS NULL
                GROUP BY trigger_kind
                ORDER BY trigger_kind
                """
            )
            recent = await conn.fetch(
                """
                SELECT id, trigger_kind, payload, fired_at
                FROM platform.forensics_triggers
                WHERE resolved_at IS NULL
                ORDER BY fired_at DESC
                LIMIT 20
                """
            )
        return {
            "by_kind": [
                {
                    "kind": r["trigger_kind"],
                    "open_count": int(r["open_count"]),
                    "oldest_open_at": r["oldest_open_at"],
                }
                for r in counts
            ],
            "recent": [
                {
                    "id": int(r["id"]),
                    "kind": r["trigger_kind"],
                    "payload": r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]),
                    "fired_at": r["fired_at"],
                }
                for r in recent
            ],
        }

    try:
        bars, fund, ca, uni, run, val, coverage, orders, cross_ref, forensics, catalyst = await asyncio.gather(
            _q_bars(),
            _q_fundamentals(),
            _q_corp_actions(),
            _q_universe(),
            _q_update_run(),
            _q_validation(),
            _q_coverage_gaps(),
            _q_open_orders(),
            _q_cross_ref(),
            _q_forensics(),
            _q_catalyst(),
        )
        out["bars"] = bars
        out["fundamentals"] = fund
        out["corp_actions"] = ca
        out["universe"] = uni
        out["update_run"] = run
        out["validation"] = val
        out["coverage"] = coverage
        out["open_orders"] = orders
        out["cross_ref"] = cross_ref
        out["forensics"] = forensics
        out["catalyst"] = catalyst
    finally:
        await pool.close()
    return out


# Engine → client-order-id prefix at Alpaca. Each engine tags its orders
# at submission time so the dashboard can attribute fills back to the
# right engine without an extra DB lookup.
_ENGINE_ORDER_PREFIXES: dict[str, str] = {
    "momentum": "mo_",
    "sigma": "sig_",
    "reversion": "rev_",
    "vector": "vec_",
}


async def _fetch_recent_orders_all_engines() -> list[dict]:
    """All engine orders at Alpaca, newest first. Each order carries an
    ``engine`` field derived from the client_order_id prefix."""
    broker = AlpacaPaperBrokerAdapter()
    orders = await broker.list_recent_orders(limit=500)
    out: list[dict] = []
    for o in orders:
        cid = o.client_order_id or ""
        engine = None
        for eng, prefix in _ENGINE_ORDER_PREFIXES.items():
            if cid.startswith(prefix):
                engine = eng
                break
        if engine is None:
            continue
        out.append(
            {
                "engine": engine,
                "ticker": o.symbol,
                "side": getattr(o.side, "value", str(o.side)),
                "qty": int(o.qty) if o.qty else 0,
                "status": getattr(o.status, "value", str(o.status)),
                "submitted_at": o.submitted_at,
                "filled_at": o.filled_at,
                "avg_fill_price": float(o.avg_fill_price) if o.avg_fill_price else None,
                "client_order_id": o.client_order_id,
            }
        )
    return out


async def _fetch_recent_momentum_orders() -> list[dict]:
    """Back-compat alias. Filter all-engines output to momentum-only."""
    return [o for o in await _fetch_recent_orders_all_engines() if o.get("engine") == "momentum"]


async def _fetch_credibility_all_engines() -> dict:
    """Pull the latest credibility rubric for each engine in SCORECARD_ENGINES."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    out: dict = {}
    try:
        for engine in SCORECARD_ENGINES:
            try:
                out[engine] = await fetch_credibility(pool, engine)
            except Exception:  # noqa: BLE001
                out[engine] = None
    finally:
        await pool.close()
    return out


async def _fetch_today_recommendations(engine: str) -> list[dict]:
    """Tomorrow's ranked picks from the engine's setup-detection plug.

    For momentum: the top-decile ranked list that would be opened on the
    next rebalance day. For other engines: empty (Phase 1 scope).
    """
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    as_of = datetime.now(UTC).date()
    try:
        return await fetch_today_recommendations(pool, engine, as_of)
    finally:
        await pool.close()


async def _fetch_signals_all_engines(days: int = 30) -> list[dict]:
    """Pull SIGNAL events across all engines, newest first."""
    since = datetime.now(UTC) - timedelta(days=days)
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    all_signals: list[dict] = []
    try:
        for engine in SCORECARD_ENGINES:
            try:
                rows = await fetch_recent_signals(pool, engine, since)
            except Exception:  # noqa: BLE001
                rows = []
            for r in rows:
                r["engine"] = engine
                all_signals.append(r)
    finally:
        await pool.close()
    all_signals.sort(key=lambda r: r.get("recorded_at") or datetime.min, reverse=True)  # noqa: DTZ901
    return all_signals


async def _fetch_trades_all_engines(days: int = 30) -> list[dict]:
    """Pull AAR rows across all engines, newest first."""
    since = datetime.now(UTC) - timedelta(days=days)
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    out: list[dict] = []
    try:
        for engine in SCORECARD_ENGINES:
            try:
                trades = await fetch_recent_trades(pool, engine, since)
            except Exception:  # noqa: BLE001
                trades = []
            for t in trades:
                out.append(
                    {
                        "engine": engine,
                        "ticker": t.ticker,
                        "entry_ts": t.entry_ts,
                        "exit_ts": t.exit_ts,
                        "entry_price": float(t.entry_price),
                        "exit_price": float(t.exit_price),
                        "pnl_net": float(t.pnl_net),
                        "exit_reason": t.exit_reason.value,
                    }
                )
    finally:
        await pool.close()
    out.sort(key=lambda r: r["exit_ts"], reverse=True)
    return out


async def _fetch_equity_history(days: int = 60) -> list[dict]:
    """Read EQUITY_SNAPSHOT rows from platform.application_log."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT recorded_at, data
                FROM platform.application_log
                WHERE engine = 'momentum'
                  AND event_type = 'EQUITY_SNAPSHOT'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                ORDER BY recorded_at ASC
                """,
                days,
            )
    finally:
        await pool.close()
    out: list[dict] = []
    for r in rows:
        data = r["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        if not isinstance(data, dict):
            continue
        equity = data.get("equity")
        if equity is None:
            continue
        out.append({"timestamp": r["recorded_at"], "equity": float(equity)})
    return out


# ────────────────────────────────────────────────────────────────────────────
# Subprocess patterns (Phase 2)
# ────────────────────────────────────────────────────────────────────────────


def run_blocking_script(script: str, *, timeout: int = 600) -> tuple[int, str]:
    """Pattern A — short script, blocking, ≤timeout seconds.

    Returns (returncode, combined_output). UI renders the output inline
    immediately; operator expects to wait."""
    script_path = REPO_ROOT / script
    try:
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return -1, f"TIMEOUT after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"
    except Exception as exc:  # noqa: BLE001
        return -1, f"ERROR: {exc}"


def run_detached_script(script: str, *args: str) -> tuple[int, str]:
    """Pattern B — long script (≥10 min), detached so Streamlit recycles don't
    SIGTERM it. Returns (pid, logfile_path). UI tails the logfile on each
    rerun until the process exits."""
    script_path = REPO_ROOT / script
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    arg_tag = ("_" + "_".join(args)) if args else ""
    logfile = LOG_DIR / f"dashboard_{Path(script).stem}{arg_tag}_{ts}.log"
    proc = subprocess.Popen(
        [str(script_path), *args],
        stdout=open(logfile, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from Streamlit's process group
        cwd=str(REPO_ROOT),
    )
    return proc.pid, str(logfile)


def detached_job_status(pid: int, logfile: str) -> dict:
    """Status of a detached job. Heartbeat = logfile mtime delta."""
    try:
        # Signal 0 = "does this PID exist?"
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    p = Path(logfile)
    if not p.exists():
        return {"alive": alive, "logfile_exists": False, "tail": "", "stale_seconds": None}
    mtime = p.stat().st_mtime
    stale = time.time() - mtime
    try:
        with p.open() as fh:
            lines = fh.readlines()
        tail = "".join(lines[-30:])
    except Exception:  # noqa: BLE001
        tail = "(could not read logfile)"
    return {
        "alive": alive,
        "logfile_exists": True,
        "tail": tail,
        "stale_seconds": stale,
        "n_lines": len(lines) if p.exists() else 0,
    }


# ────────────────────────────────────────────────────────────────────────────
# UI components
# ────────────────────────────────────────────────────────────────────────────


def _color_glyph_for_pnl(pnl: float) -> tuple[str, str]:
    """Return (glyph, color) — pair color with glyph per WCAG accessibility."""
    if pnl > 0:
        return ("▲", "#0a8a3a")
    if pnl < 0:
        return ("▼", "#c92a2a")
    return ("◆", "#666666")


def _fmt_local(dt: datetime | None) -> str:
    """Format a datetime in the dashboard process's local timezone.

    The dashboard is intended to run on the operator's own Mac
    (localhost:8501), so process-local time == operator's wall clock.
    Backend persistence stays UTC; only the display is converted."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()  # system local TZ
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def render_header():
    st.title("Trading Engine — Operator Dashboard")
    try:
        state = _fetch_account_state_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch Alpaca account state: {exc}")
        return None
    pl = state["unrealized_pl"]
    glyph, color = _color_glyph_for_pnl(pl)
    pl_pct = (pl / state["equity"]) if state["equity"] else 0.0
    cols = st.columns(4)
    cols[0].metric("Equity", f"${state['equity']:,.2f}")
    cols[1].metric("Cash", f"${state['cash']:,.2f}")
    cols[2].metric("Positions", state["n_positions"])
    cols[3].markdown(
        f"<div style='font-size:0.875em;color:#666;'>Unrealized P&L</div>"
        f"<div style='font-size:1.75em;font-weight:600;color:{color};'>"
        f"{glyph} ${pl:+,.2f}  ({pl_pct * 100:+.3f}%)</div>",
        unsafe_allow_html=True,
    )
    # Local time first (operator-friendly); UTC kept in parens for
    # cross-reference with logs and DB rows.
    now_local = datetime.now().astimezone()
    fetched_local = state["fetched_at"].astimezone()
    st.caption(
        f"Now {now_local.strftime('%H:%M:%S %Z')} · "
        f"data as of {fetched_local.strftime('%H:%M:%S %Z')} "
        f"(UTC {state['fetched_at'].strftime('%H:%M:%S')})"
    )
    return state


def render_holdings():
    """Per-engine holdings — one expander block per engine. Selected ticker
    is returned so the ticker-detail panel below renders the chart for it.

    All four engines query Alpaca for their client-order-id prefix
    (mo_/sig_/rev_/vec_); a position only shows under the engine that
    opened it. The first row click across any engine wins.
    """
    import pandas as pd

    st.subheader("Currently holding")
    selected: str | None = None
    cols = {
        "Entry": st.column_config.NumberColumn(format="$%.2f"),
        "Current": st.column_config.NumberColumn(format="$%.2f"),
        "Market Value": st.column_config.NumberColumn(format="$%.2f"),
        "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
        "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
    }
    for engine in SCORECARD_ENGINES:
        try:
            holdings = _fetch_holdings_for_engine_cached(engine)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not fetch {engine} holdings: {exc}")
            continue
        with st.expander(f"{engine.capitalize()} — {len(holdings)} position(s)", expanded=bool(holdings)):
            if not holdings:
                st.caption(f"No open {engine.capitalize()} positions.")
                continue
            df = pd.DataFrame(holdings)
            df["pnl_pct"] = df["unrealized_pl_pct"] * 100.0
            df = df[["ticker", "qty", "entry_price", "current_price", "market_value", "unrealized_pl", "pnl_pct"]]
            df.columns = ["Ticker", "Qty", "Entry", "Current", "Market Value", "P&L $", "P&L %"]
            event = st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key=f"holdings_table_{engine}",
                column_config=cols,
            )
            selected_rows = event.selection.rows if event and event.selection else []
            if selected_rows and selected is None:
                selected = str(df.iloc[selected_rows[0]]["Ticker"])
    st.caption(
        f"Data as of {datetime.now(UTC).strftime('%H:%M:%S UTC')}  ·  Click a row to see its price chart"
    )
    return selected


def render_ticker_detail(ticker: str):
    """Phase 3 — candlestick chart for one held position with entry/exit markers."""
    st.subheader(f"Ticker detail — {ticker}")
    try:
        ohlc_rows = run_async(_fetch_ohlc(ticker, days=90))
        closed = run_async(_fetch_closed_trades_for_ticker(ticker, days=365))
        active = run_async(_fetch_active_entry_for_ticker(ticker))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch ticker data: {exc}")
        return
    import pandas as pd

    ohlc_df = pd.DataFrame(ohlc_rows)
    active_entries = [active] if active else []
    render_ticker_chart(
        ticker=ticker,
        ohlc=ohlc_df,
        closed_trades=closed,
        active_entries=active_entries,
    )
    # Compact metadata below the chart.
    if closed:
        st.caption(
            f"{len(closed)} closed trades in last 365d  ·  "
            f"latest exit: {closed[0]['exit_date']}  "
            f"({closed[0]['pnl_pct'] * 100:+.2f}%)"
        )
    if active:
        st.caption(
            f"Currently held: {active['qty']} sh @ ${active['entry_price']:.2f} opened {active['entry_date']}"
        )


# Human-readable labels for the credibility rubric flags. Used by the
# scorecard panel to show what's blocking each engine. Order matters —
# the rubric is presented in roughly logical order (integrity flags first,
# then overfitting bundle).
RUBRIC_LABELS: dict[str, str] = {
    "lookahead_clean": "Look-ahead clean",
    "survivorship_inclusive": "Survivorship-inclusive",
    "pit_fundamentals": "PIT fundamentals",
    "regime_coverage": "Regime coverage",
    "out_of_sample_validated": "Out-of-sample validated",
    "monte_carlo_drawdown": "Monte-Carlo drawdown",
    "sensitivity_surface_flat": "Sensitivity surface flat",
    "monte_carlo_sequence_passed": "Monte-Carlo sequence",
    "dsr_above_pass_threshold": f"DSR ≥ {DSR_PASS_THRESHOLD}",
    "backtest_length_above_minbtl": "Length ≥ MinBTL",
    "pbo_passes": "PBO passes",
    "trades_per_param_passes": "Trades/parameter ratio",
}


def render_credibility_scorecards():
    """Phase 4 — one compact card per engine. BLOCKED cards list the
    specific rubric flags that are failing so the operator knows what
    would need to improve to clear the gate."""
    st.subheader("Credibility scorecards")
    try:
        scores = _fetch_credibility_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch credibility scores: {exc}")
        return
    cols = st.columns(len(SCORECARD_ENGINES))
    for col, engine in zip(cols, SCORECARD_ENGINES, strict=False):
        score = scores.get(engine)
        if score is None:
            col.markdown(
                f"<div style='padding:12px;border-radius:6px;background:#f0f0f0;"
                f"text-align:center;'>"
                f"<div style='font-size:0.875em;color:#666;'>{engine.upper()}</div>"
                f"<div style='font-size:1.5em;font-weight:600;color:#888;'>—</div>"
                f"<div style='font-size:0.75em;color:#888;'>no rubric on record</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            continue
        passing = score.score >= MIN_LIVE_SCORE
        if passing:
            bg, fg, glyph, status = "#e6f4ea", "#0a8a3a", "▲", "PASS"
        else:
            bg, fg, glyph, status = "#fbe9e7", "#c92a2a", "▼", "BLOCKED"

        rubric_dict = score.model_dump()
        passing_flags = [k for k, v in rubric_dict.items() if isinstance(v, bool) and v]
        failing_flags = [
            RUBRIC_LABELS.get(k, k)
            for k, v in rubric_dict.items()
            if isinstance(v, bool) and not v and k in RUBRIC_LABELS
        ]

        # Header block — score + status pill
        header = (
            f"<div style='padding:12px 12px 6px 12px;border-radius:6px 6px 0 0;"
            f"background:{bg};text-align:center;'>"
            f"<div style='font-size:0.875em;color:#666;'>{engine.upper()}</div>"
            f"<div style='font-size:1.5em;font-weight:600;color:{fg};'>"
            f"{glyph} {score.score}/100</div>"
            f"<div style='font-size:0.75em;color:{fg};font-weight:600;'>{status}</div>"
            f"</div>"
        )

        # Failing-flag list — what's blocking the gate. For PASS engines,
        # show a green 'all checks pass' line instead.
        if passing:
            details = (
                "<div style='padding:6px 12px 12px 12px;background:#f8fcf9;"
                "border-radius:0 0 6px 6px;font-size:0.75em;color:#0a8a3a;'>"
                f"All {len(passing_flags)} rubric checks pass ✓"
                "</div>"
            )
        elif failing_flags:
            items = "".join(f"<li style='margin-bottom:2px;'>{label}</li>" for label in failing_flags)
            details = (
                "<div style='padding:6px 12px 12px 12px;background:#fdf4f4;"
                "border-radius:0 0 6px 6px;font-size:0.75em;color:#666;'>"
                f"<div style='font-weight:600;color:#c92a2a;margin-bottom:4px;'>"
                f"Blocking ({len(failing_flags)}):</div>"
                f"<ul style='margin:0;padding-left:18px;'>{items}</ul>"
                "</div>"
            )
        else:
            details = (
                "<div style='padding:6px 12px 12px 12px;background:#fdf4f4;"
                "border-radius:0 0 6px 6px;font-size:0.75em;color:#666;'>"
                f"{len(passing_flags)} flags pass · score still &lt; {MIN_LIVE_SCORE}"
                "</div>"
            )

        col.markdown(header + details, unsafe_allow_html=True)

    st.caption(f"Data as of {datetime.now(UTC).strftime('%H:%M:%S UTC')}  ·  Gate is ≥{MIN_LIVE_SCORE}/100")


def render_recent_activity():
    """Phase 4 — side-by-side panels: signals (left) and closed trades (right)."""
    st.subheader("Recent activity — last 30 days")
    try:
        signals = _fetch_signals_cached(days=30)
        trades = _fetch_trades_cached(days=30)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch recent activity: {exc}")
        return
    import pandas as pd

    left, right = st.columns(2)

    with left:
        st.markdown("**Signals**")
        if not signals:
            st.info("No SIGNAL events in window.")
        else:
            rows = []
            for s in signals[:50]:
                data = s.get("data") or {}
                ticker = data.get("ticker", "?") if isinstance(data, dict) else "?"
                score = data.get("score") if isinstance(data, dict) else None
                rows.append(
                    {
                        "When": _fmt_local(s.get("recorded_at")),
                        "Engine": s.get("engine"),
                        "Ticker": ticker,
                        "Score": f"{score:+.3f}" if isinstance(score, (int, float)) else "",
                    }
                )
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True, height=320)
            if len(signals) > 50:
                st.caption(f"showing latest 50 of {len(signals)}")

    with right:
        st.markdown("**Closed trades (AARs)**")
        if not trades:
            st.info("No closed trades in window.")
        else:
            rows = []
            for t in trades[:50]:
                pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] if t["entry_price"] else 0.0
                rows.append(
                    {
                        "Exited": t["exit_ts"].date() if hasattr(t["exit_ts"], "date") else t["exit_ts"],
                        "Engine": t["engine"],
                        "Ticker": t["ticker"],
                        "P&L $": t["pnl_net"],
                        "P&L %": pnl_pct * 100.0,
                        "Exit reason": t["exit_reason"],
                    }
                )
            df = pd.DataFrame(rows)
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                height=320,
                column_config={
                    "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
                    "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )
            if len(trades) > 50:
                st.caption(f"showing latest 50 of {len(trades)}")


def render_recent_orders():
    """Recent orders across every engine — status histogram + latest 30
    rows. Each order is tagged with its originating engine via the
    client_order_id prefix (mo_/sig_/rev_/vec_)."""
    st.subheader("Recent orders — all engines")
    try:
        orders = _fetch_recent_orders_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch orders: {exc}")
        return
    if not orders:
        st.info("No engine orders (mo_/sig_/rev_/vec_ prefix) at the broker.")
        return

    # Status histogram — big numbers at top, color-coded by category.
    from collections import Counter

    statuses = Counter(o["status"] for o in orders)
    cols = st.columns(max(len(statuses), 1))
    # Stable ordering: fills/active first, then terminal.
    status_order = [
        "filled",
        "partially_filled",
        "new",
        "accepted",
        "pending_new",
        "canceled",
        "rejected",
        "expired",
    ]
    ordered_keys = [s for s in status_order if s in statuses] + sorted(set(statuses) - set(status_order))
    for col, status in zip(cols, ordered_keys, strict=False):
        n = statuses[status]
        if status == "filled":
            color = "#0a8a3a"
        elif status in ("new", "accepted", "pending_new", "partially_filled"):
            color = "#1565c0"
        elif status == "canceled":
            color = "#888"
        elif status in ("rejected", "expired"):
            color = "#c92a2a"
        else:
            color = "#d68800"
        col.markdown(
            f"<div style='text-align:center;padding:8px;'>"
            f"<div style='font-size:2em;font-weight:600;color:{color};'>{n}</div>"
            f"<div style='font-size:0.85em;color:#666;'>{status}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Table of the latest 30 orders.
    import pandas as pd

    sorted_orders = sorted(
        orders,
        key=lambda x: x["submitted_at"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    rows = []
    for o in sorted_orders[:30]:
        rows.append(
            {
                "Engine": o.get("engine", "?"),
                "Submitted": _fmt_local(o["submitted_at"]),
                "Ticker": o["ticker"],
                "Side": o["side"],
                "Qty": o["qty"],
                "Status": o["status"],
                "Fill price": o["avg_fill_price"],
                "Filled at": _fmt_local(o["filled_at"]),
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=320,
        column_config={
            "Fill price": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    by_engine = Counter(o.get("engine", "?") for o in orders)
    breakdown = " · ".join(f"{e}: {n}" for e, n in sorted(by_engine.items()))
    st.caption(f"{len(orders)} total engine orders at the broker ({breakdown})  ·  showing newest {min(30, len(orders))}")


def render_equity_curve():
    st.subheader("Equity curve — last 60 days")
    try:
        history = _fetch_equity_history_cached(days=60)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch equity history: {exc}")
        return
    if not history:
        st.info(
            "No EQUITY_SNAPSHOT events yet — these are written by the momentum "
            "scheduler each run. After the first scheduler run, this panel populates."
        )
        return
    import pandas as pd

    df = pd.DataFrame(history)
    df = df.set_index("timestamp")
    st.line_chart(df["equity"], height=240, use_container_width=True)
    st.caption(f"{len(history)} snapshots over the lookback window")


# ────────────────────────────────────────────────────────────────────────────
# Action buttons (Phase 2)
# ────────────────────────────────────────────────────────────────────────────


def _render_process_status_inline():
    """Compact process-status indicator. Sits at the top of the Actions
    section and shows whether the dashboard is idle or has a long-running
    detached job in flight. Visibility-of-system-status (Nielsen #1)."""
    job = st.session_state.get("detached_job")
    if not job:
        st.markdown(
            "<div style='display:inline-block;padding:4px 10px;border-radius:4px;"
            "background:#e6f4ea;color:#0a8a3a;font-weight:600;font-size:0.875em;'>"
            "🟢 Idle</div>",
            unsafe_allow_html=True,
        )
        return
    status = detached_job_status(job["pid"], job["logfile"])
    elapsed_min = (time.time() - job["started_at"]) / 60
    stale = status.get("stale_seconds") or 0
    if not status["alive"]:
        # Just-finished — about to be acknowledged by the heartbeat panel below.
        st.markdown(
            "<div style='display:inline-block;padding:4px 10px;border-radius:4px;"
            "background:#e6f4ea;color:#0a8a3a;font-weight:600;font-size:0.875em;'>"
            f"🟢 {job['name']} completed</div>",
            unsafe_allow_html=True,
        )
        return
    if stale > 900:
        bg, fg, glyph = "#fbe9e7", "#c92a2a", "🔴"
    elif stale > 300:
        bg, fg, glyph = "#fff4e5", "#d68800", "🟡"
    else:
        bg, fg, glyph = "#e3f2fd", "#1565c0", "🔵"
    st.markdown(
        f"<div style='display:inline-block;padding:4px 10px;border-radius:4px;"
        f"background:{bg};color:{fg};font-weight:600;font-size:0.875em;'>"
        f"{glyph} {job['name']} running — {elapsed_min:.1f} min</div>",
        unsafe_allow_html=True,
    )


def _fmt_age(ts) -> tuple[str, str]:
    """Format a 'last run' timestamp as ('2h ago', '#color') tuple.

    Color encodes staleness: green = fresh, amber = aging, red = stale,
    grey = never run. Tuned per-workflow inside _render_action_status."""
    if ts is None:
        return ("never", "#888888")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - ts
    secs = delta.total_seconds()
    if secs < 60:
        text = f"{int(secs)}s ago"
    elif secs < 3600:
        text = f"{int(secs / 60)} min ago"
    elif secs < 86400:
        text = f"{secs / 3600:.1f}h ago"
    elif secs < 86400 * 14:
        text = f"{int(secs / 86400)}d ago"
    else:
        text = ts.strftime("%Y-%m-%d")
    return (text, "")  # color set by caller based on workflow-specific staleness


def _render_status_line(label: str, age_text: str, *, status_color: str = "#666") -> None:
    st.markdown(
        f"<div style='font-size:0.8em;color:#666;margin-top:-8px;'>"
        f"{label} <span style='color:{status_color};font-weight:600;'>{age_text}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _color_for_age(secs: float, fresh_max: float, stale_min: float) -> str:
    """Green if < fresh_max seconds; amber until stale_min; red beyond."""
    if secs < fresh_max:
        return "#0a8a3a"
    if secs < stale_min:
        return "#d68800"
    return "#c92a2a"


def _age_seconds(ts) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


# ────────────────────────────────────────────────────────────────────────────
# Platform-health panel — surface what the operator needs before trading
# ────────────────────────────────────────────────────────────────────────────

_GLYPH_OK = "🟢"
_GLYPH_AGING = "🟡"
_GLYPH_BAD = "🔴"
_GLYPH_UNKNOWN = "⚪"


def _health_glyph(color: str) -> str:
    return {
        "green": _GLYPH_OK,
        "amber": _GLYPH_AGING,
        "red": _GLYPH_BAD,
    }.get(color, _GLYPH_UNKNOWN)


def _render_health_row(
    label: str,
    color: str,
    text: str,
    *,
    fix_action: str | None = None,
    row_key: str | None = None,
) -> None:
    """Render one row of the Platform-health panel.

    Args:
        label: 18ch left column.
        color: ``green`` / ``amber`` / ``red`` / unknown (drives glyph + text color).
        text: short operator-facing status string.
        fix_action: key into ``HEALTH_ACTIONS`` (e.g. ``"daily_update"``). When
            set AND the row is not green, an inline 🔧 Fix button is shown.
        row_key: extra uniqueness suffix for the Streamlit widget key — needed
            because the same ``fix_action`` may appear next to multiple rows.
    """
    glyph = _health_glyph(color)
    color_hex = {
        "green": "#0a8a3a",
        "amber": "#c77700",
        "red": "#c62828",
    }.get(color, "#666666")
    show_button = fix_action is not None and color in ("amber", "red")
    if show_button:
        cols = st.columns([3, 6, 2])
    else:
        cols = st.columns([3, 8, 0.01])
    cols[0].markdown(
        f'<div style="color:#888; padding-top:0.4rem;">{label}</div>',
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        f'<div style="padding-top:0.4rem;"><span>{glyph}</span> '
        f'<span style="color:{color_hex};">{text}</span></div>',
        unsafe_allow_html=True,
    )
    if show_button:
        action = HEALTH_ACTIONS[fix_action]
        btn_key = f"health_fix_{fix_action}_{row_key or label}".replace(" ", "_")
        if cols[2].button(
            f"🔧 {action['label']}",
            key=btn_key,
            help=action["help"],
            use_container_width=True,
        ):
            _dispatch_health_fix(fix_action)


def _render_stage_detail_row(
    stage: str,
    color: str,
    text: str,
    *,
    job_busy: bool,
    is_downstream_of_red: bool,
) -> None:
    """One row inside the stage-by-stage expander. Adds a per-stage 🔧 Fix
    button when the stage is amber/red AND we know how to remediate it
    (``data_validation`` is intentionally button-less — it's a symptom).

    Disable rules:
      * ``job_busy``           — another detached job is already running.
        Concurrent dispatches would orphan the first job's tracking entry
        in session_state.
      * ``is_downstream_of_red`` — an earlier stage is red; running this
        one solo will likely fail until the upstream is fixed. Tooltip
        steers the operator upstream.
    """
    glyph = _health_glyph(color)
    color_hex = {"green": "#0a8a3a", "amber": "#c77700", "red": "#c62828"}.get(color, "#666666")
    show_button = (
        color in ("amber", "red")
        and stage in _STAGE_FIX_HELP
        and stage != "data_validation"  # symptom, not a switch
    )
    if show_button:
        cols = st.columns([3, 6, 2])
    else:
        cols = st.columns([3, 8, 0.01])
    cols[0].markdown(
        f'<div style="color:#888; padding-top:0.4rem;">{stage}</div>',
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        f'<div style="padding-top:0.4rem;"><span>{glyph}</span> '
        f'<span style="color:{color_hex};">{text}</span></div>',
        unsafe_allow_html=True,
    )
    if show_button:
        if job_busy:
            cols[2].button(
                "🔧 busy",
                key=f"stage_fix_{stage}_disabled_busy",
                help="Another job is already running. Wait for it to finish.",
                disabled=True,
                use_container_width=True,
            )
        elif is_downstream_of_red:
            cols[2].button(
                "🔧 upstream",
                key=f"stage_fix_{stage}_disabled_upstream",
                help=(
                    "An earlier stage is red; this stage probably won't "
                    "succeed until upstream is fixed."
                ),
                disabled=True,
                use_container_width=True,
            )
        else:
            help_text = _STAGE_FIX_HELP[stage]
            if st.button(
                "🔧 Fix",
                key=f"stage_fix_{stage}_active",
                help=help_text,
                use_container_width=True,
            ):
                _dispatch_stage_fix(stage)


# ─── Fix-it action registry ─────────────────────────────────────────────────
# Each entry maps a fix_action key to {label, help, script, blocking?}.
# Non-blocking actions run detached via ``run_detached_script`` (same
# pattern as the main Daily-update button); blocking actions use
# ``run_blocking_script`` with a timeout.

HEALTH_ACTIONS: dict[str, dict] = {
    "daily_update": {
        "label": "Run data-operations",
        "help": "Full operator workflow: ops.py --update (13 stages) + cross-table audit + validation re-confirm + compress CSVs. Detached, ~30-45 min.",
        "script": "scripts/run_data_operations.sh",
        "args": (),
        "blocking": False,
    },
    "prescreener": {
        "label": "Re-run prescreener",
        "help": "Re-populates today's universe_candidates rows for momentum. Fast (~7s).",
        "script": "scripts/run_prescreener_only.sh",
        "args": (),
        "blocking": False,
    },
    "validation_rerun": {
        "label": "Re-run validation",
        "help": "Re-runs the delistings/constituent/splits checks against current prices_daily. ~15s.",
        "script": "scripts/run_stage.sh",
        "args": ("data_validation",),
        "blocking": False,
    },
}


def _render_ticker_list(label: str, tickers: list[str], cap: int = 20) -> None:
    """Render a capped list of tickers under a label. Used for coverage-gap
    detail rows. Caps at ``cap`` to keep the expander readable."""
    n = len(tickers)
    if n == 0:
        st.markdown(f"**{label}:** _none_ 🟢", unsafe_allow_html=True)
        return
    shown = tickers[:cap]
    overflow = n - len(shown)
    suffix = f" … and {overflow} more" if overflow > 0 else ""
    st.markdown(
        f"**{label}** ({n}): <code>{', '.join(shown)}</code>{suffix}",
        unsafe_allow_html=True,
    )


def _render_validation_failure_detail(source: str, notes: object) -> None:
    """Render the per-ticker failure list under a failing validation source.

    ``notes`` is the JSON payload stored in ``data_quality_log.notes`` —
    a list of ``{ticker, reason, expected, observed}`` dicts. We surface
    each one so the operator can see WHAT specifically is wrong without
    running an ad-hoc SQL query.

    Each row also offers a focused remediation when one is meaningful:

      * ``reason == "missing"``        → 🔧 Backfill ticker (re-pulls bars)
      * ``reason == "ratio_off"``      → 🔧 Re-apply splits (re-runs corp_actions)
      * ``reason == "stale"``          → 🔧 Run daily update (full refresh)

    Other reasons surface as info-only — investigation required.
    """
    if not notes:
        st.caption("No detail recorded for this failure.")
        return
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except Exception:
            st.code(notes[:500])
            return
    if not isinstance(notes, list):
        st.caption(f"Unexpected notes shape: {type(notes).__name__}")
        return
    job_busy = _detached_job_is_live()
    for i, entry in enumerate(notes[:10]):  # cap to avoid wall-of-text
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker", "?")
        reason = entry.get("reason", "?")
        expected = entry.get("expected", "")
        observed = entry.get("observed", "")
        cols = st.columns([2, 4, 2, 2, 2])
        cols[0].markdown(f"<span style='color:#888;'>&nbsp;&nbsp;&nbsp;&nbsp;{ticker}</span>", unsafe_allow_html=True)
        cols[1].markdown(f"<span style='color:#c62828;'>{reason}</span>", unsafe_allow_html=True)
        cols[2].caption(f"expected: {expected}"[:50])
        cols[3].caption(f"observed: {observed}"[:50])
        action_key = _validation_remediation(source, reason)
        if action_key is None:
            cols[4].caption("(investigate)")
            continue
        action = HEALTH_ACTIONS[action_key]
        btn_key = f"val_fix_{source}_{ticker}_{i}".replace(" ", "_")
        if job_busy:
            cols[4].button("🔧 busy", key=btn_key + "_busy", disabled=True, use_container_width=True, help="Another job running")
        else:
            if cols[4].button(
                f"🔧 {action['label']}",
                key=btn_key,
                help=action["help"],
                use_container_width=True,
            ):
                _dispatch_health_fix(action_key)
    if len(notes) > 10:
        st.caption(f"… and {len(notes) - 10} more")


def _validation_remediation(source: str, reason: str) -> str | None:
    """Map a (source, reason) pair from a validation failure to a
    HEALTH_ACTIONS key. Returns None when the failure isn't reliably
    one-click fixable (operator needs to investigate)."""
    if reason == "stale":
        return "daily_update"
    if reason == "ratio_off":
        return "daily_update"  # re-running daily_update re-applies corp_actions
    if reason == "missing":
        # A missing-ticker failure in delistings/constituent usually means
        # the universe ingest didn't cover it. daily_update is the broadest
        # safe fix.
        return "daily_update"
    return None

# Per-stage fix actions — each runs `scripts/run_stage.sh <stage>` which
# invokes `scripts/ops.py --stage <name>`. Same logging + event shape as
# inside a full --update; advisory-locked against concurrent runs of the
# same stage. The dashboard wires these into the stage-detail expander.
_STAGE_FIX_HELP: dict[str, str] = {
    "daily_bars":            "Re-pull today's bars from Alpaca. Heavy (~30 min on cold cache).",
    "corporate_actions":     "Re-pull splits/dividends and apply to prices_daily. Heavy.",
    "fundamentals_refresh":  "Re-fetch FMP fundamentals; tickers already refreshed in the last 24h are skipped, so retries make progress.",
    "data_validation":       "Re-run the delistings/constituent/splits checks. Symptom-level — root cause is upstream data.",
    "universe_prescreener":  "Re-populate today's momentum universe_candidates rows. Fast (~7s).",
    "universe_simulation":   "Diagnostic — re-runs scripts/simulate_universe.py and writes the candidate-count event.",
}


def _dispatch_health_fix(action_key: str) -> None:
    """Launch the script bound to ``action_key`` and record it in session_state
    so the detached-job panel can tail its log. Clears the Platform-health
    cache so the panel re-fetches on next render."""
    action = HEALTH_ACTIONS[action_key]
    extra_args = action.get("args") or ()
    if action.get("blocking"):
        with st.spinner(f"Running {action['label']}..."):
            rc, output = run_blocking_script(action["script"], timeout=300)
        _render_blocking_output(action["label"], rc, output)
    else:
        pid, logfile = run_detached_script(action["script"], *extra_args)
        st.session_state["detached_job"] = {
            "name": action["label"],
            "pid": pid,
            "logfile": logfile,
            "started_at": time.time(),
        }
        st.success(f"Launched (pid {pid}); logfile: {logfile}")
    _fetch_platform_health_cached.clear()
    st.rerun()


def _dispatch_stage_fix(stage_name: str) -> None:
    """Launch ``scripts/run_stage.sh <stage_name>`` detached. Same session_state
    + cache-clear contract as ``_dispatch_health_fix``."""
    pid = _spawn_detached(["scripts/run_stage.sh", stage_name])
    st.session_state["detached_job"] = {
        "name": f"Re-run stage: {stage_name}",
        "pid": pid["pid"],
        "logfile": str(pid["logfile"]),
        "started_at": time.time(),
    }
    st.success(f"Launched stage '{stage_name}' (pid {pid['pid']}); logfile: {pid['logfile']}")
    _fetch_platform_health_cached.clear()
    st.rerun()


def _spawn_detached(argv: list[str]) -> dict:
    """Spawn ``argv`` detached the same way ``run_detached_script`` does for
    single-string commands. Returns ``{"pid": int, "logfile": Path}``."""
    logfile = LOG_DIR / f"dashboard_{argv[0].rsplit('/',1)[-1]}_{argv[-1] if len(argv) > 1 else 'x'}_{datetime.now(UTC):%Y%m%d_%H%M%S}.log"
    proc = subprocess.Popen(  # noqa: S603 — script paths come from our own registry
        argv,
        stdout=open(logfile, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(REPO_ROOT),
    )
    return {"pid": proc.pid, "logfile": logfile}


def _detached_job_is_live() -> bool:
    """True iff a detached job recorded in session_state is still running.
    Used to disable Fix buttons so a second click can't overwrite the
    tracked job and leave the first one orphaned."""
    job = st.session_state.get("detached_job")
    if not job:
        return False
    try:
        os.kill(job["pid"], 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_account_state_cached() -> dict:
    return run_async(_fetch_account_state())


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_holdings_for_engine_cached(engine: str) -> list[dict]:
    return run_async(_fetch_holdings_for_engine(engine))


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_last_run_timestamps_cached() -> dict:
    return run_async(_fetch_last_run_timestamps())


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_daemon_state_cached() -> list[dict]:
    return _fetch_daemon_state()


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_recent_orders_cached() -> list[dict]:
    return run_async(_fetch_recent_orders_all_engines())


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_credibility_cached() -> dict:
    return run_async(_fetch_credibility_all_engines())


@st.cache_data(ttl=180, show_spinner=False)
def _fetch_today_recommendations_cached(engine: str) -> list[dict]:
    return run_async(_fetch_today_recommendations(engine))


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_signals_cached(days: int = 30) -> list[dict]:
    return run_async(_fetch_signals_all_engines(days=days))


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_trades_cached(days: int = 30) -> list[dict]:
    return run_async(_fetch_trades_all_engines(days=days))


@st.cache_data(ttl=120, show_spinner=False)
def _fetch_equity_history_cached(days: int = 60) -> list[dict]:
    return run_async(_fetch_equity_history(days=days))


async def _fetch_daemon_progress() -> dict:
    """Stage-by-stage progress of the most recent data_operations daemon run.

    Mirrors the queries in ``scripts/ops.py::_check_daemon_progress``.
    Verified Phase 0 (2026-05-15): every ``_STAGE_SPECS`` stage writes
    symmetric INGESTION_START + INGESTION_COMPLETE pairs (or
    INGESTION_FAILED), all sharing the daemon's tagged ``run_id``, and
    the daemon writes SHUTDOWN on exit. The renderer is intentionally
    dumb — severity decisions live in the fetcher.
    """
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    out: dict = {"state": "no_recent_run", "stages": []}
    try:
        startup = await pool.fetchrow(
            """
            SELECT run_id, recorded_at FROM platform.application_log
            WHERE engine='ops' AND event_type='STARTUP'
              AND data->>'source' = 'data_operations_daemon'
              AND recorded_at > now() - INTERVAL '25 hours'
            ORDER BY recorded_at DESC LIMIT 1
            """
        )
        if startup is None:
            return out
        out["run_id"] = str(startup["run_id"])
        out["started_at"] = startup["recorded_at"]
        rows = await pool.fetch(
            """
            SELECT data->>'stage' AS stage, event_type, recorded_at
            FROM platform.application_log
            WHERE run_id = $1
              AND event_type IN ('INGESTION_START','INGESTION_COMPLETE','INGESTION_FAILED')
              AND data->>'stage' IS NOT NULL
            ORDER BY recorded_at
            """,
            startup["run_id"],
        )
        by_stage: dict[str, dict] = {}
        for r in rows:
            st = r["stage"]
            if st not in by_stage:
                by_stage[st] = {"stage": st, "started_at": None, "ended_at": None, "status": None}
            if r["event_type"] == "INGESTION_START":
                by_stage[st]["started_at"] = r["recorded_at"]
                if by_stage[st]["status"] is None:
                    by_stage[st]["status"] = "running"
            elif r["event_type"] == "INGESTION_COMPLETE":
                by_stage[st]["ended_at"] = r["recorded_at"]
                by_stage[st]["status"] = "completed"
            elif r["event_type"] == "INGESTION_FAILED":
                by_stage[st]["ended_at"] = r["recorded_at"]
                by_stage[st]["status"] = "failed"
        out["stages"] = list(by_stage.values())
        out["n_completed"] = sum(1 for s in out["stages"] if s["status"] == "completed")
        out["n_failed"] = sum(1 for s in out["stages"] if s["status"] == "failed")
        out["n_running"] = sum(1 for s in out["stages"] if s["status"] == "running")
        shutdown = await pool.fetchrow(
            """
            SELECT recorded_at, message FROM platform.application_log
            WHERE run_id = $1 AND event_type='SHUTDOWN'
            ORDER BY recorded_at DESC LIMIT 1
            """,
            startup["run_id"],
        )
        if shutdown is None:
            out["state"] = "running"
        else:
            out["ended_at"] = shutdown["recorded_at"]
            if "exit_code=0" in (shutdown["message"] or "") and out["n_failed"] == 0:
                out["state"] = "completed_clean"
            else:
                out["state"] = "completed_with_failures"
    finally:
        await pool.close()
    return out


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_daemon_progress_cached() -> dict:
    """Short TTL — 30s — because a live daemon run should look live.
    Operator can also click the platform-health ↻ Refresh button to
    force-clear."""
    return run_async(_fetch_daemon_progress())


@st.cache_data(ttl=180, show_spinner=False)
def _fetch_platform_health_cached() -> dict:
    """Sync wrapper around ``_fetch_platform_health`` so Streamlit can
    cache the result. The fetcher hits the live DB and the bars query
    alone is ~4s — without this cache, every dashboard rerun re-paid the
    cost. 3-minute TTL is well shorter than the daily cadence at which
    these signals can actually change.

    Daemon state is filesystem-only (no DB), so we fetch it OUT of the
    cache so install/uninstall actions reflect immediately."""
    h = run_async(_fetch_platform_health())
    # NOT cached — adding here keeps the API single but lets the live
    # state refresh on every render.
    return h


async def _fetch_escalation_state() -> dict:
    """Read-only escalation/integrity SoT for the audit panel. REUSES
    ops.weekly_digest.build_weekly_digest for the policy-annotated
    undispositioned list (console & weekly digest cannot disagree);
    the rest are small reads of the rows the platform already gates
    on. Recomputes no predicate."""
    from ops.weekly_digest import build_weekly_digest

    pool = await build_asyncpg_pool(_db_url(), max_size=4)
    try:
        digest = await build_weekly_digest(pool)
        async with pool.acquire() as conn:
            holds = await conn.fetch(
                """
                SELECT h.data->>'source' AS source,
                       h.data->>'reason'  AS reason,
                       h.recorded_at      AS held_at
                FROM platform.application_log h
                LEFT JOIN platform.application_log c
                  ON c.event_type = 'DATA_SOURCE_CLEARED'
                 AND (c.data->>'hold_id') = (h.data->>'hold_id')
                WHERE h.event_type = 'DATA_SOURCE_HELD'
                  AND c.event_type IS NULL
                ORDER BY h.recorded_at
                """
            )
            ct = await conn.fetch(
                """
                WITH latest AS (
                    SELECT source, MAX(timestamp) AS t
                    FROM platform.data_quality_log
                    WHERE source LIKE 'cross_table_audit.%'
                    GROUP BY source
                )
                SELECT q.source, q.stale, q.confidence
                FROM platform.data_quality_log q
                JOIN latest l ON l.source=q.source AND l.t=q.timestamp
                ORDER BY q.source
                """
            )
            esc = await conn.fetch(
                """
                SELECT e.data->>'request_id' AS ref,
                       'DATA_REPAIR_ESCALATED' AS etype,
                       e.recorded_at, e.message,
                       EXISTS (
                         SELECT 1 FROM platform.application_log t
                         WHERE t.event_type='DATA_REPAIR_COMPLETE'
                           AND t.data->>'request_id'=e.data->>'request_id'
                           AND t.recorded_at > e.recorded_at) AS resolved
                FROM platform.application_log e
                WHERE e.event_type='DATA_REPAIR_ESCALATED'
                  AND e.recorded_at > now() - interval '7 days'
                UNION ALL
                SELECT e.data->>'feed' AS ref,
                       'AdapterContractDrift' AS etype,
                       e.recorded_at, e.message,
                       false AS resolved
                FROM platform.application_log e
                WHERE e.event_type='INGESTION_FAILED'
                  AND e.data->>'exception_type'='AdapterContractDrift'
                  AND e.recorded_at > now() - interval '7 days'
                ORDER BY recorded_at DESC
                """
            )
    finally:
        await pool.close()
    return {
        "undispositioned": list(digest.undispositioned),
        "source_holds": [dict(r) for r in holds],
        "cross_table_audit": [dict(r) for r in ct],
        "recent_escalations": [dict(r) for r in esc],
    }


@st.cache_data(ttl=180, show_spinner=False)
def _fetch_escalation_state_cached() -> dict:
    """Sync cache wrapper (same idiom/TTL as
    _fetch_platform_health_cached)."""
    return run_async(_fetch_escalation_state())


def render_daemon_progress() -> None:
    """Stage-by-stage progress of the most recent data_operations daemon run.

    Three render modes:

    * **no_recent_run** — last daemon STARTUP > 25h ago. One-line note,
      no expander. (The dedicated ``missed_data_operations`` probe flips
      red at the 30h ceiling — that's the separate alarm path.)
    * **running** — STARTUP present, no SHUTDOWN. Renders a progress
      bar (n_completed / n_stages_total) and a per-stage status table.
      The currently-running stage shows ⏳ + live-elapsed seconds.
    * **completed_clean** / **completed_with_failures** — terminal
      states. Same per-stage table, but elapsed times are frozen.

    Scope limit: covers only the 15 stages inside ``ops.py --update``.
    The bash wrapper's subsequent steps (audit, validation re-confirm,
    compress, ``DATA_OPERATIONS_COMPLETE`` emit) don't write to
    ``application_log`` — they're outside this panel's visibility. To
    confirm the full workflow completed, look at the
    ``missed_data_operations`` probe in ``--check``.
    """
    try:
        p = _fetch_daemon_progress_cached()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not fetch daemon progress: {exc}")
        return

    state = p.get("state", "no_recent_run")

    if state == "no_recent_run":
        st.caption(
            "**Daemon progress** — no `data_operations_daemon` STARTUP in the last 25h. "
            "The next scheduled fire is per the daemon row above."
        )
        return

    # Header line + run identifier.
    state_emoji = {
        "running": "⏳",
        "completed_clean": "✅",
        "completed_with_failures": "⚠️",
    }.get(state, "·")
    n_total = max(1, len(p.get("stages", [])))
    n_done = p.get("n_completed", 0)
    n_failed = p.get("n_failed", 0)
    n_running = p.get("n_running", 0)
    started_at = p.get("started_at")
    header_parts = [f"{state_emoji} **{state.replace('_', ' ')}**"]
    if started_at:
        header_parts.append(f"started {started_at.strftime('%H:%M:%S UTC')}")
    header_parts.append(f"{n_done}/{n_total} stages")
    if n_failed:
        header_parts.append(f"⚠ {n_failed} failed")
    if n_running:
        header_parts.append(f"⏳ {n_running} running")
    st.markdown(f"**Daemon progress** — {' · '.join(header_parts)}")

    # Progress bar — fraction of stages completed (failed stages count
    # as 'done' in the bar so a half-failed run still shows progress).
    completed_or_failed = n_done + n_failed
    st.progress(min(1.0, completed_or_failed / n_total))

    # Per-stage detail in an expander — auto-expand if currently running
    # or if any stage failed.
    auto_expand = state == "running" or n_failed > 0
    with st.expander("Per-stage detail", expanded=auto_expand):
        from datetime import UTC
        from datetime import datetime as _dt

        now = _dt.now(UTC)
        rows = []
        for s in p.get("stages", []):
            status = s["status"]
            glyph = {"completed": "✅", "running": "⏳", "failed": "❌"}.get(status, "·")
            if s.get("ended_at") and s.get("started_at"):
                elapsed = (s["ended_at"] - s["started_at"]).total_seconds()
                elapsed_str = f"{elapsed:.1f}s"
            elif s.get("started_at"):
                elapsed = (now - s["started_at"]).total_seconds()
                elapsed_str = f"{elapsed:.0f}s (live)"
            else:
                elapsed_str = "—"
            rows.append({"": glyph, "stage": s["stage"], "elapsed": elapsed_str})
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)
        else:
            st.caption("No stage events for this run yet — the daemon may have just started.")


def render_platform_health() -> None:
    """Visibility-of-system-status panel — heuristic #1 of the dashboard spec.

    Rendered between the header and the Actions panel so the operator sees
    data freshness + last-update health before being tempted to push a
    button. Every row is one DB-derived signal with a glyph + color +
    short string. Two collapsible details: the per-stage breakdown of the
    last --update run, and the per-source validation roll-up."""
    header_l, header_r = st.columns([6, 1])
    header_l.subheader("Platform health")
    if header_r.button("↻ Refresh", help="Force-refresh platform health (clears the 3-min cache)", key="health_force_refresh"):
        _fetch_platform_health_cached.clear()
        st.rerun()
    try:
        h = _fetch_platform_health_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch platform health: {exc}")
        return

    # Daemon status — first thing the operator sees. Goes RED when any
    # launchd agent isn't installed; that's the single biggest "you
    # haven't set this up" signal.
    daemons = _fetch_daemon_state_cached()
    d_color, d_text, d_detail = classify_daemons(daemons)
    daemon_l, daemon_r = st.columns([6, 2])
    _render_health_row("Daemons (launchd)", d_color, d_text, row_key="daemons")
    if d_color in ("amber", "red"):
        with st.expander("Per-daemon detail", expanded=(d_color == "red")):
            for name, color, text in d_detail:
                _render_health_row(name, color, text)
            st.caption(
                "**One-button install:** `scripts/install_all_daemons.sh` "
                "(installs engine_service, data_operations, allocator). "
                "Logs: `~/Library/Logs/short-term-trading-engine/`."
            )
            not_installed = [d["name"] for d in daemons if not d["installed"]]
            if not_installed:
                if st.button(
                    "🔧 Install all daemons",
                    key="health_install_daemons",
                    help="Runs scripts/install_all_daemons.sh — sets up engine_service + data_operations + allocator launchd agents.",
                ):
                    rc, output = run_blocking_script("scripts/install_all_daemons.sh", timeout=60)
                    _render_blocking_output("Install daemons", rc, output)

    # Daemon progress — live stage-by-stage view of the most recent
    # data_operations run. Visible during in-flight runs (status='running')
    # and as a post-run summary. Built on the verified Phase 0 audit:
    # every stage writes INGESTION_START + INGESTION_COMPLETE (or _FAILED)
    # tagged with the same run_id as the daemon STARTUP.
    render_daemon_progress()

    # "Update required" banner — independent of operator timezone. The
    # signal is NYSE-close relative: if the most recently closed session
    # has bars missing AND the publication grace window is past, the
    # operator needs to act. Hidden when bars are current.
    banner = update_required_banner(h["bars"]["latest_date"])
    if banner is not None:
        severity, message = banner
        if severity == "required":
            st.error(f"🛑  **Update required.** {message}", icon=None)
            if st.button(
                "📥  Run daily update",
                key="health_banner_run_update",
                help="Dispatches scripts/run_daily_update.sh (detached, ~30-45 min).",
                use_container_width=False,
            ):
                _dispatch_health_fix("daily_update")
        else:
            st.warning(f"⏳  {message}", icon=None)

    bars_color, bars_text = classify_bars(h["bars"]["latest_date"])
    bars_text += f" — {h['bars']['recent_tickers']:,} tickers (last 5d)"
    _render_health_row("Bars (prices_daily)", bars_color, bars_text, fix_action="daily_update", row_key="bars")

    fund_color, fund_text = classify_fundamentals(h["fundamentals"]["latest_at"])
    latest_period = h["fundamentals"]["latest_period"]
    if latest_period is not None:
        fund_text += f" — latest period {latest_period.isoformat()}"
    _render_health_row("Fundamentals", fund_color, fund_text, fix_action="daily_update", row_key="fundamentals")

    ca_color, ca_text = classify_corp_actions(h["corp_actions"]["latest_at"])
    _render_health_row("Corporate actions", ca_color, ca_text, fix_action="daily_update", row_key="corp_actions")

    # Per-engine universe surfaces. Each engine derives its universe
    # differently; without this breakdown the dashboard previously only
    # told you about momentum's prescreener-driven candidate table.
    uni = h["universe"]

    # 1. Momentum — prescreener-driven (platform.universe_candidates).
    uni_color, uni_text = classify_universe(
        uni["momentum"]["latest_date"],
        uni["momentum"]["today_count"],
    )
    _render_health_row(
        "Universe — momentum (prescreener)",
        uni_color, uni_text,
        fix_action="prescreener", row_key="universe_momentum",
    )

    # 2. Vector — liquidity_tiers (tier ≤ 2); refreshed quarterly via the
    # Corwin-Schultz tier refresh. Green if count > 800 and newest
    # last_updated within 180 days; amber if within 270; red beyond.
    v = uni["vector"]
    if v["ticker_count"] < 800:
        v_color, v_text = "red", f"Only {v['ticker_count']} T1+T2 tickers (need ≥ 800)"
    elif v["newest_at"] is None:
        v_color, v_text = "red", "Never refreshed"
    else:
        age_days = (datetime.now(UTC) - v["newest_at"]).days
        if age_days <= 180:
            v_color, v_text = "green", f"{v['ticker_count']} T1+T2 tickers · refreshed {age_days}d ago"
        elif age_days <= 270:
            v_color, v_text = "amber", f"{v['ticker_count']} T1+T2 tickers · {age_days}d stale (refresh quarterly)"
        else:
            v_color, v_text = "red", f"{v['ticker_count']} T1+T2 tickers · {age_days}d stale — overdue"
    _render_health_row("Universe — vector (liquidity tiers)", v_color, v_text, row_key="universe_vector")

    # 3. Sigma + Reversion — implicit universe (prices_daily distinct
    # tickers, 90d freshness). They re-derive it every run, so this is
    # really a passthrough of the matview's coverage.
    sr = uni["sigma_reversion"]
    if sr["ticker_count"] < 1000:
        sr_color, sr_text = "red", f"Only {sr['ticker_count']} active tickers (last 90d) — universe too thin"
    elif sr["newest_bar"] is None:
        sr_color, sr_text = "red", "No bars in last 90d"
    else:
        today = datetime.now(UTC).date()
        bar_age = (today - sr["newest_bar"]).days
        if bar_age <= 2:
            sr_color = "green"
        elif bar_age <= 5:
            sr_color = "amber"
        else:
            sr_color = "red"
        sr_text = f"{sr['ticker_count']:,} active tickers · newest bar {bar_age}d old"
    _render_health_row("Universe — sigma + reversion (all_active)", sr_color, sr_text, row_key="universe_sigrev")

    # Universe-coverage integrity — silent killers. The prescreener can
    # write a row with last_close populated, but the ticker may still
    # have stale bars or zero fundamentals — both invisible to upstream
    # checks. Surface explicitly.
    cov = h["coverage"]
    cov_color, cov_text = classify_coverage_gaps(
        bar_gap_count=len(cov["missing_bars"]),
        fund_gap_count=len(cov["missing_fundamentals"]),
        tier_le_2_total=cov["tier_le_2_total"],
        tier_le_2_non_etf_count=cov.get("tier_le_2_non_etf_count"),
    )
    _render_health_row(
        "Universe coverage",
        cov_color,
        cov_text,
        fix_action="daily_update" if cov_color in ("amber", "red") else None,
        row_key="coverage",
    )
    if cov_color in ("amber", "red"):
        with st.expander("Coverage gap detail (first 20 tickers per category)", expanded=(cov_color == "red")):
            _render_ticker_list("Missing bars (last 5d)", cov["missing_bars"])
            _render_ticker_list("Missing fundamentals", cov["missing_fundamentals"])

    # Open-orders liveness — orphan pending rows older than 24h indicate
    # engine state has drifted from the broker. Source of the Sigma
    # "long-while-shorting" crash class.
    oo = h["open_orders"]
    oo_color, oo_text = classify_open_orders(
        pending_count=oo["pending_count"],
        stale_24h_count=oo["stale_24h"],
    )
    _render_health_row("Open orders", oo_color, oo_text, row_key="open_orders")
    if oo["stale_sample"]:
        with st.expander("Stale open-order detail", expanded=(oo_color == "red")):
            for r in oo["stale_sample"]:
                _render_health_row(
                    f"  {r['engine']} {r['ticker']}",
                    "red",
                    f"status={r['status']} updated_at={r['updated_at']}",
                )
            st.caption("Reconcile against Alpaca with `python scripts/check_momentum_orders.sh` (momentum) or restart the relevant scheduler.")

    run_color, run_summary, run_detail = classify_update_run(h["update_run"])
    _render_health_row("Last ops --update", run_color, run_summary, fix_action="daily_update", row_key="update_run")
    with st.expander("Stage-by-stage detail of last --update run", expanded=(run_color == "red")):
        if not run_detail:
            st.caption("No recent run found in platform.application_log.")
        else:
            # First red stage in dependency order — anything later that's red
            # is likely a *consequence*, not an independent failure. Show
            # operator a hint instead of letting them click downstream
            # buttons that will just fail again.
            first_red_idx: int | None = None
            for i, (_, color, _) in enumerate(run_detail):
                if color == "red":
                    first_red_idx = i
                    break

            job_busy = _detached_job_is_live()
            for i, (stage, color, text) in enumerate(run_detail):
                is_downstream_of_red = (
                    first_red_idx is not None and i > first_red_idx and color == "red"
                )
                _render_stage_detail_row(
                    stage,
                    color,
                    text,
                    job_busy=job_busy,
                    is_downstream_of_red=is_downstream_of_red,
                )

    # Validation failures aren't one-click fixable (data quality is a
    # symptom, not a switch). Show the roll-up without a Fix button.
    # Cross-table integrity (was scripts/audit_all_tables.sh — now inline)
    cr_color, cr_summary, cr_detail = classify_cross_table_audit(h["cross_ref"])
    _render_health_row(
        "Cross-table integrity",
        cr_color,
        cr_summary,
        row_key="cross_ref",
    )
    if cr_color != "green":
        with st.expander("Per-table cross-reference detail", expanded=True):
            for label, color, text in cr_detail:
                _render_health_row(label, color, text)

    val_color, val_summary, val_detail = classify_validation(h["validation"])
    _render_health_row(
        "Data validation",
        val_color,
        val_summary,
        fix_action="validation_rerun",
        row_key="validation",
    )
    if val_detail:
        with st.expander("Per-source validation detail (latest run)", expanded=(val_color == "red")):
            # Build a lookup of latest-run notes keyed by source for the
            # per-row drill-down. Each failed source shows the actual
            # offending tickers + reasons so the operator can act.
            notes_by_source = {r["source"]: r.get("notes") for r in h["validation"]}
            for source, color, text in val_detail:
                _render_health_row(source, color, text)
                if color != "green":
                    notes = notes_by_source.get(f"validation.{source}") or notes_by_source.get(source)
                    _render_validation_failure_detail(source, notes)

    # Catalyst events — vector engine's earnings-beat source.
    cat_color, cat_summary = classify_catalyst(h["catalyst"])
    _render_health_row("Catalyst events (vector)", cat_color, cat_summary, row_key="catalyst")

    # Forensics — open triggers from per-engine AAR scans.
    f_color, f_summary = classify_forensics(h["forensics"])
    _render_health_row("Forensics (open triggers)", f_color, f_summary, row_key="forensics")
    recent = h["forensics"].get("recent") or []
    if recent:
        with st.expander(f"Open forensics triggers — {len(recent)}", expanded=(f_color == "red")):
            st.caption(
                "Each row was emitted by `tpcore.forensics`. Open the dossier "
                "to write a Sprint postmortem; click **Mark resolved** when the "
                "fix ships."
            )
            for t in recent:
                payload = t["payload"] or {}
                fired_at = t["fired_at"]
                age = (datetime.now(UTC) - (fired_at if fired_at.tzinfo else fired_at.replace(tzinfo=UTC))).days
                cols = st.columns([3, 1, 1, 1])
                cols[0].markdown(
                    f"**{t['kind']}** · engine `{payload.get('engine', '?')}` · "
                    f"`{payload.get('trade_id', '—')}` · fired {age}d ago"
                )
                dossier = payload.get("dossier_path")
                if dossier:
                    cols[1].caption(f"Dossier: `{dossier}`")
                else:
                    cols[1].caption("(no dossier)")
                if cols[2].button("Mark resolved", key=f"resolve_{t['id']}"):
                    run_async(_mark_forensics_resolved(t["id"]))
                    _fetch_platform_health_cached.clear()
                    st.rerun()


async def _mark_forensics_resolved(trigger_id: int) -> None:
    """Set ``resolved_at = NOW()`` on the trigger row."""
    pool = await build_asyncpg_pool(_db_url(), max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE platform.forensics_triggers SET resolved_at = now() WHERE id = $1",
                trigger_id,
            )
    finally:
        await pool.close()


def render_escalation_audit() -> None:
    """Escalation & integrity audit — the console's view of the
    Escalation & Hardening Ladder (rung-1/3) + Data Supervisor +
    auditheal layer. Read-only render of existing SoT (#189)."""
    hl, hr = st.columns([6, 1])
    hl.subheader("Escalation & data-integrity audit")
    if hr.button("↻ Refresh", help="Force-refresh (clears the 3-min cache)",
                 key="esc_force_refresh"):
        _fetch_escalation_state_cached.clear()
        st.rerun()
    try:
        e = _fetch_escalation_state_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch escalation state: {exc}")
        return

    for label, key, rows, fn, detail_title in (
        ("Source holds (Data Supervisor)", "src_holds",
         e["source_holds"], classify_source_holds, "Held sources"),
        ("Undispositioned escalations (rung-3)", "undisp",
         e["undispositioned"], classify_undispositioned,
         "Undispositioned — disposition each"),
        ("Cross-table audit (auditheal)", "ct_audit",
         e["cross_table_audit"], classify_cross_table_audit,
         "Per cross-table check"),
        ("Recent escalations (7d)", "recent_esc",
         e["recent_escalations"], classify_recent_escalations,
         "Recent escalations"),
    ):
        c, s, d = fn(rows)
        _render_health_row(label, c, s, row_key=key)
        if c != "green" and d:
            with st.expander(detail_title, expanded=(c == "red")):
                for dl, dc, dt in d:
                    _render_health_row(dl, dc, dt)


async def _fetch_defect_register() -> list:
    """Read-only consolidated defect SoT for the panel. REUSES
    ops.defect_register.consolidated_defects VERBATIM (it composes both
    Escalation & Hardening Ladders + the review-found anti-join open-set
    — the register, the weekly digest and this panel cannot disagree
    because they call the same functions). Recomputes no predicate;
    issues no escalation query of its own (the register owns that
    boundary). Same fetch idiom as _fetch_escalation_state."""
    from ops.defect_register import consolidated_defects

    pool = await build_asyncpg_pool(_db_url(), max_size=4)
    try:
        return list(await consolidated_defects(pool))
    finally:
        await pool.close()


@st.cache_data(ttl=180, show_spinner=False)
def _fetch_defect_register_cached() -> list:
    """Sync cache wrapper (same idiom/TTL as
    _fetch_escalation_state_cached)."""
    return run_async(_fetch_defect_register())


def render_defect_register() -> None:
    """Consolidated Defect Register — the console's view of #254's
    derived read-model (escalation-class from both Ladders + the
    review-found-defect class). Read-only render of existing SoT: NO
    recompute, NO write button (spec §5 OUT). Same render pattern as
    render_escalation_audit."""
    hl, hr = st.columns([6, 1])
    hl.subheader("Consolidated defect register")
    if hr.button("↻ Refresh", help="Force-refresh (clears the 3-min cache)",
                 key="defreg_force_refresh"):
        _fetch_defect_register_cached.clear()
        st.rerun()
    try:
        rows = _fetch_defect_register_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch defect register: {exc}")
        return

    c, s, d = classify_defect_register(rows)
    _render_health_row("Defect register (#254)", c, s, row_key="defreg")
    if c != "green" and d:
        with st.expander("Defects — `python -m ops.defect_register list`",
                         expanded=(c == "red")):
            for dl, dc, dt in d:
                _render_health_row(dl, dc, dt)


def render_tip_sheet() -> None:
    """Phase 1 tip-sheet view — operator's private research tool.

    Mimics an old-school brokerage research note: serif heading, a
    credibility "imprint" stamp, a ranked recommendations table with
    target prices, and a disclaimer. Source data is the same that
    ``scripts/generate_tip_sheet.py`` produces; this view just renders
    it for the dashboard.

    Phase 2 ("shareable to outsiders") is gated and not yet earned —
    see ``docs/superpowers/specs/2026-05-13-tip-sheet-plan.md``. The
    gate banner at the top of this view tells the operator exactly
    what's missing.
    """
    import pandas as pd

    st.markdown(
        "<div style='font-family: \"Times New Roman\", Georgia, serif; "
        "border:2px solid #444; padding:18px; background:#faf7ee; "
        "color:#222;'>"
        "<div style='text-align:center; font-size:0.85em; "
        "letter-spacing:0.3em; color:#777;'>DAILY RESEARCH NOTE</div>"
        "<h1 style='font-family: \"Times New Roman\", Georgia, serif; "
        "text-align:center; font-size:2.4em; margin:6px 0 4px 0; "
        "font-variant:small-caps; letter-spacing:0.08em; color:#222;'>"
        "The Momentum Tip Sheet</h1>"
        f"<div style='text-align:center; font-size:0.95em; color:#555;'>"
        f"For market opening — {datetime.now(UTC).astimezone().strftime('%A, %B %d, %Y')}</div>"
        "<hr style='border-top:1px solid #999; margin:14px 0;'/>"
        "<div style='font-size:0.9em; color:#444;'>"
        "<i>Long-only US-equities momentum strategy. Holdings ranked by trailing "
        "12-month return (skipping the most recent month). Top-decile bought "
        "equal-weight, held to the next monthly rebalance.</i></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    # ── Phase 2 publication-readiness gate ─────────────────────────────
    try:
        cred = _fetch_credibility_cached().get("momentum")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read credibility: {exc}")
        return

    cred_score = float(cred.score) if cred is not None else 0.0
    cred_passed = cred is not None and cred_score >= MIN_LIVE_SCORE
    try:
        trades = _fetch_trades_cached(days=180)
        n_paper_trades = len([t for t in trades if t.get("engine") == "momentum"])
    except Exception:  # noqa: BLE001
        n_paper_trades = 0

    PHASE2_TRADE_THRESHOLD = 30
    phase2_blockers = []
    if not cred_passed:
        phase2_blockers.append(
            f"credibility {cred_score:.0f}/100 (need ≥ {MIN_LIVE_SCORE})"
        )
    if n_paper_trades < PHASE2_TRADE_THRESHOLD:
        phase2_blockers.append(
            f"{n_paper_trades} paper trade(s) documented (need ≥ {PHASE2_TRADE_THRESHOLD})"
        )
    # Disclaimer review — covered by the comprehensive footer below
    # (no personalized advice, past performance, risk of loss, not an
    # RIA, no solicitation, source disclosure, data cutoff, accuracy
    # disclaimer, hold harmless — all present). Reviewed 2026-05-14.
    # If you want a registered attorney's signature, replace this
    # flag with their attestation; the gate logic stays the same.

    if phase2_blockers:
        st.warning(
            "**Not yet shareable to outsiders.** Phase 2 publication "
            "(per `docs/superpowers/specs/2026-05-13-tip-sheet-plan.md`) "
            "requires:\n\n"
            + "\n".join(f"- {b}" for b in phase2_blockers)
        )
    else:
        st.success("✓ All Phase 2 gates cleared — ready to publish.")

    # ── Per-engine recommendations ──────────────────────────────────────
    # Each engine gets its own section. The strategy descriptions are
    # the "broker's notes" — what an outsider needs to understand the
    # call before deciding whether to act on it.
    engine_specs = [
        {
            "engine": "momentum",
            "title": "Momentum — Long-only Cross-Sectional",
            "score_label": "12-1 Return",
            "extras": "Liquidity",
            "notes": (
                "Rank a liquid US-equities universe by trailing 12-month "
                "return (skipping the most recent month). Buy the top "
                "decile equal-weighted; hold to the next monthly rebalance. "
                "Backtest premium documented since Jegadeesh & Titman (1993)."
            ),
        },
        {
            "engine": "sigma",
            "title": "Sigma — Range-Scalping (Bollinger Bands)",
            "score_label": "Setup Score",
            "extras": "Direction",
            "notes": (
                "Enter on lower-Bollinger-band touch with mean-reversion "
                "filters. Two-tier OCO bracket: Tier 1 exits at the mid-"
                "band (take profit close), Tier 2 holds for the far target "
                "(upper band). Hard stop below the entry."
            ),
        },
        {
            "engine": "reversion",
            "title": "Reversion — Mean Reversion + Earnings Quality",
            "score_label": "Setup Score",
            "extras": "Direction",
            "notes": (
                "Trade ≥ 3σ deviations from the 20-day moving average; the "
                "earnings-quality gate filters fundamentally weak names "
                "where reversion is unlikely. Bracket exits at 20-MA (Tier "
                "1) and 50-MA (Tier 2)."
            ),
        },
        {
            "engine": "vector",
            "title": "Vector — Catalyst-Driven Swing",
            "score_label": "Setup Score",
            "extras": "Direction",
            "notes": (
                "Long-only on liquid (T1+T2) names that pair an earnings "
                "catalyst with cheap fundamentals (P/B + D/E) and a clean "
                "technical setup. Single-tier bracket: TP at target, stop "
                "below entry."
            ),
        },
    ]

    for spec in engine_specs:
        eng = spec["engine"]
        st.markdown(f"##### {spec['title']}")
        st.caption(spec["notes"])
        try:
            recs = _fetch_today_recommendations_cached(eng)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not fetch {eng} recommendations: {exc}")
            continue

        if not recs:
            st.info(
                f"No {eng} candidates from today's scan — either the "
                "scheduler hasn't fired in the last 36h or no setups passed "
                "the filters. Check the Health tab for scheduler status."
            )
            st.markdown("")
            continue

        df = pd.DataFrame(recs)
        df["Rank"] = range(1, len(df) + 1)
        df["Score"] = df["score"].astype(float).round(3)
        df["Last Close"] = df["last_close"].astype(float).round(2)
        if eng == "momentum":
            df["Tier"] = df.get("tier", 0).astype(int).apply(lambda t: f"T{t}")
            df = df[["Rank", "ticker", "Score", "Last Close", "Tier"]]
            df.columns = ["Rank", "Symbol", spec["score_label"], "Last Close", spec["extras"]]
        else:
            df["Direction"] = df.get("direction", "LONG")
            df = df[["Rank", "ticker", "Score", "Last Close", "Direction"]]
            df.columns = ["Rank", "Symbol", spec["score_label"], "Last Close", spec["extras"]]
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                spec["score_label"]: st.column_config.NumberColumn(format="%+.3f"),
                "Last Close": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        # Per-engine credibility tag
        engine_cred = (cred if eng == "momentum"
                       else _fetch_credibility_cached().get(eng))
        if engine_cred is not None:
            score_val = float(engine_cred.score)
            badge = "🟢 cleared" if score_val >= MIN_LIVE_SCORE else "🔴 below gate"
            st.caption(f"Credibility: {score_val:.0f}/100 — {badge}  ·  {len(recs)} candidates")
        else:
            st.caption(f"{len(recs)} candidates · credibility not yet measured")
        st.markdown("")

    # ── Disclaimer ─────────────────────────────────────────────────────
    #
    # Reviewed under a legal lens 2026-05-14. Covers the elements a
    # registered investment-advisor research note carries under SEC
    # Marketing Rule + general anti-fraud considerations:
    #
    #   1. No personalized advice (general info only)
    #   2. Past performance ≠ future results
    #   3. Risk of loss
    #   4. Author not a registered investment advisor
    #   5. No solicitation
    #   6. Source disclosure (automated tool, not human research)
    #   7. Data cutoff + update cadence
    #   8. No warranty of accuracy
    #   9. Hold harmless / no liability
    #   10. Reader-discretion advisory
    #
    # If actual attorney review is required for a specific publication,
    # replace this block with the attorney's reviewed version. The
    # Phase 2 gate logic accepts whatever is here.
    st.markdown("")
    st.markdown(
        "<div style='font-family: \"Times New Roman\", Georgia, serif; "
        "border-top:1px solid #999; padding-top:12px; font-size:0.75em; "
        "color:#555; line-height:1.55;'>"
        "<b>IMPORTANT DISCLAIMER &amp; DISCLOSURES — READ BEFORE USING.</b>"
        "<br/><br/>"
        "<b>1. Not investment advice.</b> The content of this report is "
        "general market commentary generated by an automated system. It is "
        "NOT individualized investment advice, NOT a recommendation to buy, "
        "sell, hold, or trade any security, and NOT a solicitation of any "
        "offer to transact."
        "<br/><br/>"
        "<b>2. Not a registered investment advisor.</b> The author and "
        "platform are NOT a registered investment advisor (RIA), broker-"
        "dealer, or other financial professional. No fiduciary relationship "
        "exists between the author and any reader."
        "<br/><br/>"
        "<b>3. Past performance does not predict future results.</b> Any "
        "backtest, paper-trade, or live track-record figures shown reflect "
        "historical conditions under specific assumptions that may not "
        "recur. Forward returns may differ materially, including total "
        "loss of capital."
        "<br/><br/>"
        "<b>4. Risk of loss.</b> All investing carries the risk of "
        "permanent capital loss. Strategies described here have not been "
        "validated for live use; the platform's credibility gate exists "
        "precisely because not every strategy has earned the right to be "
        "acted on."
        "<br/><br/>"
        "<b>5. Automated source.</b> This report is produced by an "
        "algorithmic process. The author does not independently verify "
        "every entry. Data is sourced from third-party vendors (Alpaca, "
        "FMP) whose accuracy is not guaranteed."
        "<br/><br/>"
        "<b>6. Data cutoff.</b> Information is current as of the "
        f"<b>generated</b> timestamp at the top of the report ({datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}). "
        "Prices and rankings change continuously after publication."
        "<br/><br/>"
        "<b>7. No warranty.</b> Information is provided <i>as-is</i> with "
        "no representation or warranty of accuracy, completeness, "
        "timeliness, or fitness for any particular purpose."
        "<br/><br/>"
        "<b>8. No liability.</b> The author, the platform, and any "
        "affiliated parties accept NO liability for any losses arising "
        "from use of or reliance on this report. By reading, you "
        "acknowledge sole responsibility for your own investment "
        "decisions."
        "<br/><br/>"
        "<b>9. Forward-looking statements.</b> Any ranking or score is a "
        "characterization of the recent past, not a prediction. "
        "Forward-looking inferences are the reader's alone."
        "<br/><br/>"
        "<b>10. Consult a professional.</b> Before acting on any "
        "information shown, consult a licensed financial advisor, "
        "registered investment advisor, and/or tax professional who can "
        "evaluate your specific situation."
        "</div>",
        unsafe_allow_html=True,
    )


def render_actions():
    st.subheader("Actions")
    _render_process_status_inline()

    try:
        last = _fetch_last_run_timestamps_cached()
    except Exception:  # noqa: BLE001
        last = {}

    # ── Daily — every market day ────────────────────────────────────────────
    st.markdown("##### Daily — every market day after the close")
    st.caption(
        "**Runs automatically** via the `com.michael.trading.data-operations` "
        "launchd daemon at 21:30 UTC (≈ 16:30 ET, after market close). The "
        "button below is a manual override — use only to re-run after a "
        "failure or out-of-band. Workflow: 13-stage `ops.py --update` (bars "
        "→ corp actions → reconcile → coverage_fill → cross_ref_cleanup "
        "→ fundamentals → tier_refresh → classify_tickers → earnings_refresh "
        "→ sec_filings → validation → universe prescreener → universe "
        "simulation) → cross-table audit → validation re-confirm → compress "
        "backfill CSVs → engine sweep. "
        "**Check the Platform-health panel above for per-stage status.**"
    )
    c1, c2 = st.columns([1, 4])
    if c1.button(
        "📥  Run data-operations",
        help="Runs scripts/run_data_operations.sh (full one-button workflow). Detached (~30-45 min).",
        use_container_width=True,
    ):
        pid, logfile = run_detached_script("scripts/run_data_operations.sh")
        st.session_state["detached_job"] = {
            "name": "Data operations",
            "pid": pid,
            "logfile": logfile,
            "started_at": time.time(),
        }
        st.success(f"Launched (pid {pid}); logfile: {logfile}")
        st.rerun()
    with c2:
        secs = _age_seconds(last.get("daily_update"))
        # Daily: fresh < 18h, stale > 36h.
        color = _color_for_age(secs, 18 * 3600, 36 * 3600) if secs is not None else "#888"
        text, _ = _fmt_age(last.get("daily_update"))
        _render_status_line("Last completed:", text, status_color=color)

    # ── Monthly — Momentum rebalance ────────────────────────────────────────
    st.markdown("##### Monthly — Momentum rebalance (first NYSE session of the month)")
    st.caption(
        "**Runs automatically** as part of the daily data-operations run: the momentum "
        "scheduler fires every day but only emits orders on the 1st NYSE session "
        "of each calendar month (no-op otherwise). The button below is a manual "
        "override — re-scores against today's data and submits a fresh batch."
    )
    c1, c2 = st.columns([1, 4])
    if c1.button(
        "🔄  Force-rebalance Momentum",
        help="Cancels stale orders, re-scores against today's data, submits a fresh batch. Typed-confirm REBALANCE.",
        use_container_width=True,
    ):
        st.session_state["pending_confirm"] = {
            "action": "force_rebalance",
            "script": "scripts/run_momentum_kickoff.sh",
            "phrase": "REBALANCE",
            "description": "About to recompute and submit ~50 orders against the Alpaca paper account.",
        }
    with c2:
        secs = _age_seconds(last.get("force_rebalance"))
        # Monthly: fresh < 35 days (one rebalance cycle + buffer), stale > 45 days.
        color = _color_for_age(secs, 35 * 86400, 45 * 86400) if secs is not None else "#888"
        text, _ = _fmt_age(last.get("force_rebalance"))
        _render_status_line("Last signal emitted:", text, status_color=color)

    # ── Periodic — after parameter or code changes ──────────────────────────
    st.markdown("##### Periodic — after parameter or code changes")
    st.caption(
        "**Refresh credibility** re-runs the parameter search and persists the rubric "
        "row to `data_quality_log`. **Smoke test** runs the full platform "
        "pytest suite + ruff + a dry-run of every engine + forensics + allocator + tip sheet."
    )
    c1, c2, c3 = st.columns([1, 1, 3])
    if c1.button(
        "📊  Refresh credibility",
        help="Re-runs the momentum parameter search (~5 min) and persists the rubric row.",
        use_container_width=True,
    ):
        with st.spinner("Running momentum search (this takes ~5 min)..."):
            rc, output = run_blocking_script("scripts/run_momentum_search.sh", timeout=900)
        _render_blocking_output("Refresh credibility", rc, output)
    if c2.button(
        "🧪  Smoke test",
        help="Runs all momentum unit tests + dry-run scheduler + tip-sheet render. Canonical 'did anything break' gate.",
        use_container_width=True,
    ):
        with st.spinner("Running smoke test..."):
            rc, output = run_blocking_script("scripts/run_smoke_test.sh", timeout=600)
        _render_blocking_output("Smoke test", rc, output)
    with c3:
        secs = _age_seconds(last.get("credibility_refresh"))
        # Periodic: fresh < 7 days, stale > 30 days.
        color = _color_for_age(secs, 7 * 86400, 30 * 86400) if secs is not None else "#888"
        text, _ = _fmt_age(last.get("credibility_refresh"))
        _render_status_line("Last credibility rubric write:", text, status_color=color)

    # ── Quarterly — liquidity-tier refresh ──────────────────────────────────
    st.markdown("##### Quarterly — liquidity-tier refresh")
    st.caption(
        "Re-runs the Corwin-Schultz bootstrap to refresh spread estimates in "
        "`platform.spread_observations` → `liquidity_tiers`. Cost model depends on this."
    )
    c1, c2 = st.columns([1, 4])
    if c1.button(
        "🧪  Refresh liquidity tiers",
        help="Re-runs Corwin-Schultz bootstrap + re-aggregates platform.liquidity_tiers. Long-running (~20-30 min); detached.",
        use_container_width=True,
    ):
        pid, logfile = run_detached_script("scripts/run_tier_refresh.sh")
        st.session_state["detached_job"] = {
            "name": "Tier refresh",
            "pid": pid,
            "logfile": logfile,
            "started_at": time.time(),
        }
        st.success(f"Launched (pid {pid}); logfile: {logfile}")
        st.rerun()
    with c2:
        secs = _age_seconds(last.get("tier_refresh"))
        # Quarterly: fresh < 90 days, stale > 180 days.
        color = _color_for_age(secs, 90 * 86400, 180 * 86400) if secs is not None else "#888"
        text, _ = _fmt_age(last.get("tier_refresh"))
        _render_status_line("Last spread observation:", text, status_color=color)

    # ── Corrective — emergency-only ─────────────────────────────────────────
    st.markdown("##### Corrective — emergency only")
    st.caption(
        "Rarely needed. Use after a failed rebalance to clean stuck orders, or before "
        "manually re-running a kickoff against fresh data."
    )
    c1, _ = st.columns([1, 4])
    if c1.button(
        "🛑  Cancel open orders",
        help="Cancels all open `mo_*` orders at Alpaca. Typed-confirm CANCEL.",
        use_container_width=True,
    ):
        st.session_state["pending_confirm"] = {
            "action": "cancel_orders",
            "script": None,
            "phrase": "CANCEL",
            "description": "About to cancel all open momentum orders at Alpaca paper.",
        }


def render_confirm_modal():
    """Typed-confirmation modal for destructive actions. Generic Yes/No invites
    muscle-memory click-through; typing a specific phrase forces a pause."""
    pending = st.session_state.get("pending_confirm")
    if not pending:
        return
    with st.container():
        st.warning(f"⚠️  Confirmation required — {pending['action']}")
        st.write(pending["description"])
        st.write(f"Type `{pending['phrase']}` to confirm:")
        typed = st.text_input("Confirmation phrase", key="confirm_input", label_visibility="collapsed")
        c1, c2 = st.columns([1, 1])
        if c1.button("Cancel", key="confirm_cancel"):
            st.session_state.pop("pending_confirm", None)
            st.rerun()
        if c2.button("Confirm", key="confirm_submit", type="primary"):
            if typed.strip() == pending["phrase"]:
                _execute_confirmed_action(pending)
                st.session_state.pop("pending_confirm", None)
                st.rerun()
            else:
                st.error(f"Phrase must match exactly. Expected `{pending['phrase']}`, got `{typed}`.")


def _execute_confirmed_action(pending: dict):
    if pending["action"] == "force_rebalance":
        with st.spinner("Running momentum kickoff..."):
            rc, output = run_blocking_script(pending["script"], timeout=600)
        _render_blocking_output("Force-rebalance", rc, output)
    elif pending["action"] == "cancel_orders":
        with st.spinner("Cancelling open orders..."):
            try:
                n = run_async(_cancel_open_momentum_orders())
                st.success(f"Cancelled {n} open momentum orders.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Cancel failed: {exc}")


async def _cancel_open_momentum_orders() -> int:
    """Cancel any `mo_*` open orders at Alpaca."""
    broker = AlpacaPaperBrokerAdapter()
    orders = await broker.list_recent_orders(limit=500)
    open_statuses = {"new", "partially_filled", "accepted", "pending_new"}
    n = 0
    for o in orders:
        if not (o.client_order_id or "").startswith("mo_"):
            continue
        status_val = getattr(o.status, "value", str(o.status)).lower()
        if status_val not in open_statuses:
            continue
        if not o.broker_order_id:
            continue
        try:
            await broker.cancel_order(o.broker_order_id)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def _render_blocking_output(action: str, rc: int, output: str):
    """Distinct treatment for non-zero exit — red border, return code prominent."""
    if rc == 0:
        st.success(f"✓  {action} completed (rc=0)")
    else:
        st.error(f"✗  {action} failed (rc={rc})")
    with st.expander("Output", expanded=(rc != 0)):
        st.code(output, language="bash")


def render_detached_job_panel():
    """Heartbeat panel for a detached long-running job. Status by stale-seconds."""
    job = st.session_state.get("detached_job")
    if not job:
        return
    status = detached_job_status(job["pid"], job["logfile"])
    elapsed = time.time() - job["started_at"]
    stale = status.get("stale_seconds")

    if not status["alive"]:
        if status["logfile_exists"]:
            st.success(f"✓  {job['name']} finished (pid {job['pid']}, elapsed {elapsed / 60:.1f} min)")
        else:
            st.error(f"✗  {job['name']} pid {job['pid']} not found and no logfile")
        with st.expander("Last 30 log lines", expanded=False):
            st.code(status["tail"] or "(no output captured)", language="bash")
        c1, _ = st.columns([1, 5])
        if c1.button("Dismiss", key="dismiss_job"):
            st.session_state.pop("detached_job", None)
            st.rerun()
        return

    # Alive — heartbeat indicator
    if stale is None:
        heartbeat = "—"
        color = "#666"
    elif stale < 300:  # 5 min
        heartbeat = f"updated {stale:.0f}s ago"
        color = "#0a8a3a"
    elif stale < 900:  # 15 min
        heartbeat = f"⚠ no output for {stale / 60:.1f} min"
        color = "#d68800"
    else:
        heartbeat = f"⚠ STALE — no output for {stale / 60:.1f} min"
        color = "#c92a2a"

    st.markdown(
        f"<div style='padding:10px;border-left:4px solid {color};background:#f8f8f8;'>"
        f"<strong>Running</strong> — {job['name']}  "
        f"<span style='color:#666;'>pid {job['pid']} · elapsed {elapsed / 60:.1f} min · "
        f"<span style='color:{color};'>{heartbeat}</span></span></div>",
        unsafe_allow_html=True,
    )
    with st.expander("Last 30 log lines", expanded=True):
        st.code(status["tail"] or "(waiting for first output)", language="bash")


# ────────────────────────────────────────────────────────────────────────────
# Page assembly
# ────────────────────────────────────────────────────────────────────────────


def render_settings_panel() -> None:
    """Phase 5 — collapsible at top with auto-refresh toggle.

    Auto-refresh is opt-in (off by default). Per the spec: never lower
    than 30s — Streamlit + custom components leak browser memory at
    higher refresh rates."""
    with st.expander("⚙️  Settings", expanded=False):
        if _AUTOREFRESH_AVAILABLE:
            on = st.checkbox(
                "Auto-refresh",
                value=st.session_state.get("autorefresh_on", False),
                key="autorefresh_on",
                help="Re-runs the dashboard every N seconds. Off by default (manual refresh is the primary path).",
            )
            interval = st.selectbox(
                "Interval (seconds)",
                options=[30, 60, 120, 300],
                index=1,
                key="autorefresh_interval",
                disabled=not on,
            )
            if on:
                st_autorefresh(interval=interval * 1000, key="autorefresh_counter")
                st.caption(f"Refreshing every {interval}s")
        else:
            st.caption(
                "Auto-refresh requires `streamlit-autorefresh` — "
                "`pip install streamlit-autorefresh` to enable."
            )
        st.caption(
            "Keyboard: press **r** to refresh, **Esc** to dismiss modals "
            "(both work when the page is focused)."
        )


def _inject_keyboard_shortcuts() -> None:
    """Phase 5 — minimal JS injection for `r` to refresh.

    Streamlit doesn't expose first-class keyboard shortcuts; a tiny JS
    listener on the parent window is the pragmatic path. Bound only when
    no text input is focused so it doesn't fire while the operator is
    typing into a confirmation modal."""
    st.markdown(
        """
        <script>
        (function() {
            if (window._dashboard_shortcuts_installed) return;
            window._dashboard_shortcuts_installed = true;
            window.parent.document.addEventListener('keydown', function(e) {
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
                if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey) {
                    // Click the topmost Streamlit toolbar's reload — emulates F5.
                    window.location.reload();
                }
            });
        })();
        </script>
        """,
        unsafe_allow_html=True,
    )


def _inject_tab_styles() -> None:
    """Make Streamlit's default tabs bigger — the stock 14px font + tight
    padding makes the four-tab cluster a small click target on a wide
    monitor. Bumping to 1.15rem with extra horizontal padding turns them
    into genuine Fitts'-Law-friendly targets."""
    st.markdown(
        """
        <style>
        button[data-baseweb="tab"] {
            font-size: 1.6rem !important;
            padding: 1rem 2rem !important;
            font-weight: 500 !important;
            min-height: 3.5rem !important;
        }
        button[data-baseweb="tab"] p,
        button[data-baseweb="tab"] div {
            font-size: 1.6rem !important;
            line-height: 1.4 !important;
        }
        div[data-baseweb="tab-list"] {
            gap: 0.75rem;
            border-bottom: 2px solid rgba(128,128,128,0.2);
        }
        button[data-baseweb="tab"][aria-selected="true"] {
            font-weight: 700 !important;
            background: rgba(100, 149, 237, 0.1);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(
        page_title="Trading Engine — Operator Dashboard",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    _inject_keyboard_shortcuts()
    _inject_tab_styles()
    render_settings_panel()
    render_header()

    # Manual refresh button — primary path. Auto-refresh is opt-in in Settings.
    c1, _ = st.columns([1, 8])
    if c1.button("🔁  Refresh", help="Re-fetch all panels  (keyboard: r)"):
        st.rerun()

    # Confirm modal + detached job panel are rendered OUTSIDE the tabs so
    # they remain visible when the operator switches tabs — a job
    # launched from one tab should still be visible from another.
    render_confirm_modal()
    render_detached_job_panel()

    # Four-tab layout. Health is the default — heuristic #1
    # (Visibility of system status): operator sees what's stale before
    # they're tempted to push a button.
    tab_health, tab_trading, tab_research, tab_tipsheet, tab_actions = st.tabs(
        [
            "🩺 Health",
            "💹 Trading",
            "🔬 Research",
            "📜 Tip Sheet",
            "⚡ Actions",
        ]
    )

    with tab_health:
        render_platform_health()
        st.divider()
        render_escalation_audit()
        st.divider()
        render_defect_register()

    with tab_trading:
        selected_ticker = render_holdings()
        # Ticker detail — only renders when a row is selected. Stays
        # inside the Trading tab so the row-click → chart flow is local.
        if selected_ticker:
            st.divider()
            render_ticker_detail(selected_ticker)
        st.divider()
        render_recent_orders()
        st.divider()
        render_equity_curve()

    with tab_research:
        render_credibility_scorecards()
        st.divider()
        render_recent_activity()

    with tab_tipsheet:
        render_tip_sheet()

    with tab_actions:
        render_actions()

    st.divider()
    st.caption(
        "Local research console — not financial advice. Dashboard dispatches "
        "existing scripts in `scripts/`; no business logic is duplicated here. "
        "See `docs/superpowers/specs/2026-05-13-operator-dashboard.md` for the design."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
