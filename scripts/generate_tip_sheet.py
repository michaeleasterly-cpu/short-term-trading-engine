"""Tip Sheet — private operator research tool (Phase 1).

Terminal-only research report per engine. Renders:

* Layman-readable engine description
* Credibility score + rubric breakdown (from platform.data_quality_log)
* Recent SIGNAL events (from platform.application_log)
* Recent completed trades / AARs (from platform.aar_events)
* Mandatory non-removable disclaimer

Gates:

* Credibility ≥ 60 enforced by default.
* ``--force`` lifts the credibility gate for private operator review of
  unproven engines. The disclaimer is **not** lifted.
* No ``--publish`` flag in Phase 1. Output is terminal-only.

Full design rationale: ``docs/superpowers/specs/2026-05-13-tip-sheet-plan.md``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.backtest.credibility import (
    CREDIBILITY_SOURCE_PREFIX,
    MIN_LIVE_SCORE,
    CredibilityScore,
)
from tpcore.backtest.statistical_validation import render_rubric
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)


DISCLAIMER = """
─────────────────────────────────────────────────────────────────────────────
DISCLAIMER — research output only

This is automated output from a research platform for **private operator
review only**. It is NOT investment advice, NOT a recommendation to buy
or sell any security, and NOT a solicitation. Past simulated or real
performance does not predict future results. The underlying strategies
are unproven; the credibility gate exists precisely because not every
strategy has earned the right to be acted on. Do not act on this output.
Do not share this output.
─────────────────────────────────────────────────────────────────────────────"""


ENGINE_DESCRIPTIONS: dict[str, str] = {
    "sigma": (
        "Sigma looks for stocks stuck in a sideways channel — bouncing between "
        "a price floor and ceiling without a clear trend. When the stock touches "
        "the bottom and shows signs of turning back up, Sigma enters with a "
        "tight stop-loss. Half off at mid-channel, the rest at the ceiling."
    ),
    "reversion": (
        "Reversion hunts for stocks that have fallen too far, too fast, and are "
        "statistically likely to snap back. Waits for fundamentals to confirm "
        "the company is still healthy (not a falling knife), then buys the panic "
        "and waits for the price to return to its average."
    ),
    "vector": (
        "Vector rides stocks moving with strong directional force, backed by a "
        "real reason — an earnings beat, a new contract, an improving business. "
        "Only enters when the stock is fundamentally cheap, a catalyst is "
        "present, and the technicals confirm the trend is accelerating."
    ),
    "momentum": (
        "Momentum is a long-only cross-sectional strategy: rank a universe of "
        "liquid US equities by trailing 12-month return (skipping the most recent "
        "month), buy the top decile equal-weighted, hold to the next monthly "
        "rebalance. Built on the academic momentum premium documented since 1993."
    ),
    "s2": (
        "S2 detects stocks that are heavily shorted and ripe for a squeeze. When "
        "the crowd starts piling in — social chatter spikes, borrow rates surge — "
        "S2 alerts that the fuse is lit. A rare-event hunter; it might fire only "
        "a handful of times a year, but when it does, the move can be explosive."
    ),
    "catalyst": (
        "Catalyst trades the aftermath of corporate events: earnings surprises, "
        "big contract wins, regulatory approvals. Waits for the news to break, "
        "lets the market digest it, then enters after the dust settles to capture "
        "the drift as the rest of the market catches up."
    ),
    "sentinel": (
        "Sentinel is the platform's insurance policy. Monitors recession "
        "indicators — unemployment claims, manufacturing data, the yield curve. "
        "When the warning signs flash red, it shifts a portion of capital into "
        "defensive ETFs (inverse equity, bonds, gold) to protect the portfolio "
        "until the storm passes."
    ),
}


# ────────────────────────────────────────────────────────────────────────────
# Queries — minimal, focused readers for the three data sources
# ────────────────────────────────────────────────────────────────────────────


async def fetch_credibility(pool, engine: str) -> CredibilityScore | None:
    """Read the latest :class:`CredibilityScore` for ``engine`` from
    ``platform.data_quality_log``.

    The full rubric is JSON-serialised into the ``notes`` column by
    :func:`tpcore.backtest.statistical_validation.write_credibility_score`,
    so we reconstruct it via ``model_validate_json``. Returns ``None`` when
    the engine has no row on record (e.g. never ran the rubric)."""
    sql = """
        SELECT confidence, notes, timestamp
        FROM platform.data_quality_log
        WHERE source = $1
        ORDER BY timestamp DESC
        LIMIT 1
    """
    source = f"{CREDIBILITY_SOURCE_PREFIX}.{engine}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, source)
    if row is None or row["notes"] is None:
        return None
    try:
        return CredibilityScore.model_validate_json(row["notes"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("tip_sheet.credibility.parse_failed", engine=engine, error=str(exc)[:200])
        return None


async def fetch_recent_signals(
    pool, engine: str, since: datetime,
) -> list[dict[str, Any]]:
    """Pull ``SIGNAL`` events for ``engine`` from ``platform.application_log``
    since ``since`` (UTC). Returns at most 100 rows, newest first."""
    sql = """
        SELECT recorded_at, message, data
        FROM platform.application_log
        WHERE engine = $1 AND event_type = 'SIGNAL' AND recorded_at >= $2
        ORDER BY recorded_at DESC
        LIMIT 100
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, engine, since)
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # `data` is jsonb; asyncpg returns it as a str unless we register
        # a codec. Parse it inline.
        if isinstance(d.get("data"), str):
            try:
                d["data"] = json.loads(d["data"])
            except Exception:  # noqa: BLE001
                pass
        out.append(d)
    return out


async def fetch_recent_trades(
    pool, engine: str, since: datetime,
) -> list[AfterActionReport]:
    """Pull completed trades for ``engine`` from ``platform.aar_events`` since
    ``since`` (UTC). Reconstructs each :class:`AfterActionReport` from the
    ``aar_data`` jsonb column. Returns at most 100 rows, newest first."""
    sql = """
        SELECT aar_data, recorded_at
        FROM platform.aar_events
        WHERE engine = $1 AND recorded_at >= $2
        ORDER BY recorded_at DESC
        LIMIT 100
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, engine, since)
    out: list[AfterActionReport] = []
    for r in rows:
        data = r["aar_data"]
        if isinstance(data, dict):
            payload = json.dumps(data)
        else:
            payload = data
        try:
            out.append(AfterActionReport.model_validate_json(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tip_sheet.aar.parse_failed", engine=engine, error=str(exc)[:200],
            )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────────────


def render_header(engine: str, as_of: datetime) -> str:
    desc = ENGINE_DESCRIPTIONS.get(engine, "(no layman description available)")
    out = [
        "═" * 77,
        f"  TIP SHEET — {engine.upper()}    generated {as_of.isoformat(timespec='seconds')}",
        "═" * 77,
        "",
        desc,
        "",
    ]
    return "\n".join(out)


def render_credibility(engine: str, score: CredibilityScore | None, force: bool) -> str:
    if score is None:
        return (
            f"\nCredibility — no rubric row on record for engine '{engine}'.\n"
            f"  Run the engine's backtest to produce a row in platform.data_quality_log.\n"
        )
    block = render_rubric(score)
    if score.score < MIN_LIVE_SCORE:
        gate_note = (
            f"\n  ▶ GATE: BLOCKED (score {score.score} < {MIN_LIVE_SCORE})"
            f"{' — bypassed via --force; private review only' if force else ''}"
        )
    else:
        gate_note = f"\n  ▶ GATE: PASS (score {score.score} ≥ {MIN_LIVE_SCORE})"
    return block + gate_note + "\n"


def render_signals(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "\nRecent signals — none in window.\n"
    out = ["", "Recent signals  (newest first)", "─" * 77]
    for s in signals[:20]:
        ts = s.get("recorded_at")
        msg = s.get("message", "")[:80]
        data = s.get("data") or {}
        ticker = data.get("ticker", "?") if isinstance(data, dict) else "?"
        score = data.get("score", "") if isinstance(data, dict) else ""
        out.append(f"  {ts}  {ticker:<8} score={score}  {msg}")
    if len(signals) > 20:
        out.append(f"  … ({len(signals) - 20} more)")
    out.append("")
    return "\n".join(out)


def render_trades(trades: list[AfterActionReport]) -> str:
    if not trades:
        return "\nRecent completed trades (AARs) — none in window.\n"
    out = ["", "Recent completed trades  (newest first)", "─" * 77]
    out.append(
        f"  {'ticker':<8} {'entry':<12} {'exit':<12} {'entry_px':>10} "
        f"{'exit_px':>10} {'pnl_net':>10}  exit_reason"
    )
    out.append("  " + "─" * 73)
    for t in trades[:20]:
        e_d = t.entry_ts.date().isoformat()
        x_d = t.exit_ts.date().isoformat()
        out.append(
            f"  {t.ticker:<8} {e_d:<12} {x_d:<12} {float(t.entry_price):>10.2f} "
            f"{float(t.exit_price):>10.2f} {float(t.pnl_net):>10.2f}  "
            f"{t.exit_reason.value}"
        )
    if len(trades) > 20:
        out.append(f"  … ({len(trades) - 20} more)")
    # Aggregate stats
    n = len(trades)
    n_win = sum(1 for t in trades if t.pnl_net > 0)
    total_pnl = sum(float(t.pnl_net) for t in trades)
    out.append("")
    out.append(
        f"  totals: {n} trades  wins={n_win} ({n_win/n*100:.1f}%)  "
        f"pnl_net=${total_pnl:+,.2f}"
    )
    out.append("")
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
        return 2

    since = args.since or (datetime.now(UTC) - timedelta(days=args.days))
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        score = await fetch_credibility(pool, args.engine)
        # Gate enforcement
        if score is not None and score.score < MIN_LIVE_SCORE and not args.force:
            print(render_header(args.engine, datetime.now(UTC)))
            print(render_credibility(args.engine, score, force=False))
            print(
                f"\n  Output suppressed — credibility gate ({MIN_LIVE_SCORE}) not met. "
                f"Use --force to view anyway for private review.\n"
            )
            print(DISCLAIMER)
            return 1
        if score is None and not args.force:
            print(render_header(args.engine, datetime.now(UTC)))
            print(
                f"\n  Output suppressed — no credibility rubric on record. "
                f"Use --force to view anyway for private review.\n"
            )
            print(DISCLAIMER)
            return 1

        signals = await fetch_recent_signals(pool, args.engine, since)
        trades = await fetch_recent_trades(pool, args.engine, since)
    finally:
        await pool.close()

    print(render_header(args.engine, datetime.now(UTC)))
    print(render_credibility(args.engine, score, force=args.force))
    print(render_signals(signals))
    print(render_trades(trades))
    print(DISCLAIMER)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--engine",
        required=True,
        choices=tuple(ENGINE_DESCRIPTIONS.keys()),
        help="Which engine to report on.",
    )
    p.add_argument(
        "--days", type=int, default=30,
        help="Lookback window in days for signals + trades (default 30).",
    )
    p.add_argument(
        "--since", type=datetime.fromisoformat, default=None,
        help="Explicit ISO datetime lower bound (overrides --days).",
    )
    p.add_argument(
        "--force", action="store_true",
        help=(
            "Bypass the credibility ≥ 60 gate. Intended for private operator "
            "review of unproven engines. The disclaimer is NOT lifted."
        ),
    )
    p.add_argument("--database-url", default=None)
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
