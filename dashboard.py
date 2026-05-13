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
from scripts.generate_tip_sheet import (
    fetch_engine_holdings,
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
        return
    if not holdings:
        st.info("No open Momentum positions.")
        return
    import pandas as pd
    df = pd.DataFrame(holdings)
    df["pnl_pct"] = df["unrealized_pl_pct"] * 100.0
    df = df[["ticker", "qty", "entry_price", "current_price", "market_value", "unrealized_pl", "pnl_pct"]]
    df.columns = ["Ticker", "Qty", "Entry", "Current", "Market Value", "P&L $", "P&L %"]
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Entry": st.column_config.NumberColumn(format="$%.2f"),
            "Current": st.column_config.NumberColumn(format="$%.2f"),
            "Market Value": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )
    st.caption(f"Data as of {datetime.now(UTC).strftime('%H:%M:%S UTC')}")


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


def render_actions():
    st.subheader("Actions")
    cols = st.columns(5)

    # Daily update — long-running, detached
    if cols[0].button("📥  Daily update", help="Pulls today's bars + corporate actions + fundamentals. ~30-45 min."):
        pid, logfile = run_detached_script("scripts/run_daily_update.sh")
        st.session_state["detached_job"] = {
            "name": "Daily update",
            "pid": pid,
            "logfile": logfile,
            "started_at": time.time(),
        }
        st.success(f"Launched (pid {pid}); logfile: {logfile}")
        st.rerun()

    # Force-rebalance momentum — typed-confirm modal
    if cols[1].button("🔄  Force-rebalance Momentum", help="Re-score and submit a fresh rebalance"):
        st.session_state["pending_confirm"] = {
            "action": "force_rebalance",
            "script": "scripts/run_momentum_kickoff.sh",
            "phrase": "REBALANCE",
            "description": "About to recompute and submit ~50 orders. This affects the Alpaca paper account.",
        }

    # Refresh credibility — blocking
    if cols[2].button("📊  Refresh credibility", help="Re-runs the momentum parameter search (~5 min)"):
        with st.spinner("Running momentum search (this takes ~5 min)..."):
            rc, output = run_blocking_script("scripts/run_momentum_search.sh", timeout=900)
        _render_blocking_output("Refresh credibility", rc, output)

    # Smoke test — blocking, fast
    if cols[3].button("🧪  Smoke test", help="Runs all momentum tests + dry-run scheduler + tip-sheet render"):
        with st.spinner("Running smoke test..."):
            rc, output = run_blocking_script("scripts/run_momentum_smoke.sh", timeout=300)
        _render_blocking_output("Smoke test", rc, output)

    # Cancel all open orders — typed-confirm modal
    if cols[4].button("🛑  Cancel open orders", help="Cancels all open mo_* orders at the broker"):
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
    render_holdings()

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
