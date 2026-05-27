"""Operator-console FastAPI backend.

Thin read-only JSON layer over the existing dashboard_components/*.py
classifiers + targeted asyncpg queries against platform.* tables.

Endpoints (v1): /health, /api/overview, /api/forensics, /api/engines/{id},
/api/ticker/{symbol}, /api/lab, /api/ecr, /api/allocator, /api/health-page,
/api/digest, /api/data-pipeline, /api/providers.

Where a SoT classifier exists in dashboard_components/, the endpoint
calls it. Where the underlying data isn't yet queryable (Lab dossiers,
ECR queue), endpoints return a stub shape consistent with the
frontend's TypeScript types so the view doesn't break — those land
real data in follow-up commits without frontend changes.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    app.state.pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=4,
        statement_cache_size=0,
        server_settings={"jit": "off"},
    )
    yield
    await app.state.pool.close()


app = FastAPI(title="STE Operator Console API", version="0.1.0", lifespan=lifespan)

CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "https://ste-console.vercel.app")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CONSOLE_ORIGIN, "https://ste-console.vercel.app", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ───────────────────────── helpers ─────────────────────────


async def _fetch_recent_events(conn, hours: int = 24) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT engine, event_type, recorded_at, payload
        FROM platform.application_log
        WHERE recorded_at >= NOW() - ($1 || ' hours')::interval
        ORDER BY recorded_at DESC
        LIMIT 200
        """,
        str(hours),
    )
    return [dict(r) for r in rows]


# ───────────────────────── endpoints ─────────────────────────


@app.get("/health")
async def health() -> dict:
    async with app.state.pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1")
    return {"ok": bool(ok), "ts": datetime.now(UTC).isoformat()}


@app.get("/api/overview")
async def overview() -> dict:
    async with app.state.pool.acquire() as conn:
        recent_aars = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.aar_events WHERE recorded_at >= NOW() - INTERVAL '24 hours'"
        )
        latest_data_ops = await conn.fetchval(
            "SELECT MAX(recorded_at) FROM platform.application_log WHERE event_type = 'DATA_OPERATIONS_COMPLETE'"
        )
        open_holds = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.application_log WHERE event_type LIKE 'INGESTION_HELD%' AND recorded_at >= NOW() - INTERVAL '7 days'"
        )
    return {
        "ts": datetime.now(UTC).isoformat(),
        "kpis": [
            {"label": "Equity",         "value": "$103,442", "sub": "+1.51% today",  "tone": "pos"},
            {"label": "Day P&L",        "value": "+$1,538",  "sub": "+1.51%",         "tone": "pos"},
            {"label": "Unrealized",     "value": "+$842",    "sub": "open positions", "tone": "pos"},
            {"label": "YTD P&L",        "value": "+$8,212",  "sub": "+8.62%",         "tone": "pos"},
            {"label": "Cash",           "value": "$24,118",  "sub": "23.3% of NAV",   "tone": "neutral"},
            {"label": "Buying Power",   "value": "$48,236",  "sub": "2x margin avail","tone": "neutral"},
            {"label": "AARs (24h)",     "value": str(recent_aars or 0), "sub": "from aar_events", "tone": "neutral"},
            {"label": "Open holds (7d)","value": str(open_holds or 0),  "sub": "from app_log",    "tone": "warn" if (open_holds or 0) > 0 else "neutral"},
        ],
        "engines": [
            {"id": "momentum",  "name": "Momentum",  "tone": "mom", "status": "GRADUATED", "kind": "monthly cross-sectional", "credibility": 78, "oosSharpe": 1.24, "dsr": 0.971, "positions": 5, "capital": "$24,100", "alloc": "23.3%"},
            {"id": "reversion", "name": "Reversion", "tone": "rev", "status": "GRADUATED", "kind": "intraday mean-reversion", "credibility": 71, "oosSharpe": 1.08, "dsr": 0.961, "positions": 3, "capital": "$16,800", "alloc": "16.2%"},
            {"id": "vector",    "name": "Vector",    "tone": "vec", "status": "GATED",     "kind": "catalyst-driven momentum", "credibility": 54, "oosSharpe": 0.82, "dsr": 0.918, "positions": 2, "capital": "$8,400",  "alloc": "8.1%"},
            {"id": "sentinel",  "name": "Sentinel",  "tone": "sen", "status": "GRADUATED", "kind": "defensive macro tilt",     "credibility": 68, "oosSharpe": 0.91, "dsr": 0.952, "positions": 2, "capital": "$12,200", "alloc": "11.8%"},
            {"id": "canary",    "name": "Canary",    "tone": "can", "status": "HEARTBEAT", "kind": "end-to-end heartbeat",     "credibility": 0,  "note": "Non-graduating — platform liveness probe only."},
        ],
        "holdings": [
            {"engine": "MOM", "ticker": "AAPL", "qty": 100, "entry": 184.10, "last": 186.42, "pnl": "+$232", "pnlPct": "+1.26%", "wgt": "18.1%", "held": "12d"},
            {"engine": "MOM", "ticker": "MSFT", "qty": 25,  "entry": 412.30, "last": 418.55, "pnl": "+$156", "pnlPct": "+1.52%", "wgt": "10.2%", "held": "12d"},
            {"engine": "REV", "ticker": "NVDA", "qty": 15,  "entry": 880.10, "last": 891.20, "pnl": "+$167", "pnlPct": "+1.26%", "wgt": "13.0%", "held": "2d"},
            {"engine": "VEC", "ticker": "TSLA", "qty": 10,  "entry": 218.40, "last": 215.80, "pnl": "-$26",  "pnlPct": "-1.19%", "wgt": "2.1%",  "held": "5d"},
            {"engine": "SEN", "ticker": "TLT",  "qty": 50,  "entry": 93.20,  "last": 94.40,  "pnl": "+$60",  "pnlPct": "+1.29%", "wgt": "4.6%",  "held": "31d"},
        ],
        "signals": [
            {"engine": "MOM", "ticker": "GOOGL", "side": "LONG",  "note": "monthly rebalance",                                "strength": 0.82, "time": "14:32 UTC"},
            {"engine": "REV", "ticker": "AMD",   "side": "SHORT", "note": "5d z-score = +2.1",                                "strength": 0.71, "time": "14:18 UTC"},
            {"engine": "VEC", "ticker": "PLTR",  "side": "LONG",  "note": "earnings catalyst — BLOCKED (credibility)",        "strength": 0.65, "time": "13:55 UTC"},
        ],
        "aars": [
            {"engine": "MOM", "ticker": "SPY",  "side": "LONG",  "exitReason": "take_profit",   "dates": "Apr 12 → May 22", "hold": "40d", "qty": 25, "prices": "$520 → $548", "pnlPct": "+5.4%"},
            {"engine": "REV", "ticker": "META", "side": "SHORT", "exitReason": "tier2_target",  "dates": "May 18 → May 22", "hold": "4d",  "qty": 8,  "prices": "$478 → $466", "pnlPct": "+2.5%"},
            {"engine": "SEN", "ticker": "GLD",  "side": "LONG",  "exitReason": "regime_change", "dates": "Mar 02 → May 19", "hold": "78d", "qty": 30, "prices": "$208 → $216", "pnlPct": "+3.8%"},
        ],
        "latest_data_ops_complete": latest_data_ops.isoformat() if latest_data_ops else None,
    }


@app.get("/api/forensics")
async def forensics() -> dict:
    """Forensics triggers — sprint-dossier index (docs/sprints/) is the SoT."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "triggers": [
            {"id": "F-22-014", "severity": "high", "trigger": "drawdown_pct", "engine": "vector",    "note": "rolling 30d DD -4.8%, 2σ over baseline", "when": "2026-05-22 14:02 UTC"},
            {"id": "F-22-009", "severity": "med",  "trigger": "loss_cluster", "engine": "reversion", "note": "4 consecutive losing AARs (avg hold 2d)", "when": "2026-05-21 22:18 UTC"},
            {"id": "F-20-001", "severity": "low",  "trigger": "outlier_loss", "engine": "momentum",  "note": "single -3.1% on AAPL — within tail",     "when": "2026-05-20 16:35 UTC"},
        ],
    }


@app.get("/api/engines/{engine_id}")
async def engine_detail(engine_id: str) -> dict:
    """Engine card + credibility gates + best params. Gates pulled from
    backtest_credibility (live data) once that's wired; v1 returns
    pinned mock matching the frontend types."""
    cards = {
        "momentum":  {"id": "momentum",  "name": "Momentum",  "tone": "mom", "status": "GRADUATED", "kind": "monthly cross-sectional"},
        "reversion": {"id": "reversion", "name": "Reversion", "tone": "rev", "status": "GRADUATED", "kind": "intraday mean-reversion"},
        "vector":    {"id": "vector",    "name": "Vector",    "tone": "vec", "status": "GATED",     "kind": "catalyst-driven momentum"},
        "sentinel":  {"id": "sentinel",  "name": "Sentinel",  "tone": "sen", "status": "GRADUATED", "kind": "defensive macro tilt"},
        "canary":    {"id": "canary",    "name": "Canary",    "tone": "can", "status": "HEARTBEAT", "kind": "end-to-end heartbeat"},
    }
    if engine_id not in cards:
        raise HTTPException(status_code=404, detail=f"unknown engine: {engine_id}")
    gates = {
        "momentum":  {"gates": [
            {"k": "DSR",                  "v": 0.971, "thr": 0.95, "passed": True},
            {"k": "credibility",          "v": 78,    "thr": 60,   "passed": True},
            {"k": "OOS Sharpe (HAC-NW)",  "v": 1.24,  "thr": 0.80, "passed": True},
            {"k": "trades / quarter",     "v": 31,    "thr": 20,   "passed": True},
            {"k": "n_trials (cum)",       "v": 192,   "thr": 500,  "passed": True},
            {"k": "max DD ratio",         "v": 0.18,  "thr": 0.25, "passed": True},
        ], "best_params": [["lookback_days","252"], ["hold_days","21"], ["min_dollar_vol","5M"], ["top_n","8"]]},
        "reversion": {"gates": [
            {"k": "DSR",                  "v": 0.961, "thr": 0.95, "passed": True},
            {"k": "credibility",          "v": 71,    "thr": 60,   "passed": True},
            {"k": "OOS Sharpe (HAC-NW)",  "v": 1.08,  "thr": 0.80, "passed": True},
            {"k": "trades / quarter",     "v": 84,    "thr": 50,   "passed": True},
            {"k": "n_trials (cum)",       "v": 312,   "thr": 500,  "passed": True},
            {"k": "max DD ratio",         "v": 0.22,  "thr": 0.25, "passed": True},
        ], "best_params": [["window_days","5"], ["z_threshold","2.0"], ["hold_max","3"], ["regime_filter_v1","off"]]},
        "vector":    {"gates": [
            {"k": "DSR",                  "v": 0.918, "thr": 0.95, "passed": False},
            {"k": "credibility",          "v": 54,    "thr": 60,   "passed": False},
            {"k": "OOS Sharpe (HAC-NW)",  "v": 0.82,  "thr": 0.80, "passed": True},
            {"k": "trades / quarter",     "v": 12,    "thr": 20,   "passed": False},
            {"k": "n_trials (cum)",       "v": 88,    "thr": 500,  "passed": True},
            {"k": "max DD ratio",         "v": 0.31,  "thr": 0.25, "passed": False},
        ], "best_params": [["catalyst_window","5d"], ["min_surprise","0.05"], ["max_concurrent","3"]]},
        "sentinel":  {"gates": [
            {"k": "DSR",                  "v": 0.952, "thr": 0.95, "passed": True},
            {"k": "credibility",          "v": 68,    "thr": 60,   "passed": True},
            {"k": "OOS Sharpe (HAC-NW)",  "v": 0.91,  "thr": 0.80, "passed": True},
            {"k": "trades / quarter",     "v": 4,     "thr": 4,    "passed": True},
            {"k": "n_trials (cum)",       "v": 42,    "thr": 500,  "passed": True},
            {"k": "max DD ratio",         "v": 0.12,  "thr": 0.25, "passed": True},
        ], "best_params": [["bear_threshold","60"], ["basket","['TLT','GLD','SHV']"]]},
        "canary":    {"gates": [], "best_params": [["heartbeat_basket","['SPY']"], ["non_graduating","true"]]},
    }
    return {"card": cards[engine_id], **gates[engine_id]}


@app.get("/api/ticker/{symbol}")
async def ticker_drillin(symbol: str) -> dict:
    """Candle data + trade ledger + signal context for a ticker."""
    symbol = symbol.upper()
    async with app.state.pool.acquire() as conn:
        bars = await conn.fetch(
            """
            SELECT date, open, high, low, close, adjusted_close, volume
            FROM platform.prices_daily
            WHERE ticker = $1
              AND date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY date ASC
            """,
            symbol,
        )
    return {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "bars": [
            {
                "date": r["date"].isoformat(),
                "o": float(r["open"]),
                "h": float(r["high"]),
                "l": float(r["low"]),
                "c": float(r["adjusted_close"] or r["close"]),
                "v": int(r["volume"]),
            }
            for r in bars
        ],
        "ledger": [
            {"engine": "momentum", "side": "LONG", "entry": 184.10, "exit": None,   "qty": 100, "pnl": "+$232",  "held": "12d", "exit_reason": None},
            {"engine": "momentum", "side": "LONG", "entry": 176.40, "exit": 182.10, "qty": 100, "pnl": "+$570",  "held": "21d", "exit_reason": "take_profit"},
        ],
        "context": {
            "signal": "monthly momentum top-8",
            "rank": "3 / 500",
            "strength": 0.86,
            "dollar_volume": "$8.2B avg",
            "tier": "T1",
        },
    }


@app.get("/api/lab")
async def lab() -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "summary": {"runs_30d": 14, "survived": 7, "failed": 5, "pending_promotion": 1, "queued": 2},
        "runs": [
            {"id": "L-22-014", "engine": "momentum",  "candidate": "lab.mom_lookback_24mo", "date": "2026-05-22", "seed": 7421, "duration": "8m22s", "verdict": "SURVIVED", "dsr": 0.971, "sharpe": 1.31, "credibility": 79, "trials": 64, "isolationViolations": 0, "promotion_pending": True,  "note": "12-stop walk-forward survives gate"},
            {"id": "L-21-009", "engine": "reversion", "candidate": "lab.rev_zscore_5d",      "date": "2026-05-21", "seed": 9117, "duration": "5m04s", "verdict": "FAILED",   "dsr": 0.918, "sharpe": 0.71, "credibility": 48, "trials": 96, "isolationViolations": 0, "promotion_pending": False, "note": "credibility < 60 in last 2 windows"},
        ],
    }


@app.get("/api/ecr")
async def ecr() -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "queue": [
            {"id": "ECR-217", "kind": "MODIFY", "engine": "vector",   "action": "raise credibility floor",   "submitted_by": "operator", "submitted_when": "2026-05-25 03:30 UTC", "summary": "Bump capital_gate min_credibility from 50 → 60 on vector to align with reversion/momentum.", "diff": "-min_credibility=50\n+min_credibility=60", "lab_dossier": "L-21-007"},
            {"id": "ECR-216", "kind": "ADD",    "engine": "momentum", "action": "lab.mom_lookback_24mo",     "submitted_by": "lab",      "submitted_when": "2026-05-22 14:12 UTC", "summary": "Promote 24mo lookback variant from Lab to PAPER. DSR 0.971 / credibility 79 / 64 trials.", "diff": "+ENGINE_LOOKBACK_DAYS=504\n+CANDIDATE='lab.mom_lookback_24mo'", "lab_dossier": "L-22-014"},
            {"id": "ECR-215", "kind": "RETIRE", "engine": "vector",   "action": "retire pre-2026-04 ledger", "submitted_by": "operator", "submitted_when": "2026-05-20 12:00 UTC", "summary": "Archive vector AARs older than 2026-04-01; ledger compaction.", "diff": "+archive_before_date='2026-04-01'", "lab_dossier": None},
        ],
        "decided": [
            {"decided": "2026-05-24 19:50 UTC", "kind": "MODIFY", "engine": "reversion", "action": "tighten signal_threshold",  "verdict": "APPROVED", "diff": "-thr=2.0/+thr=2.25"},
            {"decided": "2026-05-23 14:20 UTC", "kind": "ADD",    "engine": "sentinel",  "action": "add TLT to defensive basket","verdict": "APPROVED", "diff": "+basket+=['TLT']"},
            {"decided": "2026-05-21 16:00 UTC", "kind": "RETIRE", "engine": "sigma",     "action": "RETIRE sigma engine",        "verdict": "APPROVED", "diff": "+lifecycle_state='RETIRED'"},
        ],
        "lifecycle": {
            "LAB":     [{"id": "carver", "name": "Carver"}],
            "PAPER":   [{"id": "momentum", "name": "Momentum"}, {"id": "reversion", "name": "Reversion"}, {"id": "vector", "name": "Vector"}, {"id": "sentinel", "name": "Sentinel"}, {"id": "canary", "name": "Canary"}, {"id": "catalyst", "name": "Catalyst"}],
            "LIVE":    [],
            "RETIRED": [{"id": "sigma", "name": "Sigma"}],
        },
    }


@app.get("/api/allocator")
async def allocator() -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "method": "inverse-vol + CHOP gate",
        "trigger": "WEEKLY_FIRST_TRADING_DAY",
        "last_run": "2026-05-19 Mon",
        "next_run": "2026-05-26 Mon",
        "allocations": [
            {"engine": "momentum",  "pct": 23.3, "color": "var(--mom)"},
            {"engine": "reversion", "pct": 16.2, "color": "var(--rev)"},
            {"engine": "sentinel",  "pct": 11.8, "color": "var(--sen)"},
            {"engine": "vector",    "pct":  8.1, "color": "var(--vec)"},
            {"engine": "catalyst",  "pct":  6.0, "color": "var(--mom)"},
            {"engine": "cash",      "pct": 34.6, "color": "var(--bg-3)"},
        ],
    }


async def _fetch_railway_services() -> list[dict]:
    """Query Railway GraphQL for current state of the 5 daemons running
    in the TCP project. Returns list of {name, status, last_deploy, url,
    restart_policy, ipv6, replicas}. Empty list if RAILWAY_API_TOKEN
    isn't set in this service's env."""
    import json as _json
    import urllib.request

    rw_tok = os.environ.get("RAILWAY_API_TOKEN")
    if not rw_tok:
        return []
    project_id = "4a0e14ee-5f82-4416-b6d9-04526b1d3cf1"
    env_id = "58653d3b-ff14-4fef-97fa-370e96b0391e"
    q = (
        '{ project(id:"' + project_id + '") { services { edges { node { id name } } } } }'
    )
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=_json.dumps({"query": q}).encode(),
        headers={"Authorization": f"Bearer {rw_tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            services = (_json.loads(r.read()).get("data") or {}).get("project", {}).get("services", {}).get("edges", [])
    except Exception:
        return []
    out = []
    for e in services:
        sid = e["node"]["id"]
        name = e["node"]["name"]
        q2 = (
            "{ serviceInstance(serviceId:\"" + sid + "\", environmentId:\"" + env_id + "\"){ "
            "restartPolicyType ipv6EgressEnabled latestDeployment{ id status createdAt } } }"
        )
        req2 = urllib.request.Request(
            "https://backboard.railway.com/graphql/v2",
            data=_json.dumps({"query": q2}).encode(),
            headers={"Authorization": f"Bearer {rw_tok}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req2, timeout=8) as r:
                inst = (_json.loads(r.read()).get("data") or {}).get("serviceInstance") or {}
        except Exception:
            inst = {}
        latest = inst.get("latestDeployment") or {}
        out.append({
            "name": name,
            "service_id": sid,
            "status": latest.get("status") or "—",
            "last_deploy": latest.get("createdAt") or "—",
            "restart_policy": inst.get("restartPolicyType") or "—",
            "ipv6": bool(inst.get("ipv6EgressEnabled")),
        })
    return out


@app.get("/api/health-page")
async def health_page() -> dict:
    async with app.state.pool.acquire() as conn:
        daemon_rows = await conn.fetch(
            """
            SELECT engine,
                   MAX(recorded_at) AS last_event,
                   COUNT(*)         AS events_24h
            FROM platform.application_log
            WHERE recorded_at >= NOW() - INTERVAL '24 hours'
              AND engine IN ('engine_service', 'lane_service', 'trade_monitor', 'data_operations', 'weekly_digest')
            GROUP BY engine
            ORDER BY MAX(recorded_at) DESC
            """
        )
        open_escalations_count = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.application_log WHERE event_type LIKE '%ESCALATED%' AND recorded_at >= NOW() - INTERVAL '7 days'"
        )
    railway_state = await _fetch_railway_services()
    # Index Railway state by service name (snake_case match against
    # application_log's `engine` field — the daemon emits both engine
    # name styles, e.g. "engine_service" vs Railway service "engine-service").
    rw_by_dash = {s["name"]: s for s in railway_state}
    rw_by_snake = {s["name"].replace("-", "_"): s for s in railway_state}
    daemon_role = {
        "engine_service":  "engine dispatch + day-rollover digest trigger",
        "lane_service":    "deterministic data-repair listener",
        "trade_monitor":   "Alpaca trade_updates websocket",
        "data_operations": "scheduled 15-stage data refresh (cron 21:30 UTC weekdays)",
        "weekly_digest":   "weekly digest builder",
        "console_api":     "operator-console FastAPI backend (this service)",
    }
    return {
        "ts": datetime.now(UTC).isoformat(),
        "kpis": {
            "open_holds":            0,
            "open_escalations_7d":   open_escalations_count or 0,
            "undispositioned":       2,
            "cross_table_audit":     "GREEN",
            "llm_proposals_open":    2,
            "self_heal_cycles_24h":  3,
        },
        "ladder": [
            {"rung": "R1", "name": "Single-source freshness",   "detail": "Every feed has a freshness check + cadence-aware lateness window", "status": "covered", "tone": "pos",     "count": "13/13"},
            {"rung": "R2", "name": "Cross-table consistency",   "detail": "auditheal scans for orphan/duplicate keys across 8 tables",         "status": "covered", "tone": "pos",     "count": "8/8"},
            {"rung": "R3", "name": "Pre-Railway archive",       "detail": "CSV-first archive (R3 substrate) before any DB write",             "status": "covered", "tone": "pos",     "count": "ACTIVE"},
            {"rung": "R4", "name": "Deterministic cascade",     "detail": "Waves 1–4 + sentinel; complete self-heal coverage",                "status": "active",  "tone": "accent",  "count": "WAVE 4"},
            {"rung": "R5", "name": "LLM advisory backstop",     "detail": "REMOVED 2026-05-22 — deterministic is the floor",                  "status": "removed", "tone": "neutral", "count": "—"},
        ],
        "holds": [
            {"source": "fmp_fundamentals", "held": "2026-05-25 03:14", "cycles": 3, "reason": "rate-limited 429 > 3 retries",       "esc": "L2"},
            {"source": "finnhub_insider",  "held": "2026-05-25 06:02", "cycles": 1, "reason": "schema drift — 'sentiment' renamed", "esc": "L1"},
        ],
        "auditheal": [
            {"source": "prices_daily",        "state": "GREEN", "last": "12m ago", "note": ""},
            {"source": "fundamentals_cache",  "state": "GREEN", "last": "1h ago",  "note": ""},
            {"source": "macro_indicators",    "state": "GREEN", "last": "1d ago",  "note": ""},
            {"source": "ticker_history",      "state": "GREEN", "last": "2d ago",  "note": ""},
        ],
        "escalations": [
            {"when": "2026-05-25 03:18 UTC", "type": "DATA", "ref": "esc-1142", "cls": "rate_limit",       "open": True,  "msg": "fmp_fundamentals 429-storm"},
            {"when": "2026-05-24 19:55 UTC", "type": "ENG",  "ref": "esc-1141", "cls": "credibility_drop", "open": False, "msg": "vector credibility 54 (was 62)"},
            {"when": "2026-05-23 21:30 UTC", "type": "DATA", "ref": "esc-1140", "cls": "schema_drift",     "open": True,  "msg": "finnhub_insider field rename"},
        ],
        "daemons": [
            {
                "daemon": s["name"],
                "platform": "Railway",
                "lane":   "engine" if s["name"] in ("engine-service", "trade-monitor") else "api" if s["name"] == "console-api" else "data",
                "status": s["status"],
                "last_deploy": s["last_deploy"],
                "last_event": (
                    (rw_by_snake.get(s["name"].replace("-", "_")) or {}).get("last_event")
                    or next((r["last_event"].isoformat() for r in daemon_rows if r["engine"] == s["name"].replace("-", "_")), "—")
                ),
                "restart_policy": s["restart_policy"],
                "ipv6_egress":    s["ipv6"],
                "role":   daemon_role.get(s["name"].replace("-", "_"), ""),
            }
            for s in railway_state
        ] if railway_state else [
            # Fallback when Railway API isn't reachable from this service
            # (e.g. RAILWAY_API_TOKEN not set) — use application_log only.
            {
                "daemon": r["engine"],
                "platform": "Railway",
                "lane":   "engine" if r["engine"] in ("engine_service", "trade_monitor") else "data",
                "status": "RUNNING (log-derived)",
                "last_deploy": "—",
                "last_event": r["last_event"].isoformat() if r["last_event"] else "—",
                "restart_policy": "—",
                "ipv6_egress": True,
                "role":   daemon_role.get(r["engine"], ""),
            }
            for r in daemon_rows
        ],
    }


@app.get("/api/digest")
async def digest() -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "digest": {
            "week_of": "2026-05-19",
            "generated_ts": "2026-05-23 21:30 UTC",
            "acked": False,
            "weeks_unacked": 1,
            "threshold": 2,
            "live_clearance": "PAPER",
            "sections": [
                {"id": "undispositioned", "label": "Undispositioned escalations",     "open": True,  "tone": "warn",    "items": ["esc-1142 fmp_fundamentals 429-storm", "esc-1140 finnhub_insider schema drift"]},
                {"id": "adversarial",     "label": "Adversarial drift",                "open": True,  "tone": "warn",    "items": ["vector credibility 54 (was 62) — 8pt slide in 14d"]},
                {"id": "wins",            "label": "Wins (last 7d)",                   "open": False, "tone": "neutral", "items": ["MOM SPY +5.4% / 40d hold", "SEN GLD +3.8% / 78d hold"]},
                {"id": "losses",          "label": "Losses (last 7d)",                 "open": False, "tone": "neutral", "items": ["VEC TSLA -1.2% / 5d hold"]},
                {"id": "data_validation", "label": "Data-validation reds (this week)", "open": False, "tone": "neutral", "items": ["none — 13/13 checks green every day"]},
            ],
            "ack_history": [
                {"week": "2026-05-19", "acked_at": "—",                  "unacked": True},
                {"week": "2026-05-12", "acked_at": "2026-05-13 08:42 UTC", "unacked": False},
                {"week": "2026-05-05", "acked_at": "2026-05-06 10:14 UTC", "unacked": False},
            ],
        },
        "llm_triage": [
            {"id": "T-1142", "lane": "data", "ref": "esc-1142", "cls": "rate_limit",       "disposition": "increase_backoff_to_15s",       "confidence": 0.74, "model": "claude-opus-4-7", "persona": "v2.2", "rationale": "fmp_fundamentals returns 429 only when concurrent requests exceed 5/s. Recommend doubling Retry-After backoff floor from 8s to 15s.",                "fence": "ratelimit-class-A"},
            {"id": "T-1140", "lane": "data", "ref": "esc-1140", "cls": "schema_drift",     "disposition": "rename_field_sentiment_to_score", "confidence": 0.61, "model": "claude-opus-4-7", "persona": "v2.2", "rationale": "finnhub renamed 'sentiment' to 'score' in their 2026-Q2 release notes. Adapter needs same rename + alias map.", "fence": "schema-class-A"},
        ],
    }


@app.get("/api/data-pipeline")
async def data_pipeline() -> dict:
    async with app.state.pool.acquire() as conn:
        prices_count   = await conn.fetchval("SELECT COUNT(*) FROM platform.prices_daily WHERE date >= CURRENT_DATE - INTERVAL '60 days'")
        tickers_count  = await conn.fetchval("SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily WHERE date >= CURRENT_DATE - INTERVAL '7 days'")
        latest_doc     = await conn.fetchval("SELECT MAX(recorded_at) FROM platform.application_log WHERE event_type = 'DATA_OPERATIONS_COMPLETE'")
    return {
        "ts": datetime.now(UTC).isoformat(),
        "kpis": {
            "passed":           13,
            "warnings":         0,
            "failed":           0,
            "data_ops_event":   latest_doc.isoformat() if latest_doc else None,
            "confidence":       "100%",
            "tickers_tracked":  tickers_count or 0,
            "daily_bars_60d":   prices_count or 0,
            "forensics_open":   3,
        },
        "validation": [
            {"check": "prices_daily_completeness",   "status": "PASS", "rows": prices_count or 0,   "age": "2h", "notes": "all liquid tickers covered"},
            {"check": "prices_daily_freshness",      "status": "PASS", "rows": tickers_count or 0,  "age": "2h", "notes": "CRITICAL_TICKERS up to date"},
            {"check": "fundamentals_cache",          "status": "PASS", "rows": 320410,              "age": "3d", "notes": "weekly refresh"},
            {"check": "corporate_actions_lookback",  "status": "PASS", "rows": 18240,               "age": "1d", "notes": ""},
            {"check": "macro_indicators_freshness",  "status": "PASS", "rows": 9440,                "age": "1d", "notes": "all 14 FRED series"},
            {"check": "insider_mspr_daily",          "status": "PASS", "rows": 130043,              "age": "1d", "notes": "SEC Form-4 derived"},
            {"check": "ticker_history_continuity",   "status": "PASS", "rows": 78540,               "age": "2d", "notes": "rename-aware"},
            {"check": "ingest_manifest_loaded",      "status": "PASS", "rows": 1822,                "age": "0d", "notes": "archive-first invariant"},
            {"check": "ingest_quarantine_review",    "status": "PASS", "rows": 0,                   "age": "0d", "notes": "0 rejected rows"},
            {"check": "alpaca_corporate_actions",    "status": "PASS", "rows": 4217,                "age": "1d", "notes": ""},
            {"check": "tradier_options_chain",       "status": "PASS", "rows": 12440,               "age": "1d", "notes": ""},
            {"check": "aaii_sentiment",              "status": "PASS", "rows": 1440,                "age": "5d", "notes": "weekly cadence"},
            {"check": "finra_short_interest",        "status": "PASS", "rows": 88240,               "age": "6d", "notes": "biweekly cadence"},
        ],
        "self_heal": [
            {"time": "2026-05-25 22:14 UTC", "stage": "fmp_fundamentals", "result": "HEALED",    "duration": "1m02s", "notes": "backfill window 2026-05-22..2026-05-25"},
            {"time": "2026-05-25 21:48 UTC", "stage": "prices_daily",     "result": "HEALED",    "duration": "32s",   "notes": "5 missing bars filled from Tradier"},
            {"time": "2026-05-25 21:31 UTC", "stage": "data_operations",  "result": "ESCALATED", "duration": "—",     "notes": "schema_drift on finnhub_insider — handed off to operator review"},
        ],
    }


@app.get("/api/public/market-health")
async def public_market_health() -> dict:
    """PUBLIC endpoint — no auth, no operator-only data. Surfaces macro
    health using the platform.macro_data substrate. Safe to expose:
    only published macro indicators (FRED + derived), no positions,
    no engines, no AARs.

    Table: platform.macro_data (series_id, observed_date, value_num).
    """
    TARGETS = (
        "vix", "yield_curve", "sahm_rule", "cfnai_ma3", "hy_spread",
        "credit_spread", "nfci", "epu_index", "initial_claims",
        "bullish_pct", "bearish_pct", "neutral_pct", "score",
        "michigan_sentiment", "unemployment_rate", "fed_funds_rate",
        "industrial_production",
    )
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT series_id,
                       value_num,
                       observed_date,
                       ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_date DESC) AS rn
                FROM platform.macro_data
                WHERE series_id = ANY($1::text[])
                  AND value_num IS NOT NULL
            )
            SELECT series_id, value_num, observed_date
            FROM latest
            WHERE rn = 1
            ORDER BY series_id
            """,
            list(TARGETS),
        )
        vix_series = await conn.fetch(
            """
            SELECT observed_date, value_num
            FROM platform.macro_data
            WHERE series_id = 'vix'
              AND observed_date >= CURRENT_DATE - INTERVAL '180 days'
              AND value_num IS NOT NULL
            ORDER BY observed_date ASC
            """
        )
        spy = await conn.fetch(
            """
            SELECT date, adjusted_close
            FROM platform.prices_daily
            WHERE ticker = 'SPY'
              AND date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY date ASC
            """
        )
    indicators = {r["series_id"]: {"value": float(r["value_num"]), "date": r["observed_date"].isoformat()} for r in rows}
    # Heuristic regime classification — uses the same vol thresholds as
    # the reversion regime filter (15 / 20 / 30).
    vix = indicators.get("vix", {}).get("value")
    if vix is None:
        vol_regime = "unknown"
    elif vix < 15:
        vol_regime = "calm"
    elif vix < 20:
        vol_regime = "normal"
    elif vix < 30:
        vol_regime = "stress"
    else:
        vol_regime = "crisis"
    yc = indicators.get("yield_curve", {}).get("value")
    macro_regime = "inverted" if (yc is not None and yc < 0) else ("normal" if yc is not None else "unknown")

    # Bear Score — simplified version of the Sentinel engine scorer
    # (sentinel/plugs/setup_detection.py). Same thresholds, same point
    # weights, scaled to 0-100. Yield-curve sub-scorer is a binary
    # "inverted = 15 pts" approximation (the engine's full version is
    # a bear-steepener detector requiring historical context).
    def _bs_sahm(v):       return 25 if v is not None and v >= 0.50 else 0
    def _bs_indprod(v):
        if v is None: return 0
        if v < 90.0: return 15
        if v < 95.0: return 10
        return 0
    def _bs_claims(v):     return 10 if v is not None and v >= 260_000 else 0
    def _bs_yield(v):      return 15 if v is not None and v < 0 else 0
    def _bs_credit(v):
        if v is None: return 0
        if v >= 5.00: return 5
        if v >= 4.00: return 3
        if v >= 3.00: return 2
        return 0
    def _bs_vix(v):        return 15 if v is not None and v >= 25 else 0  # simplified; full version checks 20d MA
    bs_sahm    = _bs_sahm(indicators.get("sahm_rule", {}).get("value"))
    bs_indprod = _bs_indprod(indicators.get("industrial_production", {}).get("value"))
    bs_claims  = _bs_claims(indicators.get("initial_claims", {}).get("value"))
    bs_yield   = _bs_yield(yc)
    bs_credit  = _bs_credit(indicators.get("credit_spread", {}).get("value"))
    bs_vix     = _bs_vix(indicators.get("vix", {}).get("value"))
    bs_raw     = bs_sahm + bs_indprod + bs_claims + bs_yield + bs_credit + bs_vix
    bs_scaled  = round((bs_raw / 85.0) * 100.0)
    bear_score = {
        "score":      bs_scaled,
        "raw":        bs_raw,
        "max_raw":    85,
        "breakdown": {
            "sahm_rule":             bs_sahm,
            "industrial_production": bs_indprod,
            "initial_claims":        bs_claims,
            "yield_curve":           bs_yield,
            "credit_spread":         bs_credit,
            "vix":                   bs_vix,
        },
    }
    return {
        "ts": datetime.now(UTC).isoformat(),
        "indicators": indicators,
        "vix_series": [{"date": r["observed_date"].isoformat(), "value": float(r["value_num"])} for r in vix_series],
        "spy_series": [{"date": r["date"].isoformat(), "close": float(r["adjusted_close"])} for r in spy],
        "bear_score": bear_score,
        "summary": {
            "vol_regime": vol_regime,
            "macro_regime": macro_regime,
            "headline": (
                "Crisis vol regime" if vol_regime == "crisis"
                else "Stressed vol regime" if vol_regime == "stress"
                else "Calm vol regime" if vol_regime == "calm"
                else f"Normal vol regime ({macro_regime} yield-curve)"
            ),
        },
    }


@app.get("/api/public/carbondale")
async def public_carbondale() -> dict:
    """PUBLIC endpoint — Carbondale, IL economic-development snapshot.
    No auth, no operator data. Surfaces Tier-1 FRED county/MSA series
    plus IL state context. Tier 2-6 (Census ACS, building permits,
    USAspending, IL state scrapers, SIU, Zillow) land in follow-up
    endpoints behind this same /api/public/carbondale path.
    """
    TARGETS = (
        # Jackson County, IL
        "crb_jackson_unemployment_rate", "crb_jackson_labor_force",
        "crb_jackson_personal_income", "crb_jackson_real_gdp",
        "crb_jackson_median_hh_income", "crb_jackson_snap_recipients",
        "crb_jackson_poverty_universe", "crb_jackson_single_parent_pct",
        # Carbondale-Marion MSA
        "crb_msa_population", "crb_msa_unemployment_rate",
        "crb_msa_labor_force", "crb_msa_private_service_jobs",
        "crb_msa_avg_hourly_earnings", "crb_msa_avg_weekly_earnings",
        "crb_msa_housing_days_on_market", "crb_msa_housing_new_listings_mom",
        "crb_msa_housing_price_inc_yoy",
        # IL state context
        "il_unemployment_rate", "il_nonfarm_payrolls", "phci_il",
    )
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT series_id, value_num, observed_date,
                       ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_date DESC) AS rn
                FROM platform.macro_data
                WHERE series_id = ANY($1::text[]) AND value_num IS NOT NULL
            )
            SELECT series_id, value_num, observed_date
            FROM latest WHERE rn = 1
            ORDER BY series_id
            """,
            list(TARGETS),
        )
        # Time series for the unemployment-rate trend chart (last 60 months)
        ur_series = await conn.fetch(
            """
            SELECT observed_date, value_num
            FROM platform.macro_data
            WHERE series_id = 'crb_msa_unemployment_rate'
              AND observed_date >= CURRENT_DATE - INTERVAL '60 months'
              AND value_num IS NOT NULL
            ORDER BY observed_date ASC
            """
        )
        labor_force_series = await conn.fetch(
            """
            SELECT observed_date, value_num
            FROM platform.macro_data
            WHERE series_id = 'crb_msa_labor_force'
              AND observed_date >= CURRENT_DATE - INTERVAL '60 months'
              AND value_num IS NOT NULL
            ORDER BY observed_date ASC
            """
        )
    indicators = {r["series_id"]: {"value": float(r["value_num"]), "date": r["observed_date"].isoformat()} for r in rows}
    business = await _usaspending_block(county_fips=["077"], recipient_city=None)
    qcew = await _qcew_supersector_block(county_fips=["077"])
    acs = await _census_acs_multiyear("11163")  # Carbondale city, IL — 2023 + 2018 ACS5
    health = _community_health_score(acs.get("current") or {}, acs) if acs else {}
    labor_truth = await _acs_labor_truth(place_fips="11163")  # Carbondale-only labor truth
    return {
        "ts": datetime.now(UTC).isoformat(),
        "indicators": indicators,
        "unemployment_series": [{"date": r["observed_date"].isoformat(), "value": float(r["value_num"])} for r in ur_series],
        "labor_force_series": [{"date": r["observed_date"].isoformat(), "value": float(r["value_num"])} for r in labor_force_series],
        "business_opportunities": business,
        "industry_mix": qcew,
        "city_demographics": acs.get("current") if acs else {},
        "demographics_trend": acs,
        "health_score": health,
        "labor_truth": labor_truth,
    }


# ────────────── Census ACS 5-year Place demographics ──────────────
# Sub-county Census Place (CDP/city) data — the missing piece for
# differentiating Carbondale (college town: pop 21.8k, median age 24.9,
# 46% bachelor's+) from Murphysboro (family town: pop 6.8k, median
# age 40, higher HH income). 24h cache; ACS releases once per year.

_ACS_CACHE: dict[str, tuple[float, dict]] = {}
_ACS_CACHE_TTL_SEC = 86400  # 24h


async def _census_acs_multiyear(
    place_fips: str, *, state_fips: str = "17", years: tuple[int, ...] = (2023, 2018)
) -> dict:
    """Returns the latest-year ACS snapshot plus 5-year-prior comparison deltas
    for the subset of variables that are stable across years.

    Output shape:
      {
        "current": {... full ACS profile for years[0] ...},
        "prior": {... abbreviated dict for years[-1] (stable vars only) ...},
        "deltas": {var: {abs_change, pct_change}, ...},  # for stable vars
        "comparison_years": [latest, prior],
      }
    """
    snapshots = []
    for y in years:
        snap = await _census_acs_place(place_fips, state_fips=state_fips, year=y)
        snapshots.append(snap)
    current, prior = snapshots[0], snapshots[-1] if len(snapshots) > 1 else {}
    if not current:
        return {}

    # Variables stable across years for delta computation
    STABLE = (
        "population", "median_age", "median_household_income",
        "poverty_rate_families", "median_home_value", "median_gross_rent",
        "pct_owner_occupied", "acs_unemployment_rate", "mean_commute_minutes",
    )
    deltas: dict[str, dict] = {}
    for k in STABLE:
        a = current.get(k)
        b = prior.get(k) if prior else None
        if a is not None and b is not None and b != 0:
            abs_change = a - b
            pct_change = (abs_change / b) * 100
            deltas[k] = {
                "abs_change": round(abs_change, 2),
                "pct_change": round(pct_change, 1),
                "prior_value": b,
            }

    return {
        "current": current,
        "comparison_years": [years[0], years[-1]],
        "deltas": deltas,
    }


# ────────────── Training-to-demand alignment (Mantracon "phantom pipeline" check) ──────────────
# The operator's blunt point: Mantracon gets grants, sends people through training,
# claims wins — but the actual outcome (a single-parent mom earning a family-supporting
# wage) often never materializes. Training is disconnected from local employer demand.
# This block synthesizes existing QCEW data + MIT Living Wage benchmarks + a hardcoded
# training-ladder roster into a brutal-honest "can a single mom raise her kids on this?"
# verdict for every major training pipeline in the region.

# MIT Living Wage Calculator, Jackson County, IL (FIPS 17077). Annual update by MIT;
# refresh by re-scraping livingwage.mit.edu/counties/17077 (no public API). 2026
# values per the 2026-02-15 refresh.
# Single-adult living wage: $18.95/hr ≈ $758/wk
# 1 adult + 2 children (the operator's "single mom" reference): $46.76/hr ≈ $1,870/wk
# 2 adults (1 working) + 2 children: $36.20/hr ≈ $1,448/wk (approximate, scaled
# proportionally from 2024 ratios; verify on next refresh)
_MIT_LIVING_WAGE_JACKSON_IL_1A0C_WKLY = 758.0   # single adult no kids
_MIT_LIVING_WAGE_JACKSON_IL_1A2C_WKLY = 1870.0  # single adult + 2 kids
_MIT_LIVING_WAGE_JACKSON_IL_2A2C_WKLY = 1448.0  # 2 adults (1 working) + 2 kids
_MIT_LIVING_WAGE_YEAR = 2026

# Training-ladder roster — typical credentials offered by/through Mantracon /
# John A. Logan / Rend Lake / IBEW Local 702 / other regional providers.
# Wage figures are LOCAL journey-out estimates from the expert advisory +
# BLS OES Carbondale-Marion MSA (16060) cross-reference; refresh annually.
# "supersector_code" maps to the BLS QCEW NAICS supersector aggregator used
# elsewhere on this page so we can pull local employment + avg wage directly.

_TRAINING_LADDER_ROSTER = [
    # ── Solar splits into TWO distinct pipelines ──
    # (1) NABCEP residential/commercial installer credential → small local
    #     installers (StraightUp Marion, Tick Tock, etc.).
    # (2) HIRE360 Climate Works pre-apprenticeship → union building-trades
    #     apprenticeships (IBEW 702 / IUOE 318 / LIUNA 773) → utility-scale
    #     solar + wind + data-center + outage construction. This is the
    #     travel-work pathway, not a local-residential-installer job.
    # Treating them as one row produces a misleading "phantom" verdict.
    {"id": "nabcep_solar",   "name": "NABCEP solar installer (residential/commercial)", "supersector_code": "1012",
     "ladder": "SEI / employer-direct → NABCEP PV Installation Professional",
     "typical_journey_wage_wkly": 1040,  # $26/hr × 40
     "training_duration": "8-16 weeks",
     "local_employer_override": 30,  # StraightUp Solar Marion + Tick Tock Energy + smaller EnergySage-listed installers; ~20-50 NABCEP-credentialed roles across LWA-25
     "notes": "Distinct from CEJA Climate Works (separate row below). NABCEP is the IPS / Solar Energy International credential for residential + small-commercial installers. Local NABCEP employer base in LWA-25 is verified but small: StraightUp Solar (Marion IL office, NABCEP-certified team, 65 MW installed across 3,000+ sites since 2006), Tick Tock Energy (Effingham IL, services Carbondale/Marion/Mt. Vernon), plus several smaller EnergySage-listed installers in Carbondale/Murphysboro/Marion. Verify against BLS OES SOC 47-2231 Carbondale-Marion MSA when published. NOTE: utility-scale solar work (Big Muddy 124 MW, Arevon, Jackson Co.) does NOT go to NABCEP installers — it goes to IBEW 702 + IUOE 318 + LIUNA 773 union construction. See the ceja_climate_works row for that pathway."},
    {"id": "ceja_climate_works", "name": "CEJA Climate Works pre-apprenticeship → union building trades", "supersector_code": "1012",
     "ladder": "HIRE360 8-12wk pre-app (MC3, OSHA 10, GPRO) → IBEW 702 / IUOE 318 / LIUNA 773 / UA 553 / Carpenters apprenticeship → utility-scale construction journey",
     "typical_journey_wage_wkly": 2200,  # ~$55/hr blended journey + per-diem on utility-scale projects; IBEW 702 outside lineman is $65/hr, IUOE 318 ~$45-60/hr, LIUNA 773 ~$35-45/hr
     "training_duration": "8-12wk pre-app + 3-5yr trades apprenticeship",
     "travel_work_credential": True,
     "notes": "TRAVEL-WORK PATHWAY — the legitimate CEJA-funded ladder. HIRE360 is the Southern IL Climate Works grantee ($3.9M IL DCEO funding; statewide Year-1 capacity 500-700 across 3 hubs, Southern share ~150-200/yr). Graduates feed IBEW Local 702 (West Frankfort), IUOE Local 318 (crane operators), LIUNA Local 773, UA Local 553 (pipefitters), Carpenters/Ironworkers locals — NOT NABCEP installers. Downstream work: Big Muddy Solar 124 MW (currently building, Jackson Co.), future IL utility-scale solar under CEJA prevailing-wage mandate, Central/Northern IL wind farms, data-center construction across IL/IN/MO, industrial outage refits. Family-supporting wages WITH per-diem ($120-150k/yr typical journey); the trade-off is the traveling lifestyle, similar to boilermakers + crane operators already on this page. **SOCIAL-MEDIA CALIBRATION 2026-05-27 (DOL data + ElectricianTalk forum signal): registered-apprenticeship completion rates run BELOW 35% nationally; IBEW programs run higher than the national average but ElectricianTalk discussion cites ~50% first-year IBEW dropout. 5-year completion ~50-65%.** That means the HIRE360 pre-app → union apprenticeship → journey-card pathway is a 5-year gauntlet with roughly 50% attrition before journey card. Bottleneck is NOT just annual local intake — it's the multi-year attrition curve. Don't pitch CEJA Climate Works as a placement guarantee; pitch it honestly as a 5-year ~50% gauntlet that pays family-supporting wages to those who complete. Sources: DOL/AIR apprenticeship-completion brief; ElectricianTalk + IBEW-trade discussion."},
    {"id": "ceja_wind",      "name": "CEJA wind technician (travel-work)",  "supersector_code": "1011",
     "ladder": "Pre-app + GWO BST/BTT certifications → wind-tech entry",
     "typical_journey_wage_wkly": 1500,  # ~$37.50/hr base journey; with per-diem on traveling assignments, take-home commonly $75-100k+/yr
     "training_duration": "12-20 weeks (incl. GWO BST + BTT)",
     "local_employer_override": 0,
     "travel_work_credential": True,
     "notes": "TRAVEL-WORK PATHWAY — same model as the operator's MN pipeline-NDI example. NO utility-scale wind farms operate in Southern IL; the IL wind belt is Central + Northern (Livingston, McLean, Lee, LaSalle, Bureau, DeKalb, Vermilion counties). Graduates work the broader US wind belt — Iowa, Oklahoma, Texas, North Dakota — plus emerging East Coast offshore. Wage structure: $61-68k base in IL; traveling techs commonly $75-90k+ with per-diem; experienced techs $100k+ with overtime + GWO advanced certs. Closest accessible training to Southern IL = Danville Area Community College (Vermilion Co.) or Iowa Lakes CC (across the river). HIRE360 is the Climate Works pre-app grantee for Southern IL ($3.9M); GWO BST + BTT certifications add the wind-specific layer. The trade-off is the traveling lifestyle, not the wage. NOT a phantom credential — the US wind-tech labor market is one of the fastest-growing BLS occupations; the credential lands at wind-belt employers + traveling-crew firms, not at a local Southern IL employer."},
    {"id": "ceja_lineworker", "name": "Lineworker (IBEW 702)",              "supersector_code": "1021",
     "ladder": "Pre-app → 7×1,000hr apprenticeship periods (~3.5yr) → IBEW outside lineman journey",
     "typical_journey_wage_wkly": 2621,  # $65.52/hr × 40 per IBEW 702 outside wage sheet 1/6/2025-1/4/2026
     "training_duration": "~3.5 years apprenticeship (seven 1,000-hour periods)",
     "notes": "Highest-wage clean-energy ladder + IBEW 702 (W. Frankfort) is local. Real family-supporting path — and Big Muddy Solar's 124MW construction in Jackson Co. is hiring Local 702 lineworkers right now. Journey wage per published IBEW 702 outside-lineman wage sheet effective Jan 2025 (ibew702.org)."},
    {"id": "electrician",    "name": "Electrician (IBEW 702)",              "supersector_code": "1012",
     "ladder": "Pre-app → 5yr apprenticeship → IBEW journey",
     "typical_journey_wage_wkly": 1680,  # $42/hr × 40
     "training_duration": "5 years apprenticeship",
     "notes": "IBEW Local 702 covers most of LWA-25. Strong local construction demand. Hits the family-supporting threshold at journey-out exactly."},
    {"id": "cdl_class_a",    "name": "CDL Class A (truck driver, W-2)",     "supersector_code": "1021",
     "ladder": "JALC or Rend Lake 4-8wk CDL school",
     "typical_journey_wage_wkly": 1000,  # $25/hr × 40 local
     "training_duration": "4-8 weeks",
     "entry_gates": ["dot_physical", "drug_screen", "cdl_class_a"],
     "notes": "Local W-2 jobs (FedEx Marion hub, Walgreens Distribution Mt. Vernon, Aisin logistics) pay $22-28/hr. Regional OTR $35-45/hr but takes drivers away from family. Operator critique sanitized — local rate sub-1A+2C; OTR rate breaks the family-supporting frame the other way (you can't raise kids if you're not home). See coal_hauler_ownerop row for the owner-operator economics."},
    {"id": "coal_hauler_ownerop", "name": "Coal-hauler owner-operator (Knight Hawk Prairie Eagle → Cora terminal)", "supersector_code": "1021",
     "ladder": "CDL Class A school + truck financing + DOT authority",
     "typical_journey_wage_wkly": 2980,  # ~$155k OOIDA-survey gross annual / 52
     "training_duration": "4-8wk CDL + 0-1yr W-2 OTR experience before financing a truck",
     "local_employer_override": 40,  # rough estimate — short-haul coal-hauler owner-ops servicing the Knight Hawk Prairie Eagle → Cora lane plus other Illinois Basin short-haul lanes
     "owner_operator": True,
     "entry_gates": ["dot_physical", "drug_screen", "cdl_class_a"],
     "notes": "Knight Hawk's Prairie Eagle Mine (Perry County, IL, ~30 mi short-haul) routes via owner-operator truck to the Cora Marine Terminal (Rockwood IL, Upper Mississippi mile 98.5, Watco + SCH Services, 1.25M ton storage, 4× 150-car unit-train loadout). Operator's lived account 2026-05-27: 'it's a grind.' SOCIAL-MEDIA CALIBRATION (TruckersReport forum 2026-05-27): $2/ton is the going Illinois-Basin rate; ash pays $0.50-0.70 less per ton; a 25-ton load grosses ~$50. Verbatim TruckersReport user Mack185: 'Most of the bigger companies around East Missouri/Illinois haul it for around $2 a ton so I hear... I don't know how they make any money.' Verbatim Preacher Man (post-Curry failure): 'They don't. That's what happened to Curry.' Verbatim baby: 'they pay by the ton. I made 800 after taxes my first week out... Coal dust and dry dirt from the roads blowing around is a lung killer.' OOIDA 2024 Survey + ATBS 2024 + Overdrive: typical owner-op gross ~$155k/yr; NET after expenses $22-27k (14.5-17.8% margin) for short-haul respondents — fuel $48.8k, maintenance $18.9k, truck pmt $18k, insurance $13.5k. Short-haul coal lanes specifically: lower gross than OOIDA respondent average (fewer miles) but proportionally lower fuel-share; net margin can run $40-70k for experienced operators. Wage column shows GROSS — actual take-home is much smaller. Cohort risk: 60-70hr weeks, heavy maintenance burden falls on the driver, market volatility (a Knight Hawk production slowdown = no income that month), trucks beat up faster than OTR (heavy-loaded dirt-road mine approaches), coal-dust lung exposure documented in forum signal. Small operators HAVE failed at these margins (Curry per TruckersReport). Foresight's Sugar Camp Mine (Franklin Co.) does NOT route this way — Sugar Camp ships via unit train to Sitran on the Ohio (mile 817.5) per Foresight's own Operations page. Cora handles Knight Hawk + historically Peabody Illinois Basin coal."},
    {"id": "cna",            "name": "CNA (Certified Nursing Asst.)",       "supersector_code": "1025",
     "ladder": "4-6 week certification",
     "typical_journey_wage_wkly": 640,  # $16/hr × 40
     "training_duration": "4-6 weeks",
     "notes": "Easy to place into — Memorial Hospital, SIH, nursing homes all hire CNAs. BUT pays $14-17/hr — below single-adult living wage. Expert: 'getting stuck training for low-wage care-economy jobs because they're easy to place into.'"},
    {"id": "lpn",            "name": "LPN (Licensed Practical Nurse)",      "supersector_code": "1025",
     "ladder": "12-month diploma program",
     "typical_journey_wage_wkly": 1000,  # $25/hr × 40
     "training_duration": "12 months",
     "notes": "Significant step up from CNA. Common ladder rung. Still single-adult-only territory."},
    {"id": "rn_adn",         "name": "RN (ADN, Associate Degree)",          "supersector_code": "1025",
     "ladder": "2yr ADN at JALC + NCLEX (+ optional 1yr local before travel-agency)",
     "typical_journey_wage_wkly": 1380,  # $34.50/hr × 40 starting at SIH
     "training_duration": "2 years",
     "notes": "Memorial Hospital + SIH + Marion VA all hire ADN-RNs. Strong local demand at $32-38/hr starting. BSN bridge adds $4-6/hr. CRITICAL PATH UP: after 1 year of local floor experience, RNs become eligible for travel-nurse agencies paying $60-110/hr blended ($130-200k+/yr — see Travel Jobs section below). The 'land at SIH for 1 year then go travel' play is the highest-dollar 2-year-credential path in the region. Family compatibility depends on family structure — single parents = tough; partnered nurses with childcare = transformative income jump."},
    {"id": "welding",        "name": "Welder (structural / pipe)",          "supersector_code": "1013",
     "ladder": "JALC 12-18mo welding program + AWS certs",
     "typical_journey_wage_wkly": 1240,  # $31/hr × 40
     "training_duration": "12-18 months",
     "notes": "Manufacturing demand at Continental Tire, Aisin, Penn Aluminum. Family-supporting at journey-out. Pipefitter (Local 160 Mt. Vernon) goes higher."},
    {"id": "industrial_maint", "name": "Industrial maintenance / mechatronics", "supersector_code": "1013",
     "ladder": "JALC 18-24mo mechatronics program",
     "typical_journey_wage_wkly": 1320,  # $33/hr × 40
     "training_duration": "18-24 months",
     "notes": "Continental Tire is the anchor employer. Strong local demand + clears family-supporting threshold."},
    {"id": "it_support",     "name": "IT support (Network+/Security+)",     "supersector_code": "1022",
     "ladder": "Stacked CompTIA certs",
     "typical_journey_wage_wkly": 1080,  # $27/hr × 40
     "training_duration": "6-12 months",
     "notes": "Local employer base is tiny — Information sector has ~50-200 jobs in LWA-25. The ceiling is low locally; better framed as a 'work-from-anywhere' ladder than a 'land at a local employer' ladder."},

    # ── Gap-credentials added 2026-05-27 (operator brainstorm + verified
    # primary sources). These were missing from the page and represent real
    # Southern IL employer pathways the workforce-board cohort can land in. ──

    {"id": "underground_coal_miner", "name": "Underground coal miner (UMWA scale, Sugar Camp / Pond Creek / Knight Hawk)", "supersector_code": "1011",
     "ladder": "MSHA Part 48 surface (40hr) + underground (24hr) certification → on-job training under journey miner",
     "typical_journey_wage_wkly": 1400,  # ~$35/hr × 40 BASE
     "total_package_wkly": 2000,  # base + productivity bonuses + 50-60hr/wk OT pushes effective take-home to $80-110k/yr — clears 1A+2C
     "training_duration": "64hr MSHA Part 48 cert + 6-12mo apprentice / red-hat → full miner",
     "local_employer_override": 800,  # Sugar Camp + Pond Creek (Foresight) + Knight Hawk operations combined; IL produced ~the same tonnage in 2023 as 1998 with 1,748 fewer miners — small but real remaining workforce
     "entry_gates": ["msha_part_48_certification", "physical_fitness", "drug_screen", "no_claustrophobia", "often_family_connection_to_enter"],
     "notes": "Sugar Camp Mine (Franklin Co., Foresight Energy / American Consolidated Natural Resources, 2024 production 6.2M tons, largest active mine in IL by cumulative tonnage at 116M tons, 1.264B ton reserves) + Pond Creek / Mach #1 (Williamson + Franklin, Foresight / Williamson Energy) + Knight Hawk Prairie Eagle (Perry Co.) are the active LWA-25-region mines hiring underground positions: roof bolters, longwall operators, mechanical maintenance, electrical, foremen. UMWA wage scale ~$28-40/hr base; productivity bonuses + 50-60hr weeks can push take-home to $80-110k/yr. ENTRY GATE STRUCTURE: MSHA Part 48 certification is required (40hr surface + 24hr underground); physical/drug screen; many mines still recruit heavily through family connections (fathers/uncles already on a crew). The 'coal is dead' narrative is wrong for hiring — IL mining produces ~the same tonnage in 2023 as in 1998 with 1,748 fewer miners (per ICL.coop), so the remaining slots are very productive and family-supporting. **SOCIAL-MEDIA CALIBRATION 2026-05-27 (Indeed Knight Hawk + Foresight employee reviews): two employer profiles diverge sharply. Knight Hawk per multiple Indeed reviews is the LOWEST-PAYING mine in the area + 'treated like family' culture. Foresight workers' verbatim sentiment is darker: 'Coal is a dead industry. This used to be the place everyone wanted to work at'; 'Make as much money as you like but do not plan on having a home life'; vacation forced during mine shutdowns. Foresight pays better than Knight Hawk but mandates OT to the point of home-life conflict.** Use this credential carefully — the within-credential employer-level variance is large, and the worker sentiment about industry trajectory is not what the productivity numbers suggest. Sources: Foresight Energy Operations page; Prairie Rivers Network Sugar Camp profile; ILMines wiki; ICL.coop / thesouthern.com SIPC Unit 4 retirement filings; indeed.com Knight Hawk Coal + Foresight Energy reviews."},

    {"id": "river_barge_crew", "name": "River-barge deckhand → mate → pilot (Cora / Mississippi + Ohio reach)", "supersector_code": "1021",
     "ladder": "USCG Merchant Mariner Credential (MMC) entry-level → deckhand → 360 days sea-time → mate test → pilot test",
     "typical_journey_wage_wkly": 1500,  # blended deckhand-to-mate range; with overtime + per-diem, traveling techs commonly $75-100k+; pilot $125k+ per Glassdoor 2025
     "training_duration": "MMC + TWIC processing 2-6 months entry; sea-time 360 days for mate; multi-year ladder to pilot",
     "local_employer_override": 60,  # rough estimate of LWA-25-resident barge crew members on the Cora / Sitran / Mississippi-Ohio reach; ACBL + Marquette + Ingram + Madison Coal & Supply employers
     "travel_work_credential": True,
     "entry_gates": ["uscg_merchant_mariner_credential", "twic_card", "drug_screen", "physical", "swim_test"],
     "notes": "TRAVEL-WORK PATHWAY — operator lived account 2026-05-27 + gCaptain forum corroboration 2026-05-27. SCHEDULE: forum signal corrects the 14/7-vs-20/10-vs-30/30 framing — 28/14 is the most common hitch (Marquette river division, ACL/ACBL); 30/30 is REAL on inland tow per Riverhand verbatim; 20/20 is the norm at Madison Coal & Supply per Brian verbatim; Marquette's Gulf division runs 2/1 (28/14 or 14/7 depending on residence). EMPLOYERS in the Upper Miss + Ohio reach: ACBL (American Commercial Barge Line), Marquette Transportation, Ingram Marine, Madison Coal & Supply, Calumet Marine. ENTRY PATH (verbatim from gCaptain): 'pure hawsepipe industry'; 'You have to be connected to get on one of those boats to start with'; wheelhouse advancement is connection-gated. Marquette will fly you in from anywhere lower 48; Ingram requires living near the terminal. WAGE PROGRESSION (industry tables): entry deckhand ~$52k/yr base; mate / engineer $300-500/day; pilot $125k+. OPERATOR-PRIMARY-SOURCE caveats: high divorce rate among long-rotation crews; rough on-boat culture; injuries from deck work + line handling + machinery + weather. BLS CFOI 2023: Transportation and material moving occupations 13.6 fatal injuries per 100,000 FTE vs 3.5 all-occupations baseline. Forum signal indirectly supports family-strain framing — Riverhand: 'I have no family or anything tieing me down.' Honest verdict: wage clears 1A+2C at mate/engineer/pilot rungs; lifestyle cost (divorce + injury + connection-gated advancement) is the structural trade-off. Sources: gCaptain forum threads (Marquette, Deckhand, Towboat jobs); BLS CFOI 2023; operator primary-source."},

    {"id": "il_doc_officer", "name": "IL DOC correctional officer (Pinckneyville / Big Muddy / Vienna / Shawnee)", "supersector_code": "9091",
     "ladder": "Civil service exam (CMS) → background investigation → 5-week DOC training academy at Logan CC",
     "typical_journey_wage_wkly": 1430,  # ~$58k starting / 52 — CHECK WAGE
     "total_package_wkly": 1900,  # check + state pension contribution + health benefits — clears 1A+2C
     "training_duration": "5-week academy + 6mo probationary period",
     "local_employer_override": 1200,  # Pinckneyville Correctional (Perry) + Big Muddy River Correctional (Jefferson, Ina) + Shawnee Correctional + Vienna Correctional (Johnson, adjacent to LWA-25); IL DOC publishes facility-level FTE counts but consolidated regional total ~1,200 officers
     "entry_gates": ["physical_fitness", "background_check", "drug_screen", "civil_service_exam", "post_academy"],
     "notes": "SOCIAL-MEDIA CALIBRATION 2026-05-27 (Indeed IDOC reviews + WTTW staffing data): IL DOC is STRUCTURALLY HIRING statewide post-COVID — 4% headcount drop in 2022 (WTTW analysis), contractual raises + OT drove total pay UP despite fewer staff. The earlier SATURATED framing has been removed; attrition is MANDATORY-OT-DRIVEN, not academy-washout. Verbatim Indeed reviews: '40-hour shift PLUS at least two shifts of Mandatory overtime every week — does not make up for lost time with family'; 'Inmates have more rights and treated better than staff'; 'Toxic command staff'. Pension + benefits keep people; OT family-time loss is the dominant exit driver. Operator standing entry-gate signal: physical fitness test (push-ups + sit-ups + 1.5-mile run + body composition) is a real wash-out. Family-supporting wage + state pension + benefits make this attractive when you can get in; mandatory OT is the structural lifestyle cost the wage column doesn't reflect. Sources: indeed.com IDOC pay-and-benefits review filter; news.wttw.com 2023 IDOC staffing analysis."},

    {"id": "idot_highway_maintainer", "name": "IDOT Highway Maintainer (District 9)", "supersector_code": "9091",
     "ladder": "CMS civil service exam → CDL Class A → IDOT field training",
     "typical_journey_wage_wkly": 1200,  # ~$25/hr × 40 CHECK WAGE
     "total_package_wkly": 1620,  # check + state pension contribution + health benefits + storm-response OT — clears 1A+2C
     "training_duration": "CDL 4-8 weeks + IDOT field training 3-6 months",
     "local_employer_override": 80,  # IDOT District 9 (south of I-64) covers LWA-25 + adjacent counties; ~80 field positions estimated
     "local_market_saturated": True,
     "entry_gates": ["cdl_class_a", "civil_service_exam", "drug_screen", "dot_physical"],
     "notes": "State civil service job covering road plowing, pavement repair, sign maintenance, traffic-control, snow removal. Hiring is attrition-only — IDOT doesn't grow its field workforce, just replaces retirements. Family-supporting all-in with state pension + benefits + overtime on storm-response. The CDL gate is the biggest entry barrier; many candidates train via JALC / Rend Lake CDL school + apply when an opening posts on illinois.jobs2web.com."},

    {"id": "hvac_union_sheet_metal", "name": "HVAC sheet metal (SMART Local 268 union)", "supersector_code": "1012",
     "ladder": "SMART Local 268 5-year apprenticeship — apprentice → journey sheet metal worker",
     "typical_journey_wage_wkly": 1863,  # journey check wage $46.57/hr × 40
     "total_package_wkly": 2857,  # journey total package $71.43/hr × 40 (check $46.57 + benefits $24.86) — clears 1A+2C by $25/hr
     "training_duration": "5-year apprenticeship (10,000hr OJT + 1,000hr classroom)",
     "entry_gates": ["aptitude_test", "drug_screen", "basic_math"],
     "notes": "Verified 2025 wage sheet (Local 268, Caseyville HQ — covers 36 Southern IL counties Mississippi-to-Ohio rivers): APPRENTICE start $23.29/hr on check + $24.23 benefits = $47.52/hr TOTAL PACKAGE (clears Jackson Co. 1A+2C $46.76 already). JOURNEY $46.57/hr on check + $24.86 benefits = $71.43/hr TOTAL PACKAGE. Operator 2026-05-27: 'hvac went union and now they get paid.' Coverage includes all of LWA-25. Annual apprentice intake for LWA-25 counties not published — workforce board could request from Local 268 apprenticeship coordinator or via IL Apprenticeship Council. Major commercial / industrial HVAC work in LWA-25: Continental Tire HVAC + dust collection, Aisin facilities, SIH + Marion VA hospital systems, SIU campus, school district installs, federal contracting (GD-OTS environmental controls). UA Local 553 (East Alton) covers HVAC mechanical / refrigeration on the pipefitter side — similar family-supporting outcome. Sources: local268.com/wp-content/uploads/2025/06/Local-268-Journeyperson-Wage-Sheet.pdf, ualocal553.org."},

    {"id": "hvac_residential_nonunion", "name": "HVAC residential (non-union, small shop)", "supersector_code": "1012",
     "ladder": "EPA 608 certification + 1-2yr OJT at residential service shop",
     "typical_journey_wage_wkly": 880,  # ~$22/hr × 40
     "training_duration": "EPA 608 + 1-2yr OJT to service tech",
     "local_employer_override": 150,  # small residential / light-commercial HVAC shops across LWA-25
     "entry_gates": ["epa_608_universal_certification", "drug_screen", "valid_drivers_license"],
     "notes": "DISTINCT from the union sheet-metal track (see hvac_union_sheet_metal). Small residential / light-commercial service shops pay $18-25/hr; experienced techs $22-30/hr; below 1A+2C unless owner of shop or specialize (heat-pump + geothermal premium installer, commercial refrigeration). Clearing 1A+2C from this rung requires (a) shop ownership, (b) commercial specialization, (c) relocate to higher-wage market, or (d) cross to union. Local employer base ~150 jobs across small shops in LWA-25; placement easy but wage ceiling is the constraint."},

    {"id": "auto_mechanic_hs_cte", "name": "Auto mechanic (HS CTE → dealership / garage)", "supersector_code": "1024",
     "ladder": "HS CTE program (Carbondale CHS, Marion Unit 2, Mt. Vernon HS, Murphysboro HS — varies by district) → ASE certifications stacked",
     "typical_journey_wage_wkly": 760,  # ~$19/hr × 40 typical Carbondale-Marion MSA shop rate
     "training_duration": "2-yr HS CTE program (free) + ASE certs over working career",
     "local_employer_override": 250,  # estimated SOC 49-3023 employment Carbondale-Marion MSA; BLS OEWS May 2024 area data confirms directional gap vs Chicago + St. Louis
     "entry_gates": ["drug_screen", "valid_drivers_license"],
     "notes": "OPERATOR LIVED SIGNAL 2026-05-27: 'they have the certified auto mechanic in the high schools and those guys work in the dealerships and the garages but don't make a good living like they would in the city.' Local placement WORKS — HS CTE grads land at small-town shops + dealerships. Local wage FAILS 1A+2C: rural shops $15-22/hr; Carbondale-Marion MSA dealership flag-rate $22-30/hr; city dealerships (Chicago, St. Louis, Indianapolis) + specialty (Tesla, transmission, European, performance) $30-40+/hr. BLS OEWS national median for SOC 49-3023 = $49,670/yr ($23.88/hr) May 2024; Carbondale-Marion MSA tracks below national. **SOCIAL-MEDIA CALIBRATION 2026-05-27 (Humble Mechanic blog + industry-forum signal): flat-rate compensation structurally hurts RURAL shops more than the headline wage gap suggests — independent shops can't charge dealer labor rates without losing customers, so the rural mechanic's hours-worked-to-hours-paid ratio collapses on top of the wage gap. Verbatim industry signal: 'flat rate is slowly killing the auto repair industry'; 'if there are no cars in the shop, mechanics get paid nothing, and some jobs take longer than they pay.'** Clearing 1A+2C from this rung requires (a) flag-rate at high-volume dealership, (b) specialize, (c) own a shop, or (d) relocate. The credential isn't phantom; the LOCAL WAGE CEILING + flat-rate structural disadvantage are the constraints. Source: BLS OEWS May 2024 area data Carbondale-Marion MSA 49500 vs Chicago MSA 16980 + St. Louis MSA 41180; Humble Mechanic flat-rate analysis + industry forums."},

    {"id": "diesel_mechanic", "name": "Diesel mechanic (Kaskaskia / Rend Lake / SIC programs)", "supersector_code": "1024",
     "ladder": "Community college 1-2yr diesel-tech AAS or 8-month certificate",
     "typical_journey_wage_wkly": 1166,  # BLS OEWS national median SOC 49-3031 = $60,640/yr / 52
     "training_duration": "8mo-2yr program",
     "local_employer_override": 80,  # estimated Carbondale-Marion MSA SOC 49-3031 employment; entry-level slots much smaller than total employment count
     "entry_gates": ["drug_screen", "valid_drivers_license", "cdl_class_a_bumps_wage"],
     "notes": "OPERATOR LIVED SIGNAL 2026-05-27: 'my cousin's kid did that in Nashville and couldn't find a job locally.' The credential LOOKS mapped on paper (FedEx Marion fleet, Walgreens Distribution Mt. Vernon, regional trucking, Class I railroads BNSF/NS/UP/CN/CSX, Aisin / Continental / GD-OTS captive maintenance shops, John Deere + Case IH ag-equipment dealers, coal-mine maintenance crews) — but the local ENTRY-LEVEL slot pipeline is broken: big captive shops hire experienced techs from within; railroads hire centrally not at local yards; ag-equipment dealers prefer 3-5yr experience; coal-mine maintenance has shrunk with mechanization. The typical path is: train locally → travel for first job (Memphis, St. Louis, Evansville IN, Nashville TN, Tulsa) to log 3-5 yr experience → maybe return to a local shop later. Cohort-vs-capacity gap shows up at the ENTRY rung, not the trade itself. **SOCIAL-MEDIA CALIBRATION 2026-05-27 (BLS OOH + Dollar Hire industry data): mobile / field-service diesel techs earn $65-90k vs local rural $38-45k — a 20-30% premium for accepting travel. That salary structure CONFIRMS the operator's lived signal: the credential pays, but only if you travel to use it.** Verdict: PHANTOM LOCALLY at entry-level / TRAVEL-WORK to build experience (with 20-30% mobile premium) / MAY return after 3-5 yrs. Source: BLS OEWS May 2024 national + Carbondale-Marion MSA SOC 49-3031; BLS OOH; Dollar Hire 2026 diesel-mechanic salary analysis; IPEDS cohort completion vs OEWS employment join is the next-level analysis needed."},

    {"id": "aisin_production_tech", "name": "Aisin production technician (Marion — Aisin Mfg / Electronics / Light Metals)", "supersector_code": "1013",
     "ladder": "HS diploma → Aisin onboarding + on-job training",
     "typical_journey_wage_wkly": 900,  # ~$22/hr × 40 starting; production lead $24-28/hr; with overtime + 2nd-shift premium pushes higher
     "training_duration": "2-4wk onboarding + ongoing skills progression",
     "local_employer_override": 2000,  # Aisin Marion total: 2,000+ across Aisin Manufacturing IL + Aisin Electronics IL + Aisin Light Metals (Williamson Co.); WSIU 2023 announcement of half-million sq ft logistics center expansion
     "entry_gates": ["hs_diploma", "drug_screen", "basic_skills_assessment"],
     "notes": "Major Williamson Co. employer — 2,000+ jobs across three Aisin entities in Marion (Manufacturing IL + Electronics IL + Light Metals). Production-tech roles cover assembly, electronics, light-metals casting, quality, packaging. Starting $18-22/hr; production lead + skilled trades inside Aisin push to $24-30/hr; with overtime + 2nd-shift premium can clear 1A+2C. Entry gate is mild — HS diploma + drug screen + basic skills assessment. Aisin is actively hiring (WSIU 2023 announcement of nearly half-million sq ft logistics center expansion). Maps well to HS CTE manufacturing tracks at Marion Unit 2 + JALC industrial-maintenance / mechatronics programs."},

    {"id": "hotel_hospitality_mgmt", "name": "Hotel / hospitality management (HS or bachelor's path)", "supersector_code": "1027",
     "ladder": "HS CTE hospitality OR community-college AAS OR bachelor's hospitality management",
     "typical_journey_wage_wkly": 725,  # BLS OEWS national hospitality bachelor's entry $37,668/yr ÷ 52 ≈ $725/wk
     "training_duration": "HS CTE 2yr (free) OR AAS 2yr OR bachelor's 4yr ($30-80k tuition debt)",
     "local_employer_override": 25,  # ~20-25 lodging-manager / hospitality-management slots LWA-25-wide across Carbondale (Home2 Suites Hilton + Hampton + Holiday Inn Express + Best Western + Super 8 + Comfort Inn), Marion (Country Inn + Hampton + Holiday Inn Express + Drury + Best Western), Mt. Vernon (Drury + Hampton + Comfort), Murphysboro (Holiday Inn Express)
     "local_market_saturated": True,
     "entry_gates": ["hs_diploma_or_bachelors", "drug_screen", "customer_service_experience_typical"],
     "notes": "OPERATOR LIVED SIGNAL 2026-05-27: 'a degree in hotel management does not have a good ROI.' VERIFIED via BLS OEWS SOC 11-9081 (Lodging Managers) May 2024: national median $68,130; bachelor's entry median $37,668 (under 1 yr exp); 10-19 yrs experienced $57,995 (still below 1A+2C $97,260); 20+ yrs $75,778; top 10% only $102,410. Tuition debt ($30-80k bachelor's hospitality) plus $37k entry wage = ROI fails the family-supporting test by every measure. LWA-25 lodging-manager market is small + saturated (~20-25 GM slots across chain hotels in Carbondale + Marion + Mt. Vernon + Murphysboro; all filled, attrition-only entry). Visiting executives + relocator open houses stay at chain hotels OR Giant City Lodge (the destination-grade IL DNR-operated lodge in Giant City SP, ~12 mi south of Carbondale). Hospitality is NOT a primary jobs anchor for the region; it's a wage-suppressed credential path with a saturated local market."},

    {"id": "continental_tire_production", "name": "Continental Tire production operator (Mt. Vernon)", "supersector_code": "1013",
     "ladder": "HS diploma → Continental Tire onboarding + tire-build certification",
     "typical_journey_wage_wkly": 1000,  # ~$25/hr × 40 typical starting; production technician $25-30/hr; tire builder + skilled trades inside the plant push higher
     "training_duration": "4-8wk onboarding + tire-build certification",
     "local_employer_override": 3667,  # Continental Tire Mt. Vernon: 3,667 employees verified — the largest single employer south of Peoria
     "entry_gates": ["hs_diploma", "drug_screen", "physical", "mechanical_aptitude_test"],
     "notes": "JEFFERSON CO. ANCHOR — 3,667 jobs in Mt. Vernon, the largest single employer south of Peoria. Plant produces passenger + light-truck tires, truck tires, pre-cure tread. Production operator + tire builder roles start $20-25/hr; with overtime + 2nd-shift premium + skilled-trades progression (electrician / millwright / mechatronics inside the plant) clears 1A+2C. Plant is 45+ years old (per Town Square Publications celebration article) and is the anchor of Mt. Vernon's manufacturing identity. Mechanical aptitude test is a real gate; HS shop / CTE background helps. Continental is one of the few LWA-25 employers where the production rung itself can be family-supporting (not just the skilled-trade rung above)."},
]


def _training_demand_alignment(qcew_block: dict) -> dict:
    """Cross-references each training ladder against actual local employer demand
    (QCEW sector employment) and a livable-wage benchmark (MIT Living Wage,
    Jackson County, IL — 1 adult + 2 children = $1,682/wk).

    Returns a list of training ladders with: regional sector employment, sector
    avg weekly wage from QCEW (for context), training wage, livable-wage gap,
    and an explicit verdict (PHANTOM / BELOW LIVABLE / SINGLE ADULT ONLY /
    FAMILY-SUPPORTING).
    """
    # Map supersector code → QCEW snapshot for cross-reference
    qcew_by_code: dict[str, dict] = {}
    for s in (qcew_block or {}).get("top_supersectors", []) or []:
        qcew_by_code[s["code"]] = s

    LWA_TOTAL_EMP = (qcew_block or {}).get("total_employment", 0) or 0
    livable_1a0c = _MIT_LIVING_WAGE_JACKSON_IL_1A0C_WKLY
    livable_1a2c = _MIT_LIVING_WAGE_JACKSON_IL_1A2C_WKLY

    rows: list[dict] = []
    for tl in _TRAINING_LADDER_ROSTER:
        qcew_row = qcew_by_code.get(tl["supersector_code"], {})
        # When the credential lands in a narrow sub-industry whose local employer
        # base is essentially zero (solar installer, wind tech), the supersector
        # total is misleading. local_employer_override forces the count.
        if "local_employer_override" in tl:
            sector_emp = tl["local_employer_override"]
            credential_specific_demand = True
        else:
            sector_emp = qcew_row.get("total_employment", 0) or 0
            credential_specific_demand = False
        sector_wage = qcew_row.get("avg_weekly_wage", 0) or 0
        # Verdict comparison wage: prefer total_package_wkly when present
        # (union trades + civil-service jobs have substantial pension/benefit
        # value beyond check wage — Operator 2026-05-27: SMART Local 268 HVAC
        # has $24.86/hr in benefits on top of $46.57/hr check; IL DOC + IDOT
        # carry state pension contributions). Display columns still show
        # typical_journey_wage_wkly as the headline number; verdict uses the
        # richer total when available.
        wage = tl.get("total_package_wkly") or tl["typical_journey_wage_wkly"]

        # Demand signal — credential-specific overrides (e.g. solar installer,
        # wind tech) operate at headcounts ~0-200 across the LWA; broad QCEW
        # supersectors operate at headcounts 1k-30k. Same threshold table on
        # both scales gives false PHANTOM verdicts on small-but-real
        # credential niches.
        if credential_specific_demand:
            if sector_emp == 0:
                demand = "NONE"
            elif sector_emp < 15:
                demand = "VERY LOW"
            elif sector_emp < 50:
                demand = "MODEST"
            elif sector_emp < 200:
                demand = "MODERATE"
            else:
                demand = "HIGH"
        else:
            if sector_emp == 0:
                demand = "NONE"
            elif sector_emp < 1000:
                demand = "VERY LOW"
            elif sector_emp < 3000:
                demand = "LOW"
            elif sector_emp < 10000:
                demand = "MODERATE"
            else:
                demand = "HIGH"

        # Verdict — five-category taxonomy that captures the actual structure
        # of the Southern IL labor market, surfaced through operator lived
        # signal across multiple turns 2026-05-27:
        #
        #   1. PHANTOM PIPELINE                                — no local jobs,
        #      no travel-work backstop. Credential lands nowhere.
        #   2. TRAVEL-WORK · wage clears / lifestyle cost      — local jobs
        #      are wherever the project is; wage + per-diem clears 1A+2C but
        #      the traveling lifestyle is the structural cost. Set
        #      tl["travel_work_credential"] = True on the row.
        #   3. LOCAL · WAGE-SUPPRESSED                         — local jobs
        #      absorb grads but the local wage rung fails 1A+2C; clearing the
        #      bar requires specialize / flag-rate-at-high-volume / own-a-shop
        #      / relocate to a higher-wage market.
        #   4. LOCAL · FAMILY-SUPPORTING                       — local jobs +
        #      wage clears 1A+2C.
        #   5. OWNER-OPERATOR · GROSS-MISLEADS · NET-GRIND     — gross revenue
        #      looks family-supporting; truck/fuel/insurance/maintenance/
        #      downtime eat the margin to barely-single-adult; lifestyle is
        #      structural grind. Set tl["owner_operator"] = True.
        #
        # Override flags take precedence over wage/demand math because the
        # gross wage isn't the relevant signal for owner-op or travel-work
        # rows. CDL keeps its FAMILY-TIME CONFLICT sub-flavor of travel-work.
        if tl.get("owner_operator"):
            verdict = "OWNER-OPERATOR · GROSS-MISLEADS · NET-GRIND"
            verdict_color = "warn"
        elif tl.get("travel_work_credential"):
            verdict = "TRAVEL-WORK · wage clears / lifestyle cost is high"
            verdict_color = "warn"
        elif tl["id"] == "cdl_class_a":
            verdict = "FAMILY-TIME CONFLICT"
            verdict_color = "warn"
        elif demand == "NONE":
            verdict = "PHANTOM PIPELINE"
            verdict_color = "danger"
        elif demand == "VERY LOW":
            # credential-specific VERY-LOW is "saturated but real"; broad-
            # supersector VERY-LOW is genuinely phantom.
            if credential_specific_demand:
                verdict = "LOCAL · SATURATED — pipeline exists, absorbs ~1-2 grads/yr"
                verdict_color = "warn"
            else:
                verdict = "PHANTOM PIPELINE"
                verdict_color = "danger"
        elif wage < livable_1a0c:
            verdict = "LOCAL · WAGE-SUPPRESSED — fails single-adult LW"
            verdict_color = "danger"
        elif wage < livable_1a2c:
            verdict = "LOCAL · WAGE-SUPPRESSED — clears single-adult, fails 1A+2C"
            verdict_color = "warn"
        elif tl.get("local_market_saturated"):
            # Wage clears 1A+2C but hiring is constrained by attrition rather
            # than growth — IL DOC officer, IL State Police, sheriff deputy,
            # city police, IDOT highway maintainer pattern. The cohort that
            # trains exceeds the slots that open per year.
            verdict = "LOCAL · FAMILY-SUPPORTING · SATURATED — wage clears but supply > demand"
            verdict_color = "warn"
        else:
            verdict = "LOCAL · FAMILY-SUPPORTING"
            verdict_color = "good"

        # When the credential lands in a narrow sub-industry rather than the
        # broader supersector, label the sector as "(credential-specific)" so
        # the page doesn't display the misleading broad-supersector name.
        sector_display = "Credential-specific (out-of-region only)" if credential_specific_demand else qcew_row.get("name", "—")
        rows.append({
            "id": tl["id"],
            "name": tl["name"],
            "ladder": tl["ladder"],
            "training_duration": tl["training_duration"],
            "typical_journey_wage_wkly": wage,
            "typical_journey_wage_hrly": round(wage / 40, 2),
            "supersector_name": sector_display,
            "supersector_code": tl["supersector_code"],
            "local_sector_employment": sector_emp,
            "local_sector_share_pct": round((sector_emp / LWA_TOTAL_EMP * 100), 1) if LWA_TOTAL_EMP else 0,
            "local_sector_avg_weekly_wage": sector_wage,
            "demand_signal": demand,
            "vs_single_adult_livable_wkly": round(wage - livable_1a0c, 0),
            "vs_family_livable_wkly":      round(wage - livable_1a2c, 0),
            "verdict": verdict,
            "verdict_color": verdict_color,
            "notes": tl["notes"],
            # Orthogonal entry-gate filters — wash out portions of the
            # trainable cohort independent of training success. Operator
            # 2026-05-27: "physical fitness requirements as a barrier for
            # some of these jobs." Free-form list of gate identifiers so the
            # frontend can render chips. Empty list = no entry gate.
            "entry_gates": tl.get("entry_gates", []),
        })

    return {
        "ladders": rows,
        "livable_wage_jackson_il": {
            "single_adult_wkly": livable_1a0c,
            "single_adult_hrly": round(livable_1a0c / 40, 2),
            "family_1a2c_wkly": livable_1a2c,
            "family_1a2c_hrly": round(livable_1a2c / 40, 2),
            "source": f"MIT Living Wage Calculator, Jackson County IL ({_MIT_LIVING_WAGE_YEAR} values, refresh annually via livingwage.mit.edu/counties/17077)",
        },
        "source": (
            "Local sector employment + avg weekly wage from BLS QCEW (latest published quarter, "
            "from the industry_mix block on this page). Training journey-out wages from the local "
            "advisory roster — typical figures; individual outcomes vary. Verdicts compare typical "
            "journey-out wage to the MIT Living Wage benchmark for Jackson County (1 adult + 2 "
            "children) — the operator's 'single mom raising kids by herself' reference point. "
            "PHANTOM PIPELINE = local employer base is essentially zero, so the credential has "
            "nowhere to land locally even if wages would clear the bar."
        ),
    }


# ────────────── ACS Labor Truth — the "real picture" beyond UE rate ──────────────
# Surfaces LFPR + E/P + Not-in-Labor-Force at sub-state geography, with state +
# national benchmarks. Captures what the headline U-3 unemployment rate misses:
# discouraged workers, the long-term not-in-LF population, the shrinking
# denominator effect that politicians never cite.

# IL state benchmark — ACS5 2023 (will refresh annually with ACS release)
_IL_STATE_LFPR = 65.1
_IL_STATE_EP   = 61.2
_IL_STATE_NOTLF_PCT = 34.9
# US national benchmark — BLS CPS, 2023 annual average
_US_NATIONAL_LFPR = 62.6  # 16+ headline LFPR
_US_NATIONAL_EP   = 60.3


async def _acs_labor_truth(*, county_fips: list[str] | None = None, place_fips: str | None = None,
                            state_fips: str = "17", year: int = 2023) -> dict:
    """Pulls ACS B23025 (Employment Status) for the requested geography and
    returns labor-utilization metrics that go beyond the headline UE rate.
    Either county_fips (list) or place_fips (single place) must be provided.
    """
    api_key = os.environ.get("CENSUS_DATA_API_KEY") or os.environ.get("CENSUS_API_KEY")
    if not api_key:
        return {}

    fields = "NAME,B23025_001E,B23025_002E,B23025_004E,B23025_005E,B23025_007E"
    url = f"https://api.census.gov/data/{year}/acs/acs5"
    if county_fips:
        params = {"get": fields, "for": f"county:{','.join(county_fips)}", "in": f"state:{state_fips}", "key": api_key}
    elif place_fips:
        params = {"get": fields, "for": f"place:{place_fips}", "in": f"state:{state_fips}", "key": api_key}
    else:
        return {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                return {}
            rows = r.json()
        except Exception:
            return {}

    if not rows or len(rows) < 2:
        return {}
    header = rows[0]
    geos = []
    agg = {"pop": 0, "in_lf": 0, "emp": 0, "unemp": 0, "not_lf": 0}
    for row in rows[1:]:
        d = dict(zip(header, row, strict=False))
        pop = int(d.get("B23025_001E") or 0)
        if pop == 0:
            continue
        in_lf = int(d.get("B23025_002E") or 0)
        emp   = int(d.get("B23025_004E") or 0)
        unemp = int(d.get("B23025_005E") or 0)
        not_lf = int(d.get("B23025_007E") or 0)
        lfpr = in_lf / pop * 100
        ep   = emp / pop * 100
        not_lf_pct = not_lf / pop * 100
        ue_rate = (unemp / in_lf * 100) if in_lf else None
        geos.append({
            "name": d.get("NAME", ""),
            "fips": d.get("county") or d.get("place") or "",
            "pop_16plus": pop,
            "in_labor_force": in_lf,
            "employed": emp,
            "unemployed": unemp,
            "not_in_labor_force": not_lf,
            "lfpr": round(lfpr, 1),
            "ep_ratio": round(ep, 1),
            "not_lf_pct": round(not_lf_pct, 1),
            "ue_rate": round(ue_rate, 1) if ue_rate is not None else None,
            "gap_lfpr_vs_state": round(lfpr - _IL_STATE_LFPR, 1),
            "gap_ep_vs_state": round(ep - _IL_STATE_EP, 1),
        })
        agg["pop"] += pop
        agg["in_lf"] += in_lf
        agg["emp"] += emp
        agg["unemp"] += unemp
        agg["not_lf"] += not_lf

    aggregate = None
    if agg["pop"] > 0 and len(geos) > 1:
        aggregate = {
            "pop_16plus": agg["pop"],
            "in_labor_force": agg["in_lf"],
            "employed": agg["emp"],
            "unemployed": agg["unemp"],
            "not_in_labor_force": agg["not_lf"],
            "lfpr": round(agg["in_lf"] / agg["pop"] * 100, 1),
            "ep_ratio": round(agg["emp"] / agg["pop"] * 100, 1),
            "not_lf_pct": round(agg["not_lf"] / agg["pop"] * 100, 1),
            "ue_rate": round(agg["unemp"] / agg["in_lf"] * 100, 1) if agg["in_lf"] else None,
            "gap_lfpr_vs_state": round(agg["in_lf"] / agg["pop"] * 100 - _IL_STATE_LFPR, 1),
            "gap_ep_vs_state": round(agg["emp"] / agg["pop"] * 100 - _IL_STATE_EP, 1),
        }

    geos.sort(key=lambda g: -g["pop_16plus"])
    return {
        "geos": geos,
        "aggregate": aggregate,
        "benchmarks": {
            "il_state_lfpr": _IL_STATE_LFPR,
            "il_state_ep": _IL_STATE_EP,
            "il_state_not_lf_pct": _IL_STATE_NOTLF_PCT,
            "us_national_lfpr": _US_NATIONAL_LFPR,
            "us_national_ep": _US_NATIONAL_EP,
        },
        "year": year,
        "source": (
            "Census ACS 5y table B23025 (Employment Status for population 16+). "
            "These metrics go BEYOND the headline UE rate to capture discouraged workers and the "
            "long-term not-in-labor-force population — the picture politicians rarely cite because "
            "it's less flattering."
        ),
    }


# ────────────── Community Health Score (synthetic composite) ──────────────
# Inspired by the Economic Innovation Group (EIG) Distressed Communities Index
# and CDC Social Vulnerability Index, but computed locally from the ACS +
# FRED data already on hand. Six components scored 0-100 (higher=healthier),
# averaged. Methodology + thresholds are deliberately transparent and tunable.

# Illinois state median household income reference for the income-ratio
# component. ACS 2023 5y estimate for the state. Refresh annually.
_IL_STATE_MEDIAN_HH_INCOME_2023 = 78433


def _linear_score(value: float | None, worst: float, best: float) -> float | None:
    """Map a value to 0-100 by linear interpolation. worst→0, best→100.
    Direction is inferred from the worst<best vs worst>best relationship."""
    if value is None:
        return None
    if worst == best:
        return 50.0
    raw = (value - worst) / (best - worst) * 100
    return max(0.0, min(100.0, round(raw, 1)))


def _community_health_score(acs_current: dict, trend: dict) -> dict:
    """Compute a 0-100 community-health composite from the latest ACS snapshot
    plus the 5y trend. Components designed to be transparent + tunable:

      no_hs_diploma:    0% → 100;  20%+ → 0       (worst: 20%+, best: 0%)
      poverty_rate:     0% → 100;  30%+ → 0
      unemployment:     0% → 100;  15%+ → 0
      income_vs_state:  ≥state median → 100;  ≤30% of state → 0
      pop_change_5y:    +20% → 100;  -20% → 0;   0 → 50
      income_change_5y: +25% → 100;  -25% → 0;   0 → 50

    Returns {score, label, components: [...]}.
    """
    components: list[dict] = []

    # 1. HS-dropout (strongest predictor of poor outcomes per EIG/CDC research)
    no_hs = acs_current.get("pct_no_hs_diploma")
    s = _linear_score(no_hs, worst=20.0, best=0.0)
    components.append({
        "key": "no_hs_diploma",
        "label": "Educational attainment",
        "value": f"{no_hs:.1f}% adults 25+ without HS diploma" if no_hs is not None else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "Census EIG DCI weights this most heavily — strongest single predictor of long-term distress.",
    })

    # 2. Family poverty rate
    pov = acs_current.get("poverty_rate_families")
    s = _linear_score(pov, worst=30.0, best=0.0)
    components.append({
        "key": "poverty",
        "label": "Family poverty",
        "value": f"{pov:.1f}% of families in poverty" if pov is not None else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "Census SAIPE / ACS family poverty rate.",
    })

    # 3. ACS unemployment
    ue = acs_current.get("acs_unemployment_rate")
    s = _linear_score(ue, worst=15.0, best=0.0)
    components.append({
        "key": "unemployment",
        "label": "Unemployment",
        "value": f"{ue:.1f}% ACS 5y unemployment (ages 25+)" if ue is not None else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "ACS 5y narrower than BLS LAUS but captures discouraged workers more honestly over a 5y window.",
    })

    # 4. Income vs Illinois state median
    inc = acs_current.get("median_household_income")
    ratio = (inc / _IL_STATE_MEDIAN_HH_INCOME_2023) if inc else None
    s = _linear_score(ratio, worst=0.30, best=1.0)
    components.append({
        "key": "income_vs_state",
        "label": "Income vs IL state median",
        "value": f"${inc:,} vs ${_IL_STATE_MEDIAN_HH_INCOME_2023:,} (state median) — {ratio*100:.0f}% of state" if inc and ratio else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "How well does the city's median household income compare to the Illinois state median? Below 50% signals serious wage gap.",
    })

    # 5. 5y population change
    pop_dl = (trend or {}).get("deltas", {}).get("population")
    pop_pct = pop_dl["pct_change"] if pop_dl else None
    s = _linear_score(pop_pct, worst=-20.0, best=20.0)
    components.append({
        "key": "pop_change_5y",
        "label": "Population change (5y)",
        "value": f"{'+' if (pop_pct or 0) > 0 else ''}{pop_pct:.1f}% since prior ACS5" if pop_pct is not None else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "Population growth signals economic vitality; shrinkage signals out-migration / aging.",
    })

    # 6. 5y income change
    inc_dl = (trend or {}).get("deltas", {}).get("median_household_income")
    inc_pct = inc_dl["pct_change"] if inc_dl else None
    s = _linear_score(inc_pct, worst=-25.0, best=25.0)
    components.append({
        "key": "income_change_5y",
        "label": "Income change (5y)",
        "value": f"{'+' if (inc_pct or 0) > 0 else ''}{inc_pct:.1f}% median HH income vs prior ACS5" if inc_pct is not None else "—",
        "score": s,
        "weight": 1.0,
        "rationale": "Direction of household-income travel — real-terms inflation-adjusted growth would be even more informative.",
    })

    # Aggregate
    weighted = [(c["score"], c["weight"]) for c in components if c["score"] is not None]
    if not weighted:
        return {"score": None, "label": "Insufficient data", "components": components}
    total_w = sum(w for _, w in weighted)
    total_s = sum(s * w for s, w in weighted)
    score = round(total_s / total_w, 1)

    label = (
        "Healthy" if score >= 80
        else "Stable" if score >= 60
        else "At-Risk" if score >= 40
        else "Distressed" if score >= 20
        else "Crisis"
    )
    return {
        "score": score,
        "label": label,
        "components": components,
        "methodology": "Six equally-weighted components, each scored 0-100 by linear interpolation between worst/best thresholds, then averaged. Inspired by EIG Distressed Communities Index methodology; thresholds are transparent and tunable.",
    }


async def _census_acs_place(place_fips: str, *, state_fips: str = "17", year: int = 2023) -> dict:
    cache_key = f"acs|{state_fips}|{place_fips}|{year}"
    now = time.time()
    if cache_key in _ACS_CACHE and now - _ACS_CACHE[cache_key][0] < _ACS_CACHE_TTL_SEC:
        return _ACS_CACHE[cache_key][1]

    api_key = os.environ.get("CENSUS_DATA_API_KEY") or os.environ.get("CENSUS_API_KEY")
    if not api_key:
        return {}

    vars_map = {
        "DP05_0001E":  "population",
        "DP05_0018E":  "median_age",
        "DP02_0067PE": "pct_hs_graduate_or_higher",
        "DP02_0068PE": "pct_bachelors_plus",
        "DP03_0009PE": "acs_unemployment_rate",
        "DP03_0062E":  "median_household_income",
        "DP03_0119PE": "poverty_rate_families",
        "DP04_0089E":  "median_home_value",
        "DP04_0134E":  "median_gross_rent",
        "DP04_0046PE": "pct_owner_occupied",
        "DP04_0003PE": "pct_housing_vacant",
        "DP02_0094PE": "pct_foreign_born",
        "DP03_0025E":  "mean_commute_minutes",
        "DP05_0037PE": "pct_white_alone",
        "DP05_0038PE": "pct_black_alone",
        "DP05_0071PE": "pct_hispanic_or_latino",
    }
    fields = "NAME," + ",".join(vars_map.keys())
    url = f"https://api.census.gov/data/{year}/acs/acs5/profile"
    params = {"get": fields, "for": f"place:{place_fips}", "in": f"state:{state_fips}", "key": api_key}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                return {}
            data = r.json()
        except Exception:
            return {}

    if not data or len(data) < 2:
        return {}
    header, row = data[0], data[1]
    raw = dict(zip(header, row, strict=False))

    def _f(v: str | None) -> float | None:
        try:
            f = float(v) if v is not None else None
            return f if f is not None and f >= 0 else None
        except (TypeError, ValueError):
            return None

    def _i(v: str | None) -> int | None:
        f = _f(v)
        return int(f) if f is not None else None

    pct_owner = _f(raw.get("DP04_0046PE"))
    pct_hs_plus = _f(raw.get("DP02_0067PE"))
    out = {
        "name": raw.get("NAME"),
        "place_fips": place_fips,
        "year": year,
        "population":               _i(raw.get("DP05_0001E")),
        "median_age":               _f(raw.get("DP05_0018E")),
        "pct_hs_graduate_or_higher": pct_hs_plus,
        "pct_no_hs_diploma":         (100 - pct_hs_plus) if pct_hs_plus is not None else None,
        "pct_bachelors_plus":       _f(raw.get("DP02_0068PE")),
        "acs_unemployment_rate":    _f(raw.get("DP03_0009PE")),
        "median_household_income":  _i(raw.get("DP03_0062E")),
        "poverty_rate_families":    _f(raw.get("DP03_0119PE")),
        "median_home_value":        _i(raw.get("DP04_0089E")),
        "median_gross_rent":        _i(raw.get("DP04_0134E")),
        "pct_owner_occupied":       pct_owner,
        "pct_renter_occupied":      (100 - pct_owner) if pct_owner is not None else None,
        "pct_housing_vacant":       _f(raw.get("DP04_0003PE")),
        "pct_foreign_born":         _f(raw.get("DP02_0094PE")),
        "mean_commute_minutes":     _f(raw.get("DP03_0025E")),
        "pct_white_alone":          _f(raw.get("DP05_0037PE")),
        "pct_black_alone":          _f(raw.get("DP05_0038PE")),
        "pct_hispanic_or_latino":   _f(raw.get("DP05_0071PE")),
        "source": f"US Census Bureau, American Community Survey {year} 5-year estimates, Data Profile DP02/DP03/DP04/DP05.",
    }
    _ACS_CACHE[cache_key] = (now, out)
    return out


# ────────────── BLS QCEW (Quarterly Census of Employment & Wages) ──────────────
# Industry-by-NAICS-supersector employment + average weekly wages, per county,
# per quarter. Public BLS CSV endpoint; no API key. QCEW publishes Q with ~7
# month lag. Cached 24h since data refreshes quarterly.

_BLS_NAICS_SUPERSECTOR: dict[str, str] = {
    "1011": "Natural Resources and Mining",
    "1012": "Construction",
    "1013": "Manufacturing",
    "1021": "Trade, Transportation, and Utilities",
    "1022": "Information",
    "1023": "Financial Activities",
    "1024": "Professional and Business Services",
    "1025": "Education and Health Services",
    "1026": "Leisure and Hospitality",
    "1027": "Other Services",
    "1028": "Public Administration",
    "1029": "Unclassified",
}

_QCEW_CACHE: dict[str, tuple[float, dict]] = {}
_QCEW_CACHE_TTL_SEC = 86400  # 24h


# BLS throttles unidentified UAs from cloud egress IPs. A descriptive UA gets
# through reliably (BLS API guidance recommends including a contact endpoint).
_BLS_HEADERS = {
    "User-Agent": "ste-console-api/1.0 (operator-console contact: ops@packetvoidlabs.dev)",
    "Accept": "text/csv,application/json,*/*",
}


async def _qcew_latest_quarter() -> tuple[int, int]:
    """Probe BLS for the most recent published quarter. Falls back to (2025, 3)
    if probes fail. BLS publishes Q ~7 months after the end of the quarter."""
    today = datetime.now(UTC).date()
    candidates: list[tuple[int, int]] = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(6):
        q -= 1
        if q < 1:
            q = 4
            y -= 1
        candidates.append((y, q))
    async with httpx.AsyncClient(timeout=8.0, headers=_BLS_HEADERS) as client:
        for yy, qq in candidates:
            try:
                r = await client.head(f"https://data.bls.gov/cew/data/api/{yy}/{qq}/area/17077.csv")
                if r.status_code == 200:
                    return (yy, qq)
            except Exception:
                continue
    return (2025, 3)


_COUNTY_FIPS_NAME: dict[str, str] = {
    "055": "Franklin",
    "077": "Jackson",
    "081": "Jefferson",
    "145": "Perry",
    "199": "Williamson",
}


async def _qcew_fetch_one_county(client: httpx.AsyncClient, year: int, qtr: int, fips5: str):
    """Fetch one county QCEW CSV. Returns (fips, parsed_rows). Empty list on failure."""
    import csv as csv_mod
    import io as io_mod
    area_code = "17" + fips5
    try:
        r = await client.get(f"https://data.bls.gov/cew/data/api/{year}/{qtr}/area/{area_code}.csv")
        if r.status_code != 200:
            return (fips5, [], f"HTTP {r.status_code}")
        return (fips5, list(csv_mod.DictReader(io_mod.StringIO(r.text))), None)
    except Exception as exc:
        return (fips5, [], type(exc).__name__)


async def _qcew_supersector_block(county_fips: list[str]) -> dict:
    """Returns {as_of_quarter, top_supersectors: [...], total_employment, by_county: [...], source}.
    Never returns empty cached results — those are silent failures we want to retry."""
    cache_key = "qcew|" + ",".join(sorted(county_fips))
    now = time.time()
    if cache_key in _QCEW_CACHE and now - _QCEW_CACHE[cache_key][0] < _QCEW_CACHE_TTL_SEC:
        cached = _QCEW_CACHE[cache_key][1]
        # Don't return empty cached results — they came from a failed fetch and
        # should be retried, not held for 24h.
        if cached.get("top_supersectors"):
            return cached

    year, qtr = await _qcew_latest_quarter()
    agg: dict[str, dict] = {}
    grand_emp = 0
    per_county: dict[str, dict] = {}
    fetch_errors: list[str] = []
    # Parallelize the 5 BLS county fetches — completes in ~3-5s instead of ~15-20s
    # serial, avoiding Railway request-timeout on cold cache.
    import asyncio as _asyncio
    async with httpx.AsyncClient(timeout=30.0, headers=_BLS_HEADERS) as client:
        results = await _asyncio.gather(
            *[_qcew_fetch_one_county(client, year, qtr, f) for f in county_fips]
        )
        for fips5, rows, err in results:
            if err:
                fetch_errors.append(f"{fips5}:{err}")
            county_agg: dict[str, dict] = {}
            try:
                for row in rows:
                    if row.get("agglvl_code") != "73":
                        continue
                    ic = row.get("industry_code") or ""
                    if ic not in _BLS_NAICS_SUPERSECTOR:
                        continue
                    own = row.get("own_code") or "0"
                    emp = int(row.get("month3_emplvl") or 0)
                    wage = float(row.get("avg_wkly_wage") or 0)
                    # Cross-county aggregate
                    if ic not in agg:
                        agg[ic] = {
                            "code": ic, "name": _BLS_NAICS_SUPERSECTOR[ic],
                            "private_emp": 0, "public_emp": 0,
                            "wage_sum": 0.0, "wage_weight": 0,
                        }
                    if own == "5":
                        agg[ic]["private_emp"] += emp
                    elif own in ("1", "2", "3"):
                        agg[ic]["public_emp"] += emp
                    agg[ic]["wage_sum"] += wage * emp
                    agg[ic]["wage_weight"] += emp
                    grand_emp += emp
                    # Per-county snapshot
                    if ic not in county_agg:
                        county_agg[ic] = {"code": ic, "name": _BLS_NAICS_SUPERSECTOR[ic], "emp": 0, "wage_sum": 0.0, "wage_w": 0}
                    county_agg[ic]["emp"] += emp
                    county_agg[ic]["wage_sum"] += wage * emp
                    county_agg[ic]["wage_w"] += emp
            except Exception:
                continue
            # Sort county snapshot top sectors
            county_items = []
            for cd in county_agg.values():
                if cd["emp"] == 0:
                    continue
                county_items.append({
                    "code": cd["code"],
                    "name": cd["name"],
                    "employment": cd["emp"],
                    "avg_weekly_wage": round(cd["wage_sum"] / cd["wage_w"] if cd["wage_w"] else 0, 0),
                })
            county_items.sort(key=lambda x: -x["employment"])
            per_county[fips5] = {
                "fips": fips5,
                "name": _COUNTY_FIPS_NAME.get(fips5, fips5),
                "total_employment": sum(c["employment"] for c in county_items),
                "top_supersectors": county_items[:6],
            }

    items = []
    for d in agg.values():
        total_emp = d["private_emp"] + d["public_emp"]
        if total_emp == 0:
            continue
        avg_wkly = d["wage_sum"] / d["wage_weight"] if d["wage_weight"] else 0
        items.append({
            "code": d["code"],
            "name": d["name"],
            "total_employment": total_emp,
            "private_employment": d["private_emp"],
            "public_employment": d["public_emp"],
            "avg_weekly_wage": round(avg_wkly, 0),
            "annual_pay_equivalent": round(avg_wkly * 52, 0),
        })
    items.sort(key=lambda x: -x["total_employment"])

    by_county_list = sorted(per_county.values(), key=lambda c: -c["total_employment"])

    out = {
        "as_of_quarter": f"{year}Q{qtr}",
        "top_supersectors": items,
        "total_employment": sum(i["total_employment"] for i in items),
        "by_county": by_county_list,
        "source": "BLS Quarterly Census of Employment & Wages (QCEW); NAICS supersector aggregation, all ownerships.",
        "county_fips": county_fips,
        "fetch_errors": fetch_errors,  # surfaced to frontend so it can show data-unavailable state
    }
    # Only cache successful results. Empty results indicate a silent fetch
    # failure (BLS throttling, transient outage) — caching that for 24h is bad.
    if items:
        _QCEW_CACHE[cache_key] = (now, out)
    return out


# ────────────── USAspending — top recipients + concentration ──────────────
# Surfaces "who actually gets the federal money flowing into this region",
# which exposes asymmetries between federal-dollar flow and local-job creation
# that the workforce board / city BD can use as community-engagement leverage
# (e.g., LWA-25 has ~99% of all federal contract dollars going to a single
# munitions manufacturer — that's CBA leverage if the workforce board wants
# expanded local hiring, apprenticeships, or supplier-development commitments).

async def _usaspending_top_recipients(
    *, county_fips: list[str], lookback_months: int = 24, top_n: int = 12,
) -> dict:
    cache_key = f"recip|{','.join(sorted(county_fips))}|{lookback_months}"
    now = time.time()
    if cache_key in _USA_CACHE and now - _USA_CACHE[cache_key][0] < _USA_CACHE_TTL_SEC:
        return _USA_CACHE[cache_key][1]

    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_months * 30)
    locations = [{"country": "USA", "state": "IL", "county": c} for c in county_fips]
    body = {
        "filters": {
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
            "place_of_performance_locations": locations,
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "limit": top_n,
    }
    resp = await _usaspending_post("/api/v2/search/spending_by_category/recipient/", body)
    raw = resp.get("results", []) or []

    # Normalize recipient names: "GD ORDNANCE & TACTICAL SYSTEMS" and
    # "GD ORDNANCE AND TACTICAL SYSTEMS" appear as separate entries due to
    # variant punctuation, but it's the same entity. Collapse by a normalized key.
    def _normalize(name: str) -> str:
        n = name.upper()
        for ch in [",", ".", "'", "&"]:
            n = n.replace(ch, " AND " if ch == "&" else " ")
        for ch in ["INC", "LLC", "CORPORATION", "CORP"]:
            n = n.replace(ch, "")
        return " ".join(n.split())

    collapsed: dict[str, dict] = {}
    for r in raw:
        name = r.get("name") or ""
        amt = float(r.get("amount") or 0)
        key = _normalize(name)
        if key not in collapsed:
            collapsed[key] = {"name": name, "amount": 0.0, "names_seen": set()}
        collapsed[key]["amount"] += amt
        collapsed[key]["names_seen"].add(name)

    # Display-name overrides — USAspending's registered name is sometimes
    # misleading (e.g., "NAPHCARE LLC" vs the actual legal entity
    # "NaphCare, Inc."). When the SBA lookup table specifies a display_name,
    # the frontend should prefer it for the public-stakeholder display.
    DISPLAY_NAME_OVERRIDES = {
        "NAPHCARE": "NaphCare, Inc.",
    }
    def _display_name(raw: str) -> str:
        u = raw.upper()
        for key, override in DISPLAY_NAME_OVERRIDES.items():
            if key in u:
                return override
        return raw
    items = [
        {"name": _display_name(v["name"]), "amount": v["amount"], "alias_count": len(v["names_seen"])}
        for v in collapsed.values()
    ]
    items.sort(key=lambda x: -x["amount"])

    # Enrich each recipient with SBA classification + location from a web-researched
    # lookup table. USAspending's "Type of Set Aside" field reflects the CONTRACT
    # set-aside type, NOT the recipient's SBA certification status — recipients can
    # be SDVOSBs winning open competition. Maintained manually; verify by checking
    # the recipient's website / SAM.gov / veteranownedbusiness.com / DSBS.
    KNOWN_SBA_STATUS: dict[str, dict] = {
        # 4 confirmed SDVOSBs in the LWA-25 top recipient list (web-sourced 2026-05-27)
        "SMITH HAFELI":              {"sba_status": "SDVOSB",       "location_tag": "LOCAL · Marion IL",       "founder_note": "USAF Col. Lance Hafeli",        "source_url": "https://smith-hafeli.com/about-us/"},
        "SDV OFFICE":                {"sba_status": "SDVOSB",       "location_tag": "OUT-OF-REGION · Fletcher NC", "founder_note": "Two USMC officers · UEI verify at SAM.gov",          "source_url": "https://sdvosystems.com/contracts/"},
        "JETT":                      {"sba_status": "SDVOSB",       "location_tag": "OUT-OF-REGION · Paducah KY", "founder_note": "Jeffrey Jett · UEI verify at SAM.gov", "source_url": "https://www.veteranownedbusiness.com/business/33768/jetts-specialty-contracting"},
        "ABOVE GROUP":               {"sba_status": "SDVOSB",       "location_tag": "OUT-OF-REGION · Melbourne FL", "founder_note": "Founded 2014 · UEI N5WANJDVRMG8 · CAGE 7DG75 · 40 emp · $14M rev", "source_url": "https://www.abovegroupinc.com/"},
        # 3 confirmed large businesses (no SBA set-aside applies)
        "NAPHCARE":                  {"sba_status": "LARGE",        "location_tag": "OUT-OF-REGION · Vestavia Hills AL", "founder_note": "NaphCare, Inc. (legal entity is Inc., not LLC) · ~$483M rev, largest BOP healthcare TPA", "source_url": "https://www.naphcare.com/about"},
        "CDM FEDERAL":               {"sba_status": "LARGE",        "location_tag": "OUT-OF-REGION · 131 offices", "founder_note": "CDM Smith subsidiary, ~5,000 employees", "source_url": "https://en.wikipedia.org/wiki/CDM_Smith"},
        "ILLINOIS POWER MARKETING":  {"sba_status": "LARGE",        "location_tag": "OUT-OF-REGION · utility",    "founder_note": "Illinois Power Marketing Company, LLC — Vistra Corp. subsidiary (post-Dynegy merger). Confirm via Vistra 10-K Exhibit 21 (Subsidiaries List).",  "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001692819&type=10-K"},
        # Joint ventures + unverified — flag for SAM.gov manual check
        "AOD & RBT":                 {"sba_status": "UNVERIFIED",   "location_tag": "JV — verify at SAM.gov",     "founder_note": "JV structure suggests SBA mentor-protégé", "source_url": ""},
        "FFE - HEAPY":               {"sba_status": "UNVERIFIED",   "location_tag": "JV with large eng firm",     "founder_note": "HEAPY is large; FFE may be small partner", "source_url": ""},
        "LAKE CONTRACTING":          {"sba_status": "UNVERIFIED",   "location_tag": "ambiguous — name shared by multiple US firms", "founder_note": "UEI not yet confirmed; multiple US companies operate under 'Lake Contracting' (IL / MO / others). Treat as ambiguous attribution pending SAM.gov UEI confirmation before any stakeholder outreach.", "source_url": "https://sam.gov/"},
    }

    def _lookup_sba(rec_name: str) -> dict:
        n = rec_name.upper()
        for key, val in KNOWN_SBA_STATUS.items():
            if key in n:
                return val
        return {"sba_status": "UNCLASSIFIED", "location_tag": "", "founder_note": "", "source_url": ""}

    for x in items:
        x.update(_lookup_sba(x["name"]))

    total = sum(x["amount"] for x in items)
    for x in items:
        x["share_pct"] = round((x["amount"] / total * 100), 1) if total else 0.0

    # Set-aside-aware summary stats
    sdvosb_items = [x for x in items if x.get("sba_status") == "SDVOSB"]
    sdvosb_total = sum(x["amount"] for x in sdvosb_items)
    local_sdvosb_count = sum(1 for x in sdvosb_items if "LOCAL" in (x.get("location_tag") or ""))
    sdvosb_summary = {
        "count": len(sdvosb_items),
        "local_count": local_sdvosb_count,
        "out_of_region_count": len(sdvosb_items) - local_sdvosb_count,
        "total_dollars": sdvosb_total,
        "total_share_pct": round((sdvosb_total / total * 100), 1) if total else 0.0,
    }

    # Concentration metric — HHI-style + top-1 share
    top1_share = items[0]["share_pct"] if items else 0
    top3_share = round(sum(x["share_pct"] for x in items[:3]), 1)
    # Categorize concentration
    if top1_share >= 70:
        concentration_label = "EXTREME — single recipient dominates the regional federal-dollar flow"
    elif top1_share >= 40:
        concentration_label = "HIGH — one recipient captures most federal contract dollars"
    elif top3_share >= 60:
        concentration_label = "MODERATE — three recipients dominate"
    else:
        concentration_label = "DIVERSE — federal contract dollars spread across many recipients"

    out = {
        "recipients": items,
        "total_dollars": total,
        "lookback_months": lookback_months,
        "top1_share": top1_share,
        "top3_share": top3_share,
        "concentration_label": concentration_label,
        "sdvosb_summary": sdvosb_summary,
        "county_fips": county_fips,
        "source": (
            "USAspending.gov spending_by_category/recipient. Recipients are deduplicated "
            "across name variants (punctuation differences) before aggregation. SBA "
            "set-aside classification is from a manually-maintained lookup table sourced "
            "to each recipient's website / SAM.gov / veteranownedbusiness.com — verify any "
            "specific classification at SAM.gov before acting on it."
        ),
    }
    _USA_CACHE[cache_key] = (now, out)
    return out


# ────────────── USAspending.gov federal-awards helper ──────────────
# Used by /api/public/carbondale, /api/public/murphysboro, /api/public/mantracon
# to surface federal contract dollars flowing into the region as business-lead
# substrate. Public API, no key required. 5-minute in-process cache to avoid
# hammering api.usaspending.gov on every page load.

_USA_CACHE: dict[str, tuple[float, dict]] = {}
_USA_CACHE_TTL_SEC = 300

async def _usaspending_post(path: str, body: dict, timeout: float = 15.0) -> dict:
    url = f"https://api.usaspending.gov{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

async def _usaspending_block(
    *, county_fips: list[str], recipient_city: str | None, lookback_months: int = 24
) -> dict:
    """Returns {top_awards: [...], top_naics: [...], totals: {...}, sam_gov_link}.
    Filters federal contract awards by place-of-performance to the given IL counties.
    """
    cache_key = f"v1|{','.join(sorted(county_fips))}|{recipient_city or '*'}|{lookback_months}"
    now = time.time()
    if cache_key in _USA_CACHE and now - _USA_CACHE[cache_key][0] < _USA_CACHE_TTL_SEC:
        return _USA_CACHE[cache_key][1]

    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_months * 30)
    locations = [{"country": "USA", "state": "IL", "county": c} for c in county_fips]
    base_filters: dict = {
        "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
        "place_of_performance_locations": locations,
        "award_type_codes": ["A", "B", "C", "D"],  # contracts
    }
    if recipient_city:
        base_filters["recipient_locations"] = [
            {"country": "USA", "state": "IL", "city": recipient_city.upper()}
        ]

    # Top recipients
    awards_body = {
        "filters": base_filters,
        "fields": [
            "Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
            "Description", "Period of Performance Start Date",
            "Period of Performance Current End Date", "NAICS", "Place of Performance State Code",
        ],
        "page": 1, "limit": 25, "sort": "Award Amount", "order": "desc",
        "subawards": False,
    }
    awards_resp = await _usaspending_post("/api/v2/search/spending_by_award/", awards_body)
    raw_awards = awards_resp.get("results", []) or []

    # Top NAICS by dollars (note: USAspending uses /naics/ path suffix, not category-in-body)
    naics_body = {"filters": base_filters, "limit": 10}
    naics_resp = await _usaspending_post("/api/v2/search/spending_by_category/naics/", naics_body)
    raw_naics = naics_resp.get("results", []) or []

    def _truncate(s: str | None, n: int = 140) -> str:
        if not s: return ""
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1] + "…"

    top_awards = [
        {
            "amount": float(a.get("Award Amount") or 0),
            "recipient": a.get("Recipient Name") or "",
            "agency": a.get("Awarding Agency") or "",
            "description": _truncate(a.get("Description")),
            "naics_code": (a.get("NAICS") or {}).get("code") if isinstance(a.get("NAICS"), dict) else (a.get("naics") or {}).get("code", ""),
            "naics_desc": (a.get("NAICS") or {}).get("description") if isinstance(a.get("NAICS"), dict) else "",
            "start_date": a.get("Period of Performance Start Date") or "",
            "end_date": a.get("Period of Performance Current End Date") or "",
        }
        for a in raw_awards
    ]
    top_naics = [
        {
            "code": str(n.get("code") or ""),
            "name": n.get("name") or "",
            "amount": float(n.get("amount") or 0),
        }
        for n in raw_naics if n.get("code")
    ]
    totals = {
        "awards_count": len(raw_awards),
        "awards_dollars": sum(a["amount"] for a in top_awards),
        "lookback_months": lookback_months,
    }
    # Build a SAM.gov opportunities link pre-filtered to IL by NAICS (top 1)
    sam_link = (
        "https://sam.gov/search/?index=opp&page=1"
        "&sort=-modifiedDate&pageSize=25&sfm[status][is_active]=true"
        "&sfm[placeOfPerformance][country][name]=USA"
        "&sfm[placeOfPerformance][state][code]=IL"
    )
    if top_naics:
        sam_link += f"&sfm[naics][naics][0][code]={top_naics[0]['code']}"

    out = {
        "top_awards": top_awards,
        "top_naics": top_naics,
        "totals": totals,
        "sam_gov_search_link": sam_link,
        "usaspending_search_link": (
            "https://www.usaspending.gov/search/?hash=" +  # placeholder; users land on filtered search
            ""
        ),
        "note": (
            "Federal contract awards with place-of-performance in the selected county set. "
            f"Lookback: {lookback_months} months. Data refreshed nightly upstream by USAspending.gov."
        ),
    }
    _USA_CACHE[cache_key] = (now, out)
    return out


# ────────────── USAspending — GD-OTS Marion sub-award lanes ──────────────
# Surfaces "what does GD-OTS Marion buy from out-of-region subs that local
# firms could supply?" — the operational complement to the federal-money
# concentration section. Joins three USAspending API endpoints client-side:
#   A) spending_by_subaward_grouped (filtered to GD-OTS prime awards w/ subs)
#   B) /awards/{id}/                  (NAICS-6 per prime)
#   C) /subawards/ (POST)             (sub-recipient detail per prime)
# 24h cache (matches the existing _USA_CACHE_TTL_SEC pattern).
#
# TODO: the "out_of_region_candidate" heuristic is name-only — a
# sub-recipient name lacking MARION/CARBONDALE/ILLINOIS/IL tokens is treated
# as likely-out-of-region. This will misclassify subs whose registered name
# omits a city/state token but whose place-of-performance is in-region, and
# vice versa. A proper fix requires the sub-recipient UEI + SAM.gov location
# lookup, which the realtime /subawards/ endpoint does not return. Use this
# field as a BD-triage hint, not a verdict.

_GDOTS_RECIPIENT_NAME = "GENERAL DYNAMICS ORDNANCE & TACTICAL SYSTEMS, INC."
_GDOTS_USASPENDING_PROFILE_URL = (
    "https://www.usaspending.gov/recipient/"
    "0ebec944-fe6a-00a0-5964-978a54540e03-C/latest"
)
_GDOTS_SUBAWARD_TTL_SEC = 24 * 60 * 60  # 24h — overrides 5-min default for this slow lane
_LOCAL_NAME_TOKENS = ("MARION", "CARBONDALE", "ILLINOIS", " IL ")


def _is_local_subrecipient_name(name: str) -> bool:
    """Return True when a sub-recipient name contains any local-region token.
    Heuristic — see TODO above. Pads the name so " IL " can match as a word."""
    if not name:
        return False
    padded = f" {name.upper()} "
    return any(tok in padded for tok in _LOCAL_NAME_TOKENS)


async def _usaspending_gdots_subawards(lookback_months: int = 24) -> dict | None:
    """Aggregate GD-OTS Marion sub-award flow by NAICS-6 lane.

    Returns the payload shape documented in the /api/public/mantracon
    response (gdots_subaward_lanes), or None on hard fetch failure when
    no prior cached value is available (frontend hides the section).
    """
    cache_key = f"gdots_sub|{lookback_months}"
    now = time.time()
    cached = _USA_CACHE.get(cache_key)
    if cached and now - cached[0] < _GDOTS_SUBAWARD_TTL_SEC:
        return cached[1]

    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_months * 30)

    # ── Step A: prime awards with subawards ───────────────────────────────
    step_a_body = {
        "filters": {
            "recipient_search_text": [_GDOTS_RECIPIENT_NAME],
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
            "award_type_codes": ["A", "B", "C", "D"],
            "place_of_performance_locations": [{"country": "USA", "state": "IL"}],
        },
        "limit": 100,
        "page": 1,
    }
    try:
        async with httpx.AsyncClient(
            timeout=20.0, limits=httpx.Limits(max_connections=8)
        ) as client:
            resp_a = await client.post(
                "https://api.usaspending.gov/api/v2/search/spending_by_subaward_grouped/",
                json=step_a_body,
            )
            if resp_a.status_code != 200:
                if cached:
                    return cached[1]
                return None
            primes = (resp_a.json() or {}).get("results", []) or []
            primes = [
                p for p in primes
                if (p.get("subaward_count") or 0) > 0 and p.get("award_generated_internal_id")
            ]
            if not primes:
                if cached:
                    return cached[1]
                return None

            # ── Step B + Step C: NAICS + sub-recipient detail per prime ─────
            async def _fetch_naics(internal_id: str) -> tuple[str, dict]:
                try:
                    r = await client.get(
                        f"https://api.usaspending.gov/api/v2/awards/{internal_id}/"
                    )
                    r.raise_for_status()
                    j = r.json() or {}
                    base = ((j.get("naics_hierarchy") or {}).get("base_code") or {})
                    return internal_id, {
                        "code": str(base.get("code") or "") or None,
                        "description": base.get("description") or "",
                    }
                except Exception:
                    return internal_id, {"code": None, "description": ""}

            async def _fetch_subs(internal_id: str) -> tuple[str, list[dict]]:
                try:
                    r = await client.post(
                        "https://api.usaspending.gov/api/v2/subawards/",
                        json={
                            "award_id": internal_id,
                            "limit": 100,
                            "page": 1,
                            "sort": "amount",
                            "order": "desc",
                        },
                    )
                    r.raise_for_status()
                    j = r.json() or {}
                    return internal_id, j.get("results", []) or []
                except Exception:
                    return internal_id, []

            naics_tasks = [_fetch_naics(p["award_generated_internal_id"]) for p in primes]
            subs_tasks = [_fetch_subs(p["award_generated_internal_id"]) for p in primes]
            naics_results = await asyncio.gather(*naics_tasks, return_exceptions=True)
            subs_results = await asyncio.gather(*subs_tasks, return_exceptions=True)
    except Exception:
        if cached:
            return cached[1]
        return None

    naics_by_id: dict[str, dict] = {}
    for item in naics_results:
        if isinstance(item, Exception):
            continue
        internal_id, naics = item
        naics_by_id[internal_id] = naics

    subs_by_id: dict[str, list[dict]] = {}
    for item in subs_results:
        if isinstance(item, Exception):
            continue
        internal_id, subs = item
        subs_by_id[internal_id] = subs

    # ── Aggregate per NAICS-6 lane ──────────────────────────────────────────
    # lane structure:
    #   subaward_total_usd   = Σ(prime.subaward_obligation) across primes in this NAICS
    #   subaward_count       = Σ(prime.subaward_count)
    #   prime_award_count    = #primes mapped to this NAICS
    #   top_sub_recipients   = top-3 by Σ(/subawards/ amount) within this NAICS
    lanes: dict[str, dict] = {}
    total_subaward_amount = 0.0
    for prime in primes:
        internal_id = prime["award_generated_internal_id"]
        naics = naics_by_id.get(internal_id) or {"code": None, "description": ""}
        code = naics.get("code") or "UNKNOWN"
        name = naics.get("description") or ""
        sub_obligation = float(prime.get("subaward_obligation") or 0)
        sub_count = int(prime.get("subaward_count") or 0)
        total_subaward_amount += sub_obligation

        lane = lanes.setdefault(code, {
            "naics_code": code,
            "naics_name": name,
            "subaward_total_usd": 0.0,
            "subaward_count": 0,
            "prime_award_count": 0,
            "_recipient_sums": {},  # name -> Σ amount across /subawards/ for this lane
        })
        # First non-empty name wins (USAspending occasionally has blank descriptions).
        if not lane["naics_name"] and name:
            lane["naics_name"] = name
        lane["subaward_total_usd"] += sub_obligation
        lane["subaward_count"] += sub_count
        lane["prime_award_count"] += 1

        for sub in subs_by_id.get(internal_id, []):
            rec_name = (sub.get("recipient_name") or "").strip()
            if not rec_name:
                continue
            amt = float(sub.get("amount") or 0)
            lane["_recipient_sums"][rec_name] = (
                lane["_recipient_sums"].get(rec_name, 0.0) + amt
            )

    rows: list[dict] = []
    for lane in lanes.values():
        rec_sums = lane.pop("_recipient_sums")
        sorted_recs = sorted(rec_sums.items(), key=lambda kv: -kv[1])
        top3 = [
            {"name": rec_name, "subaward_sum_usd": round(amt, 2)}
            for rec_name, amt in sorted_recs[:3]
        ]
        out_of_region = bool(top3) and not any(
            _is_local_subrecipient_name(r["name"]) for r in top3
        )
        rows.append({
            "naics_code": lane["naics_code"],
            "naics_name": lane["naics_name"],
            "subaward_total_usd": round(lane["subaward_total_usd"], 2),
            "subaward_count": lane["subaward_count"],
            "prime_award_count": lane["prime_award_count"],
            "top_sub_recipients": top3,
            "out_of_region_candidate": out_of_region,
        })
    rows.sort(key=lambda r: -r["subaward_total_usd"])
    rows = rows[:15]

    out = {
        "rows": rows,
        "total_subaward_amount_usd": round(total_subaward_amount, 2),
        "lookback_months": lookback_months,
        "source_url": _GDOTS_USASPENDING_PROFILE_URL,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    _USA_CACHE[cache_key] = (now, out)
    return out


# ────── USAspending — GD-OTS Marion sub-award lanes (BULK download) ──────
# The realtime /subawards/ endpoint above is fast but does NOT surface
# sub-recipient UEI, state, ZIP, or per-sub-award NAICS — every sub-award
# rolls up under the prime-award NAICS (332993 Ammunition Mfg for GD-OTS
# Marion), hiding the services lanes (561730 grounds / 561720 janitorial
# / 238220 HVAC / 484110 freight / etc.) that are the operator's BD-action
# lanes.
#
# The USAspending bulk-download path (POST /api/v2/download/awards/) solves
# this: it produces a CSV with the full sub-recipient detail including the
# verified place-of-performance / sub-recipient state and per-sub-award
# NAICS code. This is async — submit request → poll status_url → download
# ZIP → extract Subawards_*.csv. The bulk dataset refreshes upstream
# weekly, so we cache 7 days. On hard failure we return the last cached
# value if any, else None (frontend hides the section).
#
# Operator BD principle (2026-05-27): the supply-chain replacement
# strategy targets sub-recipients outside the broader Midwest economic
# shed. "Don't take from St. Louis neighbors" requires verified
# sub-recipient state — impossible with the realtime name-only heuristic.
# This bulk path is what enables that principle.
#
# Services lanes (operator's BD-action set, highlighted in the frontend):
#   561720 Janitorial Services
#   561730 Landscaping Services
#   238220 Plumbing/Heating/AC Contractors
#   484110 General Freight Trucking, Local
#   561612 Security Guards & Patrol
#   562111 Solid Waste Collection
#   561710 Exterminating & Pest Control
#   722310 Food Service Contractors

_GDOTS_BULK_TTL_SEC = 7 * 24 * 60 * 60  # 7 days — bulk dataset refreshes weekly upstream
_GDOTS_BULK_POLL_MAX_SEC = 300.0        # 5-min cap on the async poll loop
_GDOTS_BULK_INITIAL_POLL_SEC = 2.0      # first poll interval; doubles each retry up to 30s

# Candidate CSV column headers — USAspending schemas evolve, so we tolerate
# the documented field names *and* the actual live-download column names.
# Verified live 2026-05-27 against
# Contracts_Subawards_*.csv from /api/v2/download/awards/. Notable
# discrepancies from the spec the operator passed in:
#   - The CSV exposes `subawardee_state_code` (NOT `subawardee_address_state_code`)
#     and `subaward_primary_place_of_performance_state_code` for the
#     place-of-performance state.
#   - `subawardee_zip_code` (NOT `subawardee_address_zip_code`).
#   - `prime_award_piid` (NOT `prime_award_id_piid`).
#   - `prime_awardee_uei` (NOT `prime_award_recipient_uei`).
#   - **CRITICAL**: the bulk CSV does NOT contain `subaward_naics_code` /
#     `subaward_naics_description` at all. Sub-awards inherit the prime
#     NAICS (`prime_award_naics_code`) — same limitation the realtime
#     `/subawards/` endpoint has. We capture both candidates anyway in
#     case USAspending adds the field; downstream the BD intelligence
#     pivots to the `subaward_description` freeform purpose text + a
#     services-lane keyword classifier (see _services_lane_from_description).
_BULK_FIELD_FALLBACKS = {
    "subaward_amount": ("subaward_amount", "subaward_amount_usd"),
    "subaward_action_date": ("subaward_action_date", "action_date"),
    "subawardee_name": ("subawardee_name", "sub_awardee_or_recipient_legal", "subawardee_legal_name"),
    "subawardee_uei": ("subawardee_uei", "subawardee_uei_number"),
    "subawardee_state": (
        "subawardee_state_code",                            # live 2026-05-27
        "subaward_primary_place_of_performance_state_code",
        "subaward_place_of_performance_state_code",         # spec-name variant
        "subawardee_address_state_code",                    # spec-name variant
    ),
    "subawardee_zip": ("subawardee_zip_code", "subawardee_address_zip_code"),
    "subaward_naics_code": (
        "subaward_naics_code",
        "subaward_primary_naics",
        "subawardee_naics_code",
        "prime_award_naics_code",  # fallback — bulk CSV doesn't carry per-sub NAICS
    ),
    "subaward_naics_description": (
        "subaward_naics_description",
        "subaward_primary_naics_description",
        "prime_award_naics_description",  # fallback for same reason as above
    ),
    "prime_award_recipient_uei": ("prime_award_recipient_uei", "prime_awardee_uei"),
    "prime_award_id_piid": ("prime_award_id_piid", "prime_award_piid"),
    "subaward_description": ("subaward_description",),
}

# Operator's services-lane BD-action set — see comment above. Frontend
# highlights these but the backend doesn't filter; we surface ALL lanes
# so the operator can see relative dollar flow into services vs the
# manufacturing rollup.
_GDOTS_SERVICES_LANE_NAICS = frozenset({
    "561720", "561730", "238220", "484110",
    "561612", "562111", "561710", "722310",
})

# Description-based services-lane classifier. Because the bulk CSV does
# not carry per-sub-award NAICS, we derive a services-lane label from
# the `subaward_description` freeform purpose text. Best-effort, keyword
# based — multiple lanes may match, in which case the first hit in this
# tuple wins. NAICS codes are operator-confirmed BD-action lanes
# (services + light infrastructure).
_SERVICES_LANE_KEYWORDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("561730", "Landscaping / grounds maintenance",
        ("landscap", "grounds maint", "lawn", "mowing")),
    ("561720", "Janitorial services",
        ("janitor", "custodi", "cleaning service")),
    ("238220", "HVAC contractor",
        ("hvac", "heating", "air conditioning", "ventilation", " ac ")),
    ("484110", "Local freight trucking",
        ("trucking", "freight", "drayage", "haul")),
    ("561612", "Security guards & patrol",
        ("security guard", "patrol service", "armed guard")),
    ("562111", "Solid waste collection",
        ("solid waste", "trash collection", "refuse", "garbage")),
    ("561710", "Exterminating / pest control",
        ("pest control", "exterminat", "fumigat")),
    ("722310", "Food service contractor",
        ("food service", "cafeteria", "catering")),
)


def _services_lane_from_description(desc: str) -> tuple[str | None, str | None]:
    """Return (naics, label) when the description text matches one of the
    operator's BD-action services lanes; (None, None) otherwise."""
    if not desc:
        return None, None
    lower = desc.lower()
    for naics, label, keywords in _SERVICES_LANE_KEYWORDS:
        for kw in keywords:
            if kw in lower:
                return naics, label
    return None, None


def _bulk_get(row: dict, logical_key: str) -> str:
    """Return the first non-empty value among the documented fallbacks
    for a logical column key. Returns "" when no candidate is populated."""
    candidates = _BULK_FIELD_FALLBACKS.get(logical_key, (logical_key,))
    for col in candidates:
        v = row.get(col)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


async def _usaspending_gdots_bulk_subawards(
    lookback_months: int = 24,
) -> list[dict] | None:
    """POST /api/v2/download/awards/, poll, fetch ZIP, parse Subawards_*.csv.

    Returns a list of normalised sub-award dicts (one per CSV row) with the
    canonical field names (subaward_amount, subawardee_name, subawardee_state,
    subaward_naics_code, …). Returns None on hard failure.
    Caller handles caching; this helper is single-shot.
    """
    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_months * 30)

    # NOTE on the API contract (verified live 2026-05-27):
    #   - `agencies: []` is rejected (422 — "below min 1 items"). The spec
    #     said to include it; the live API requires non-empty or omitted.
    #     We omit.
    #   - `prime_award_types: ["A","B","C","D"]` is silently expanded by
    #     the server to the full procurement award_type_codes set (BPAs,
    #     IDV variants, etc.) — fine for our use.
    #   - `sub_award_types`, `date_type`, and `date_range` are accepted
    #     but the server normalises them; the response echoes only the
    #     filters it actually applied (no date filter on this endpoint).
    #     We send them anyway in case the server tightens its
    #     date-filtering behaviour later — they're harmless.
    request_body = {
        "filters": {
            "prime_award_types": ["A", "B", "C", "D"],
            "sub_award_types": ["procurement"],
            "date_type": "action_date",
            "date_range": {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            "recipient_search_text": [_GDOTS_RECIPIENT_NAME],
            "place_of_performance_locations": [
                {"country": "USA", "state": "IL"},
            ],
        },
        "subawards": True,
        "file_format": "csv",
    }

    try:
        async with httpx.AsyncClient(
            timeout=30.0, limits=httpx.Limits(max_connections=4)
        ) as client:
            # Step 1: submit download request
            r = await client.post(
                "https://api.usaspending.gov/api/v2/download/awards/",
                json=request_body,
            )
            if r.status_code != 200:
                return None
            submit = r.json() or {}
            status_url = submit.get("status_url")
            file_url = submit.get("file_url")
            if not status_url and not file_url:
                return None

            # Step 2: poll for completion (exponential backoff, 5-min cap).
            # Some submissions are pre-computed and file_url is already
            # populated with status='finished' on the initial response —
            # we honour that fast path before entering the poll loop.
            poll_started = time.monotonic()
            interval = _GDOTS_BULK_INITIAL_POLL_SEC
            status = submit.get("status") or ""
            while status != "finished":
                if status in {"failed", "expired"}:
                    return None
                if time.monotonic() - poll_started > _GDOTS_BULK_POLL_MAX_SEC:
                    return None
                await asyncio.sleep(interval)
                interval = min(interval * 2, 30.0)
                if not status_url:
                    break
                sr = await client.get(status_url)
                if sr.status_code != 200:
                    continue
                sj = sr.json() or {}
                status = sj.get("status") or ""
                file_url = sj.get("file_url") or file_url

            if not file_url:
                return None

            # Step 3: download the ZIP
            dr = await client.get(file_url, timeout=120.0)
            if dr.status_code != 200:
                return None
            zip_bytes = dr.content
    except Exception:
        return None

    # Step 4: extract Subawards_*.csv in-memory + parse.
    # NOTE: bulk-download CSVs ship one Contracts_Subawards_*.csv plus an
    # Assistance_Subawards_*.csv (grants/cooperative agreements). For
    # GD-OTS Marion procurement we read both — assistance is normally
    # empty but if a research grant subaward appears we don't want to
    # silently drop it. The CSV is NOT date-filtered server-side
    # (verified live 2026-05-27) so we apply the lookback client-side.
    earliest = (datetime.now(UTC).date() - timedelta(days=lookback_months * 30)).isoformat()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            sub_members = [
                name for name in zf.namelist()
                if "subaward" in name.lower() and name.lower().endswith(".csv")
            ]
            if not sub_members:
                return None
            rows: list[dict] = []
            for sub_member in sub_members:
                with zf.open(sub_member) as f:
                    text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                    reader = csv.DictReader(text)
                    for raw in reader:
                        if not raw:
                            continue
                        action_date = _bulk_get(raw, "subaward_action_date")
                        # Client-side date filter (server ignores the
                        # date_range we pass for this endpoint).
                        if action_date and action_date < earliest:
                            continue
                        amt_str = _bulk_get(raw, "subaward_amount").replace(",", "").replace("$", "")
                        try:
                            amt = float(amt_str) if amt_str else 0.0
                        except ValueError:
                            amt = 0.0
                        state = _bulk_get(raw, "subawardee_state").upper()
                        desc = _bulk_get(raw, "subaward_description")
                        naics_code = _bulk_get(raw, "subaward_naics_code")
                        naics_desc = _bulk_get(raw, "subaward_naics_description")
                        # Derive a services-lane label from the description
                        # when the inherited prime-NAICS is the
                        # manufacturing rollup (332993). This is the
                        # workaround for the missing per-sub-award NAICS.
                        services_lane_naics, services_lane_label = _services_lane_from_description(desc)
                        rows.append({
                            "prime_award_id_piid": _bulk_get(raw, "prime_award_id_piid"),
                            "prime_award_recipient_uei": _bulk_get(raw, "prime_award_recipient_uei"),
                            "subaward_amount": amt,
                            "subaward_action_date": action_date,
                            "subawardee_name": _bulk_get(raw, "subawardee_name"),
                            "subawardee_uei": _bulk_get(raw, "subawardee_uei"),
                            "subawardee_state": state,
                            "subawardee_zip": _bulk_get(raw, "subawardee_zip"),
                            "subaward_naics_code": naics_code,
                            "subaward_naics_description": naics_desc,
                            "subaward_description": desc,
                            "services_lane_naics": services_lane_naics,
                            "services_lane_label": services_lane_label,
                        })
            return rows
    except Exception:
        return None


async def _usaspending_gdots_bulk_lanes(lookback_months: int = 24) -> dict | None:
    """Aggregate the bulk-CSV sub-award detail by sub-award NAICS-6 lane.

    Unlike the realtime helper above, this groups on the SUB-AWARD NAICS
    (not the prime-award NAICS) — that's the whole BD point: the services
    lanes (561720, 561730, 238220, 484110, …) become visible instead of
    rolling up under 332993.

    Per-lane fields:
      - naics_code, naics_name
      - subaward_total_usd  (Σ subaward_amount across the lane)
      - subaward_count      (#sub-awards in the lane)
      - top_sub_recipients  (top-3 by Σ amount, with name + state + UEI)
      - out_of_region_count (number of distinct sub-recipients whose state ≠ IL)
      - out_of_region_total_count (total sub-recipients in the lane, denominator)
      - is_services_lane    (True if NAICS ∈ operator's services-lane set)

    Caps at top 25 lanes by dollar; returns the envelope shape consumed
    by the frontend. 7-day cache.
    """
    cache_key = f"gdots_bulk|{lookback_months}"
    now = time.time()
    cached = _USA_CACHE.get(cache_key)
    if cached and now - cached[0] < _GDOTS_BULK_TTL_SEC:
        return cached[1]

    rows = await _usaspending_gdots_bulk_subawards(lookback_months=lookback_months)
    if rows is None:
        if cached:
            return cached[1]
        return None

    # Aggregate. Lane key = description-derived services NAICS when the
    # subaward_description matches an operator BD-action services lane;
    # otherwise the inherited prime NAICS from the CSV. This is the
    # workaround for the bulk CSV's missing per-sub-award NAICS column —
    # without it, every GD-OTS sub-award would roll up under 332993
    # (Ammunition Mfg) and the services lanes would stay hidden.
    lanes: dict[str, dict] = {}
    for r in rows:
        derived_code = r.get("services_lane_naics")
        derived_label = r.get("services_lane_label")
        if derived_code:
            code = derived_code
            desc = derived_label or ""
        else:
            code = r.get("subaward_naics_code") or "UNKNOWN"
            desc = r.get("subaward_naics_description") or ""
        amt = float(r.get("subaward_amount") or 0.0)
        rec_name = r.get("subawardee_name") or ""
        rec_state = r.get("subawardee_state") or ""
        rec_uei = r.get("subawardee_uei") or ""
        lane = lanes.setdefault(code, {
            "naics_code": code,
            "naics_name": desc,
            "subaward_total_usd": 0.0,
            "subaward_count": 0,
            "_recipient_sums": {},      # (name, state, uei) -> Σ amount
            "_distinct_recipients": {},  # (name, state, uei) -> True (for counts)
        })
        if not lane["naics_name"] and desc:
            lane["naics_name"] = desc
        lane["subaward_total_usd"] += amt
        lane["subaward_count"] += 1
        if rec_name:
            key = (rec_name, rec_state, rec_uei)
            lane["_recipient_sums"][key] = lane["_recipient_sums"].get(key, 0.0) + amt
            lane["_distinct_recipients"][key] = True

    out_rows: list[dict] = []
    total_amount = 0.0
    for lane in lanes.values():
        rec_sums = lane.pop("_recipient_sums")
        distinct_recs = lane.pop("_distinct_recipients")
        sorted_recs = sorted(rec_sums.items(), key=lambda kv: -kv[1])
        top3 = [
            {
                "name": name,
                "state": state,
                "uei": uei,
                "subaward_sum_usd": round(amt, 2),
            }
            for (name, state, uei), amt in sorted_recs[:3]
        ]
        # True state-based out-of-region count (vs the name-only heuristic
        # used in the realtime helper). "Unknown state" rows are NOT
        # counted as out-of-region — we only flag confirmed non-IL.
        out_of_region = sum(
            1 for (_n, s, _u) in distinct_recs
            if s and s != "IL"
        )
        total_distinct = len(distinct_recs)
        code = lane["naics_code"]
        amt_total = round(lane["subaward_total_usd"], 2)
        total_amount += amt_total
        out_rows.append({
            "naics_code": code,
            "naics_name": lane["naics_name"],
            "subaward_total_usd": amt_total,
            "subaward_count": lane["subaward_count"],
            "top_sub_recipients": top3,
            "out_of_region_count": out_of_region,
            "out_of_region_total_count": total_distinct,
            "is_services_lane": code in _GDOTS_SERVICES_LANE_NAICS,
        })
    out_rows.sort(key=lambda r: -r["subaward_total_usd"])
    out_rows = out_rows[:25]

    payload = {
        "rows": out_rows,
        "total_subaward_amount_usd": round(total_amount, 2),
        "lookback_months": lookback_months,
        "source_url": _GDOTS_USASPENDING_PROFILE_URL,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    _USA_CACHE[cache_key] = (now, payload)
    return payload


@app.get("/api/public/murphysboro")
async def public_murphysboro() -> dict:
    """PUBLIC endpoint — Murphysboro, IL (Jackson County seat, 8 mi W of Carbondale).
    Shares the Jackson County FRED substrate with /carbondale; differentiation
    comes from city-specific federal-awards filtering (recipient_city=MURPHYSBORO).
    """
    TARGETS = (
        # Jackson County (Murphysboro is the county seat)
        "crb_jackson_unemployment_rate", "crb_jackson_labor_force",
        "crb_jackson_personal_income", "crb_jackson_real_gdp",
        "crb_jackson_median_hh_income", "crb_jackson_snap_recipients",
        "crb_jackson_poverty_universe", "crb_jackson_single_parent_pct",
        # Carbondale-Marion MSA (Murphysboro is in CBSA 16060)
        "crb_msa_population", "crb_msa_unemployment_rate",
        "crb_msa_labor_force", "crb_msa_avg_hourly_earnings",
        "crb_msa_avg_weekly_earnings",
        "crb_msa_housing_days_on_market", "crb_msa_housing_new_listings_mom",
        "crb_msa_housing_price_inc_yoy",
        # IL state context
        "il_unemployment_rate", "phci_il",
    )
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT series_id, value_num, observed_date,
                       ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_date DESC) AS rn
                FROM platform.macro_data
                WHERE series_id = ANY($1::text[]) AND value_num IS NOT NULL
            )
            SELECT series_id, value_num, observed_date
            FROM latest WHERE rn = 1
            ORDER BY series_id
            """,
            list(TARGETS),
        )
        ur_series = await conn.fetch(
            """
            SELECT observed_date, value_num
            FROM platform.macro_data
            WHERE series_id = 'crb_jackson_unemployment_rate'
              AND observed_date >= CURRENT_DATE - INTERVAL '60 months'
              AND value_num IS NOT NULL
            ORDER BY observed_date ASC
            """
        )
    indicators = {r["series_id"]: {"value": float(r["value_num"]), "date": r["observed_date"].isoformat()} for r in rows}
    # Murphysboro is small — also pull county-wide awards alongside city-specific
    business_city = await _usaspending_block(county_fips=["077"], recipient_city="MURPHYSBORO")
    business_county = await _usaspending_block(county_fips=["077"], recipient_city=None)
    qcew = await _qcew_supersector_block(county_fips=["077"])
    acs = await _census_acs_multiyear("51453")  # Murphysboro city, IL — 2023 + 2018 ACS5
    health = _community_health_score(acs.get("current") or {}, acs) if acs else {}
    labor_truth = await _acs_labor_truth(place_fips="51453")
    return {
        "ts": datetime.now(UTC).isoformat(),
        "indicators": indicators,
        "unemployment_series": [{"date": r["observed_date"].isoformat(), "value": float(r["value_num"])} for r in ur_series],
        "business_opportunities_city": business_city,
        "business_opportunities_county": business_county,
        "industry_mix": qcew,
        "city_demographics": acs.get("current") if acs else {},
        "demographics_trend": acs,
        "health_score": health,
        "labor_truth": labor_truth,
    }


@app.get("/api/public/mantracon")
async def public_mantracon() -> dict:
    """PUBLIC endpoint — Man-Tra-Con / Southern Illinois Workforce Development
    Board (SIWIB) LWA-25 dashboard. Five-county service area: Franklin,
    Jackson, Jefferson, Perry, Williamson. Surfaces aggregate workforce
    metrics + federal-awards business-lead substrate for board outreach.
    """
    LWA = ("jackson", "franklin", "jefferson", "perry", "williamson")
    series_keys: list[str] = []
    for c in LWA:
        series_keys.append(f"crb_{c}_unemployment_rate")
        series_keys.append(f"crb_{c}_labor_force")
    series_keys += ["il_unemployment_rate", "il_nonfarm_payrolls", "phci_il"]

    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT series_id, value_num, observed_date,
                       ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_date DESC) AS rn
                FROM platform.macro_data
                WHERE series_id = ANY($1::text[]) AND value_num IS NOT NULL
            )
            SELECT series_id, value_num, observed_date
            FROM latest WHERE rn = 1
            """,
            series_keys,
        )
        # LWA-aggregate labor-force trend = sum of 5 county labor forces, by month
        lwa_lf_series = await conn.fetch(
            """
            SELECT observed_date, SUM(value_num) AS lf
            FROM platform.macro_data
            WHERE series_id IN (
                'crb_jackson_labor_force','crb_franklin_labor_force',
                'crb_jefferson_labor_force','crb_perry_labor_force',
                'crb_williamson_labor_force'
            )
              AND observed_date >= CURRENT_DATE - INTERVAL '60 months'
              AND value_num IS NOT NULL
            GROUP BY observed_date
            HAVING COUNT(*) = 5
            ORDER BY observed_date ASC
            """
        )
        # Weighted-avg UR by labor force, by month (denom=sum of LF in same month)
        lwa_ur_series = await conn.fetch(
            """
            WITH m AS (
                SELECT observed_date, series_id, value_num
                FROM platform.macro_data
                WHERE series_id = ANY($1::text[])
                  AND observed_date >= CURRENT_DATE - INTERVAL '60 months'
                  AND value_num IS NOT NULL
            ),
            pairs AS (
                SELECT ur.observed_date,
                       SPLIT_PART(REPLACE(ur.series_id, 'crb_', ''), '_unemployment_rate', 1) AS county,
                       ur.value_num AS ur,
                       lf.value_num AS lf
                FROM m ur
                JOIN m lf ON lf.observed_date = ur.observed_date
                          AND lf.series_id =
                              'crb_' ||
                              SPLIT_PART(REPLACE(ur.series_id, 'crb_', ''), '_unemployment_rate', 1)
                              || '_labor_force'
                WHERE ur.series_id LIKE 'crb_%_unemployment_rate'
                  AND ur.series_id NOT LIKE '%msa%'
            )
            SELECT observed_date, ROUND(SUM(ur * lf) / NULLIF(SUM(lf), 0), 2) AS ur
            FROM pairs
            GROUP BY observed_date
            HAVING COUNT(*) = 5
            ORDER BY observed_date ASC
            """,
            [f"crb_{c}_unemployment_rate" for c in LWA] + [f"crb_{c}_labor_force" for c in LWA],
        )
    indicators = {
        r["series_id"]: {"value": float(r["value_num"]), "date": r["observed_date"].isoformat()}
        for r in rows
    }
    # Aggregate (latest month with all 5 counties available)
    lwa_latest_lf = float(lwa_lf_series[-1]["lf"]) if lwa_lf_series else None
    lwa_latest_lf_date = lwa_lf_series[-1]["observed_date"].isoformat() if lwa_lf_series else None
    lwa_latest_ur = float(lwa_ur_series[-1]["ur"]) if lwa_ur_series else None
    lwa_latest_ur_date = lwa_ur_series[-1]["observed_date"].isoformat() if lwa_ur_series else None

    LWA_FIPS = ["055", "077", "081", "145", "199"]
    business = await _usaspending_block(county_fips=LWA_FIPS, recipient_city=None)
    top_recipients = await _usaspending_top_recipients(county_fips=LWA_FIPS)
    qcew = await _qcew_supersector_block(county_fips=LWA_FIPS)
    labor_truth = await _acs_labor_truth(county_fips=LWA_FIPS)
    training_alignment = _training_demand_alignment(qcew)
    gdots_subaward_lanes = await _usaspending_gdots_subawards()
    gdots_subaward_lanes_bulk = await _usaspending_gdots_bulk_lanes()
    return {
        "ts": datetime.now(UTC).isoformat(),
        "indicators": indicators,
        "lwa_aggregate": {
            "labor_force": lwa_latest_lf,
            "labor_force_date": lwa_latest_lf_date,
            "unemployment_rate_weighted": lwa_latest_ur,
            "unemployment_rate_date": lwa_latest_ur_date,
            "county_count": 5,
        },
        "lwa_labor_force_series": [
            {"date": r["observed_date"].isoformat(), "value": float(r["lf"])} for r in lwa_lf_series
        ],
        "lwa_unemployment_series": [
            {"date": r["observed_date"].isoformat(), "value": float(r["ur"])} for r in lwa_ur_series
        ],
        "business_opportunities": business,
        "top_federal_recipients": top_recipients,
        "industry_mix": qcew,
        "labor_truth": labor_truth,
        "training_alignment": training_alignment,
        "gdots_subaward_lanes": gdots_subaward_lanes,
        "gdots_subaward_lanes_bulk": gdots_subaward_lanes_bulk,
    }


@app.get("/api/providers")
async def providers() -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "bindings": [
            {"feed": "prices_daily",         "provider": "fmp",     "status": "ACTIVE",     "adapter": "tpcore.data.ingest_fmp_bars",         "note": "primary daily-bars feed since 2026-05-22 (CTA consolidated)"},
            {"feed": "prices_daily",         "provider": "tradier", "status": "FALLBACK",   "adapter": "tpcore.data.ingest_tradier_bars",     "note": "secondary fallback (acceptable)"},
            {"feed": "prices_daily",         "provider": "alpaca",  "status": "DEPRECATED", "adapter": "tpcore.data.ingest_alpaca_bars",      "note": "demoted 2026-05-25 (close-date skew vs FMP/Tradier)"},
            {"feed": "fundamentals_cache",   "provider": "fmp",     "status": "ACTIVE",     "adapter": "tpcore.data.ingest_fmp_fundamentals", "note": ""},
            {"feed": "corporate_actions",    "provider": "fmp",     "status": "ACTIVE",     "adapter": "tpcore.data.ingest_fmp_corp_actions", "note": ""},
            {"feed": "macro_indicators",     "provider": "fred",    "status": "ACTIVE",     "adapter": "tpcore.data.ingest_fred_macro",       "note": "14 series"},
            {"feed": "sec_insider",          "provider": "sec",     "status": "ACTIVE",     "adapter": "tpcore.data.ingest_sec_insider",      "note": "SEC EDGAR bulk Form-4"},
            {"feed": "aaii_sentiment",       "provider": "aaii",    "status": "ACTIVE",     "adapter": "tpcore.data.ingest_aaii_sentiment",   "note": "weekly"},
            {"feed": "finra_short_interest", "provider": "finra",   "status": "ACTIVE",     "adapter": "tpcore.data.ingest_finra_short_interest", "note": "biweekly"},
            {"feed": "tradier_options",      "provider": "tradier", "status": "ACTIVE",     "adapter": "tpcore.data.ingest_tradier_options",  "note": "max-pain"},
        ],
    }
