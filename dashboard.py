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
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import streamlit as st

# Reuse existing tip-sheet query helpers + broker adapter — same data layer,
# same shape. Dashboard adds zero new business logic.
from dashboard_components.charts import render_ticker_chart
from scripts.generate_tip_sheet import (
    fetch_engine_holdings,
    fetch_recent_trades,
)
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool


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
        "unrealized_pl": sum(
            float(p.unrealized_pl) for p in positions if p.unrealized_pl is not None
        ),
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
                ticker, days,
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
                ticker, days,
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
                out.append({
                    "entry_date": e,
                    "entry_price": entry_px,
                    "exit_date": x,
                    "exit_price": exit_px,
                    "pnl_pct": pnl_pct,
                    "qty": qty,
                    "exit_reason": data.get("exit_reason", ""),
                })
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
                "entry_date": o.filled_at.date() if o.filled_at else o.submitted_at.date() if o.submitted_at else None,
                "entry_price": float(o.avg_fill_price) if o.avg_fill_price else float(pos.avg_entry_price),
                "qty": int(pos.qty),
            }
    # Fallback: no order history; use today's date with avg_entry_price.
    return {
        "entry_date": datetime.now(UTC).date(),
        "entry_price": float(pos.avg_entry_price) if pos.avg_entry_price else 0.0,
        "qty": int(pos.qty),
    }


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


def run_detached_script(script: str) -> tuple[int, str]:
    """Pattern B — long script (≥10 min), detached so Streamlit recycles don't
    SIGTERM it. Returns (pid, logfile_path). UI tails the logfile on each
    rerun until the process exits."""
    script_path = REPO_ROOT / script
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    logfile = LOG_DIR / f"dashboard_{Path(script).stem}_{ts}.log"
    proc = subprocess.Popen(
        [str(script_path)],
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


def render_header():
    st.title("Trading Engine — Operator Dashboard")
    try:
        state = run_async(_fetch_account_state())
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
        f"{glyph} ${pl:+,.2f}  ({pl_pct*100:+.3f}%)</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Data as of {state['fetched_at'].strftime('%H:%M:%S UTC')}")
    return state


def render_holdings():
    st.subheader("Currently holding — Momentum")
    try:
        holdings = run_async(_fetch_holdings_for_engine("momentum"))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch holdings: {exc}")
        return None
    if not holdings:
        st.info("No open Momentum positions.")
        return None
    import pandas as pd
    df = pd.DataFrame(holdings)
    df["pnl_pct"] = df["unrealized_pl_pct"] * 100.0
    df = df[["ticker", "qty", "entry_price", "current_price", "market_value", "unrealized_pl", "pnl_pct"]]
    df.columns = ["Ticker", "Qty", "Entry", "Current", "Market Value", "P&L $", "P&L %"]
    # Single-row selection — clicking a row sets selected ticker for the
    # ticker-detail panel below. on_select='rerun' triggers a script rerun
    # so the chart panel picks up the selection.
    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="holdings_table",
        column_config={
            "Entry": st.column_config.NumberColumn(format="$%.2f"),
            "Current": st.column_config.NumberColumn(format="$%.2f"),
            "Market Value": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )
    st.caption(f"Data as of {datetime.now(UTC).strftime('%H:%M:%S UTC')}  ·  Click a row to see its price chart")
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        return str(df.iloc[selected_rows[0]]["Ticker"])
    return None


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
        ticker=ticker, ohlc=ohlc_df,
        closed_trades=closed, active_entries=active_entries,
    )
    # Compact metadata below the chart.
    if closed:
        st.caption(
            f"{len(closed)} closed trades in last 365d  ·  "
            f"latest exit: {closed[0]['exit_date']}  "
            f"({closed[0]['pnl_pct']*100:+.2f}%)"
        )
    if active:
        st.caption(
            f"Currently held: {active['qty']} sh @ ${active['entry_price']:.2f} "
            f"opened {active['entry_date']}"
        )


def render_equity_curve():
    st.subheader("Equity curve — last 60 days")
    try:
        history = run_async(_fetch_equity_history(days=60))
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


def render_actions():
    st.subheader("Actions")
    _render_process_status_inline()
    st.caption(
        "Recommended order: **Step 1** (daily update) → **Step 4** (smoke test) → **Step 2** "
        "(rebalance, if needed). **Step 3** runs after parameter changes. **Step 5** is "
        "corrective and rarely needed."
    )

    cols = st.columns(5)

    # Step 1 — Daily update (long-running, detached)
    if cols[0].button(
        "📥 Step 1\nDaily update",
        help="Pulls today's bars + corporate actions + fundamentals refresh. Long-running (~30-45 min); detaches so the browser tab can close.",
    ):
        pid, logfile = run_detached_script("scripts/run_daily_update.sh")
        st.session_state["detached_job"] = {
            "name": "Daily update",
            "pid": pid,
            "logfile": logfile,
            "started_at": time.time(),
        }
        st.success(f"Launched (pid {pid}); logfile: {logfile}")
        st.rerun()

    # Step 2 — Force-rebalance (typed-confirm)
    if cols[1].button(
        "🔄 Step 2\nForce-rebalance",
        help="Cancels any stale momentum orders, re-scores against today's data, submits a fresh batch. Requires typing REBALANCE to confirm.",
    ):
        st.session_state["pending_confirm"] = {
            "action": "force_rebalance",
            "script": "scripts/run_momentum_kickoff.sh",
            "phrase": "REBALANCE",
            "description": "About to recompute and submit ~50 orders. This affects the Alpaca paper account.",
        }

    # Step 3 — Refresh credibility (blocking)
    if cols[2].button(
        "📊 Step 3\nRefresh credibility",
        help="Re-runs the momentum parameter search and persists the held-back credibility rubric to platform.data_quality_log. ~5 min.",
    ):
        with st.spinner("Running momentum search (this takes ~5 min)..."):
            rc, output = run_blocking_script("scripts/run_momentum_search.sh", timeout=900)
        _render_blocking_output("Refresh credibility", rc, output)

    # Step 4 — Smoke test (blocking, fast)
    if cols[3].button(
        "🧪 Step 4\nSmoke test",
        help="Runs all momentum plug unit tests + scheduler dry-run + tip-sheet render. Canonical 'did the last change break anything' gate.",
    ):
        with st.spinner("Running smoke test..."):
            rc, output = run_blocking_script("scripts/run_momentum_smoke.sh", timeout=300)
        _render_blocking_output("Smoke test", rc, output)

    # Step 5 — Cancel open orders (corrective)
    if cols[4].button(
        "🛑 Step 5\nCancel open orders",
        help="Corrective only — cancels all `mo_*` open orders at Alpaca. Requires typing CANCEL to confirm.",
    ):
        st.session_state["pending_confirm"] = {
            "action": "cancel_orders",
            "script": None,  # handled inline
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
            st.success(
                f"✓  {job['name']} finished (pid {job['pid']}, elapsed {elapsed/60:.1f} min)"
            )
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
        heartbeat = f"⚠ no output for {stale/60:.1f} min"
        color = "#d68800"
    else:
        heartbeat = f"⚠ STALE — no output for {stale/60:.1f} min"
        color = "#c92a2a"

    st.markdown(
        f"<div style='padding:10px;border-left:4px solid {color};background:#f8f8f8;'>"
        f"<strong>Running</strong> — {job['name']}  "
        f"<span style='color:#666;'>pid {job['pid']} · elapsed {elapsed/60:.1f} min · "
        f"<span style='color:{color};'>{heartbeat}</span></span></div>",
        unsafe_allow_html=True,
    )
    with st.expander("Last 30 log lines", expanded=True):
        st.code(status["tail"] or "(waiting for first output)", language="bash")


# ────────────────────────────────────────────────────────────────────────────
# Page assembly
# ────────────────────────────────────────────────────────────────────────────


def main():
    st.set_page_config(
        page_title="Trading Engine — Operator Dashboard",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    render_header()

    # Manual refresh button
    c1, _ = st.columns([1, 8])
    if c1.button("🔁  Refresh", help="Re-fetch all panels"):
        st.rerun()

    st.divider()
    render_actions()
    render_confirm_modal()
    render_detached_job_panel()

    st.divider()
    selected_ticker = render_holdings()

    # Ticker detail panel — only renders when a row is selected. Sits
    # between holdings and equity curve so the operator's eye flows
    # naturally: row click → chart appears directly below.
    if selected_ticker:
        st.divider()
        render_ticker_detail(selected_ticker)

    st.divider()
    render_equity_curve()

    st.divider()
    st.caption(
        "Local research console — not financial advice. Dashboard dispatches "
        "existing scripts in `scripts/`; no business logic is duplicated here. "
        "See `docs/superpowers/specs/2026-05-13-operator-dashboard.md` for the design."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
