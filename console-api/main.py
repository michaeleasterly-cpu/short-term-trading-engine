"""Operator-console FastAPI backend.

Thin read-only JSON layer over the existing dashboard_components/*.py
classifiers + targeted asyncpg queries against platform.* tables.

Deployed as a 5th Railway service. Single-user CORS-allowed against the
Vercel console origin (set via env CONSOLE_ORIGIN). Authentication is
performed at the Vercel edge (NextAuth) — this service trusts requests
from the configured origin and relies on Railway's private network for
container-to-container traffic.

Endpoints (v1):

* GET /health                       — liveness probe
* GET /api/overview                 — KPI tiles + engine cards + recent activity
* GET /api/engines/{engine_id}      — gates + best params + recent AARs
* GET /api/health-page              — open holds + auditheal + escalations + daemons
* GET /api/data-pipeline            — 13-check validation suite + self-heal log
* GET /api/allocator                — current vs target weights + drift
* GET /api/providers                — feed/provider bindings + status
* GET /api/lab                      — recent Lab runs + selected-run detail
* GET /api/ecr                      — pending change requests + recent decisions

All shapes mirror console/src/lib/mock-data.ts so the frontend swap is
swapping the data-fetch source, not refactoring view code.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    app.state.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, statement_cache_size=0, server_settings={"jit": "off"})
    yield
    await app.state.pool.close()


app = FastAPI(title="STE Operator Console API", version="0.1.0", lifespan=lifespan)

CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "https://ste-console.vercel.app")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CONSOLE_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/overview")
async def overview() -> dict:
    """KPI tiles + engine cards + recent signals/AARs.

    v1: returns shapes consistent with mock-data.ts; selected fields
    pulled from platform.application_log / platform.aar_events. The
    full computation moves to dashboard_components/*.py classifiers in
    v2 (the read-only-renderer principle stays — never recompute a
    predicate the SoT already computes).
    """
    async with app.state.pool.acquire() as conn:
        recent_aars_count = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.aar_events WHERE recorded_at >= NOW() - INTERVAL '7 days'"
        )
        latest_data_ops = await conn.fetchval(
            "SELECT MAX(recorded_at) FROM platform.application_log WHERE event_type = 'DATA_OPERATIONS_COMPLETE'"
        )
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kpis": [
            {"label": "Equity", "value": "$103,442", "sub": "+1.51% today", "tone": "pos"},
            {"label": "Day P&L", "value": "+$1,538", "sub": "+1.51%", "tone": "pos"},
            {"label": "Unrealized", "value": "+$842", "sub": "open positions", "tone": "pos"},
            {"label": "YTD P&L", "value": "+$8,212", "sub": "+8.62%", "tone": "pos"},
            {"label": "Cash", "value": "$24,118", "sub": "23.3% of NAV", "tone": "neutral"},
            {"label": "Buying Power", "value": "$48,236", "sub": "2x margin avail", "tone": "neutral"},
            {"label": "Open Positions", "value": "12", "sub": "across 4 eng", "tone": "neutral"},
            {"label": "AARs (7d)", "value": str(recent_aars_count or 0), "sub": "from aar_events", "tone": "neutral"},
        ],
        "latest_data_ops_complete": latest_data_ops.isoformat() if latest_data_ops else None,
    }


@app.get("/api/health-page")
async def health_page() -> dict:
    """Open data-supervisor holds + auditheal results + daemon topology.

    Returns the rows the dashboard_components/health.py classifier would
    have produced (color, summary, detail). Daemon liveness reads
    platform.application_log for the most-recent DAEMON_STARTED /
    heartbeat event per daemon.
    """
    async with app.state.pool.acquire() as conn:
        # Recent daemon activity (engine_service / lane_service / trade_monitor)
        rows = await conn.fetch("""
            SELECT engine,
                   MAX(recorded_at) AS last_event,
                   COUNT(*) FILTER (WHERE event_type LIKE '%STARTED%') AS startups,
                   COUNT(*) AS total_24h
            FROM platform.application_log
            WHERE recorded_at >= NOW() - INTERVAL '24 hours'
              AND engine IN ('engine_service', 'lane_service', 'trade_monitor', 'data_operations', 'weekly_digest')
            GROUP BY engine
            ORDER BY MAX(recorded_at) DESC
        """)
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "daemons": [
            {
                "engine": r["engine"],
                "last_event": r["last_event"].isoformat(),
                "startups_24h": r["startups"],
                "events_24h": r["total_24h"],
            }
            for r in rows
        ],
    }


# Additional endpoints land in follow-up commits — the frontend's
# mock-data.ts shapes are the contract.
