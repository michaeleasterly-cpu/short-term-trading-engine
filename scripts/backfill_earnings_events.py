"""Backfill ``platform.earnings_events`` with FMP earnings events.

Emits **two** ``event_type`` values, never silently drops a reported
event:

* ``EARNINGS_BEAT`` — ``actual_eps > estimated_eps × (1 + 0.05)``
  (i.e. a >5% beat). ``magnitude_pct = (actual − estimated) /
  estimated`` (sentinel ``999999`` on a zero-estimate / positive-actual
  edge).
* ``EARNINGS_NO_BEAT`` — actual + estimated both present but not a beat
  (miss, in-line, zero-estimate-with-non-positive-actual, negative-
  estimate). ``magnitude_pct = NULL`` (the beat magnitude is undefined
  for misses).

Rows where ``epsActual`` OR ``epsEstimated`` is ``None`` are STILL
skipped — no event happened (pre-announcement / calendar-only /
suspended).

Why both populations: the per-ticker monotone-non-decrease invariant
(``earnings_events_monotone``) gates on the UNION
``event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')``. Storing only
beats let an FMP outage silently miss-detect a quarter (no row at all)
without ever tripping the invariant. With NO_BEAT sentinels, every
reported event lands as a row — the monotone-on-the-union invariant
now catches both vendor truncation AND missed-detection.

Downstream consumers (``vector/backtest.py``, ``catalyst/backtest.py``)
filter ``event_type='EARNINGS_BEAT'`` so they are unaffected by
NO_BEAT rows.

Run::

    python scripts/backfill_earnings_events.py
    python scripts/backfill_earnings_events.py --start 2018-01-01 --end 2025-12-31

FMP endpoint (verified 2026-05-11): ``GET /stable/earnings?symbol={t}``
returns the full historical + future earnings calendar for one symbol
in a single response. We filter client-side to the requested window.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from decimal import Decimal

import httpx

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.backfill_earnings_events")

DEFAULT_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)

INTER_SYMBOL_SLEEP_S = 0.4

EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"
BEAT_THRESHOLD = Decimal("0.05")  # > 5% beat


_INSERT_SQL = """
    INSERT INTO platform.earnings_events
        (ticker, event_date, event_type, magnitude_pct, source, recorded_at)
    VALUES ($1, $2, $3, $4, $5, now())
    ON CONFLICT (ticker, event_date, event_type) DO NOTHING
"""


async def fetch_earnings(client: httpx.AsyncClient, symbol: str, api_key: str) -> list[dict]:
    """Pull the full /stable/earnings history for one symbol."""
    try:
        resp = await client.get(EARNINGS_URL, params={"symbol": symbol, "apikey": api_key})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("fmp.earnings_fetch_failed symbol=%s err=%s", symbol, exc)
        return []
    body = resp.json()
    return body if isinstance(body, list) else []


def _classify_earnings(row: dict) -> tuple[str, Decimal | None] | None:
    """Classify one FMP earnings row.

    Returns:
        ``None`` if ``epsActual`` OR ``epsEstimated`` is missing — no
        event happened (pre-announcement / calendar-only / suspended).
        Skipping these is intentional: there is nothing to record.

        ``("EARNINGS_BEAT", magnitude)`` if
        ``actual > estimated × (1 + threshold)``. Magnitude is
        ``(actual − estimated) / estimated`` quantized to 6 places.
        Zero-estimate + positive-actual surfaces as sentinel magnitude
        ``999999`` to flag the edge for downstream attention.
        Negative-estimate beats are treated as NO_BEAT — "less of a
        loss than expected" doesn't carry the same momentum.

        ``("EARNINGS_NO_BEAT", None)`` for every other case where both
        sides were reported but the row is not a beat (miss, in-line,
        zero-estimate-with-non-positive-actual, negative-estimate).
        ``magnitude_pct`` is NULL because beat magnitude is undefined
        for misses.

    The NO_BEAT sentinel is what closes the BEAT-only ingestion gap
    that the ``earnings_events_monotone`` invariant flagged: every
    reported event lands as a row so the monotone-on-the-union check
    catches both vendor truncation AND missed-detection from FMP
    outages.
    """
    actual = row.get("epsActual")
    estimated = row.get("epsEstimated")
    if actual is None or estimated is None:
        return None
    try:
        a = Decimal(str(actual))
        e = Decimal(str(estimated))
    except Exception:
        return None
    if e == 0:
        if a > 0:
            return "EARNINGS_BEAT", Decimal("999999")
        return "EARNINGS_NO_BEAT", None
    if e < 0:
        # Negative-estimate "beats" treated as NO_BEAT — see docstring.
        return "EARNINGS_NO_BEAT", None
    pct = (a - e) / e
    if pct > BEAT_THRESHOLD:
        return "EARNINGS_BEAT", pct.quantize(Decimal("0.000001"))
    return "EARNINGS_NO_BEAT", None


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set", file=sys.stderr)
        return 2
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    universe = args.universe or DEFAULT_UNIVERSE
    pool = await build_asyncpg_pool(db_url)
    inserted_total = 0
    skipped_no_data: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, symbol in enumerate(universe, 1):
                rows = await fetch_earnings(client, symbol, api_key)
                if not rows:
                    skipped_no_data.append(symbol)
                    await asyncio.sleep(INTER_SYMBOL_SLEEP_S)
                    continue

                events: list[tuple] = []
                beats_in_window = 0
                no_beats_in_window = 0
                for r in rows:
                    raw_date = r.get("date")
                    if not raw_date:
                        continue
                    try:
                        ev_date = date.fromisoformat(raw_date)
                    except ValueError:
                        continue
                    if ev_date < args.start or ev_date > args.end:
                        continue
                    classification = _classify_earnings(r)
                    if classification is None:
                        continue
                    event_type, magnitude = classification
                    events.append(
                        (symbol, ev_date, event_type, magnitude, "fmp")
                    )
                    if event_type == "EARNINGS_BEAT":
                        beats_in_window += 1
                    else:
                        no_beats_in_window += 1

                if events:
                    async with pool.acquire() as conn:
                        await conn.executemany(_INSERT_SQL, events)
                    inserted_total += len(events)
                logger.info(
                    "[%d/%d] %s earnings_rows=%d beats_in_window=%d no_beats_in_window=%d",
                    i, len(universe), symbol, len(rows), beats_in_window, no_beats_in_window,
                )
                await asyncio.sleep(INTER_SYMBOL_SLEEP_S)

        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT "
                "COUNT(*) FILTER (WHERE event_type='EARNINGS_BEAT') AS beats, "
                "COUNT(*) FILTER (WHERE event_type='EARNINGS_NO_BEAT') AS no_beats, "
                "COUNT(*) AS n_total, "
                "COUNT(DISTINCT ticker) AS t, "
                "MIN(event_date) AS mn, MAX(event_date) AS mx "
                "FROM platform.earnings_events "
                "WHERE event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')"
            )
    finally:
        await pool.close()

    print(f"\nbackfill complete  symbols={len(universe)}  rows_inserted_this_run={inserted_total}")
    print(
        f"earnings_events total: {r['n_total']:,} rows "
        f"(BEAT={r['beats']:,}, NO_BEAT={r['no_beats']:,}) / "
        f"{r['t']} tickers / span {r['mn']}..{r['mx']}"
    )
    if skipped_no_data:
        print(f"no FMP data for: {skipped_no_data}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument(
        "--universe",
        type=lambda s: tuple(t.strip().upper() for t in s.split(",") if t.strip()),
        default=None,
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
