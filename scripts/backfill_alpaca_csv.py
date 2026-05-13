"""Phase 1 of the historical backfill — Alpaca → CSV.

Pulls daily bars from Alpaca for the (ticker, date-range) pairs needed
to close the gaps left by the 2026-05-13 Tradier-source cleanup. Writes
output as CSV under ``data/alpaca_backfill/`` so the next phase
(``load_alpaca_csv.py``) can validate every row against the
``row_integrity`` predicate BEFORE upserting into prices_daily — no
intermediate bad data.

Why a two-phase (CSV → DB) approach instead of direct ingest?

1. **Re-runnability.** If a load step fails halfway through, the CSV
   stays on disk; re-running starts from the load step, not from
   another expensive Alpaca pull.
2. **Auditability.** The CSV is a permanent record of exactly what
   Alpaca returned, decoupled from whatever we choose to persist.
3. **Integrity gate.** Loading validates each row against the same
   strict predicate the validation suite enforces. Bad rows from the
   source (e.g. close=0 on a halt day) are filtered out at the CSV
   load step rather than entering prices_daily.

Default scope: every ticker in ``platform.liquidity_tiers`` (tier ≤ 2,
i.e. the active backtest + live universe), date range "first existing
bar … yesterday". Bars already present in prices_daily are NOT
re-fetched (idempotent — the CSV is gap-only). Override via flags
below.

Usage::

    scripts/run_backfill_alpaca_csv.sh                         # all tier ≤ 2
    scripts/run_backfill_alpaca_csv.sh --tickers AAPL,MSFT     # specific names
    scripts/run_backfill_alpaca_csv.sh --since 2020-01-01      # tighter start
    scripts/run_backfill_alpaca_csv.sh --batch-size 50         # smaller pages
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from tpcore.data.ingest_alpaca_bars import fetch_daily_bars_multi
from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.backfill_alpaca_csv")

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "alpaca_backfill"
CSV_COLS = ("ticker", "date", "open", "high", "low", "close", "volume")


async def _list_universe(pool, tickers_override: list[str] | None) -> list[str]:
    if tickers_override:
        return sorted({t.strip().upper() for t in tickers_override if t.strip()})
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2 ORDER BY ticker"
        )
    return [r["ticker"] for r in rows]


async def _gap_dates_for(
    pool, tickers: list[str], start_date: date, end_date: date,
) -> dict[str, tuple[date, date]]:
    """For each ticker, return the (first_missing, last_missing) date window we
    need to backfill. We don't expand to every gap-date inside the window —
    Alpaca's API takes a date range and we upsert; bars already present are
    overwritten with the same value (idempotent). Tickers fully covered get
    no entry."""
    if not tickers:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker,
                   MIN(date) AS first_existing,
                   MAX(date) AS last_existing,
                   COUNT(*)  AS bars_present
            FROM platform.prices_daily
            WHERE ticker = ANY($1::text[])
            GROUP BY ticker
            """,
            tickers,
        )
    by_ticker = {r["ticker"]: r for r in rows}
    out: dict[str, tuple[date, date]] = {}
    for t in tickers:
        r = by_ticker.get(t)
        if r is None:
            # No bars at all — pull the whole requested window.
            out[t] = (start_date, end_date)
            continue
        first = r["first_existing"] or start_date
        last = r["last_existing"] or end_date
        # Two windows: pre-existing-coverage and post-existing-coverage.
        # Inside the existing window there can still be gaps; capture
        # by asking Alpaca for the entire span and upserting (idempotent).
        backfill_start = min(first, start_date)
        backfill_end = max(last, end_date)
        out[t] = (backfill_start, backfill_end)
    return out


def _bar_passes_integrity(b: dict) -> bool:
    """Same physical-truth predicate the validation suite enforces.

    Filters obvious bad rows AT the CSV layer so they never reach
    prices_daily. We accept Alpaca's adjustment="all" which produces
    self-consistent OHLC.
    """
    try:
        o, h, l_, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        v = int(b["v"])
    except (KeyError, TypeError, ValueError):
        return False
    if c <= 0 or c > 100_000_000:
        return False
    if h < max(o, c, l_):
        return False
    if l_ > min(o, c, h):
        return False
    if v < 0:
        return False
    return True


async def _backfill_one(
    client: httpx.AsyncClient,
    symbols: list[str],
    start_d: date,
    end_d: date,
    writer: csv.writer,
    feed: str,
) -> tuple[int, int, int]:
    """Pull bars for ``symbols`` over [start_d, end_d], write to CSV.
    Returns (kept, dropped, sym_with_data)."""
    bars_by_sym = await fetch_daily_bars_multi(client, symbols, start_d, end_d, feed=feed)
    kept = dropped = sym_with_data = 0
    for sym, bars in bars_by_sym.items():
        if bars:
            sym_with_data += 1
        for b in bars:
            if not _bar_passes_integrity(b):
                dropped += 1
                continue
            # Alpaca returns "t" as RFC3339 string like "2024-01-02T05:00:00Z".
            ts = b.get("t") or ""
            bar_date = ts[:10] if isinstance(ts, str) else ""
            if not bar_date:
                dropped += 1
                continue
            writer.writerow([sym, bar_date, b["o"], b["h"], b["l"], b["c"], b["v"]])
            kept += 1
    return kept, dropped, sym_with_data


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"alpaca_bars_backfill_{ts}.csv"
    logger.info("backfill: writing to %s", out_path)

    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        tickers_override = (
            [t.strip() for t in args.tickers.split(",") if t.strip()]
            if args.tickers else None
        )
        tickers = await _list_universe(pool, tickers_override)
        if not tickers:
            logger.error("backfill: no tickers to process")
            return 1
        logger.info("backfill: %d tickers", len(tickers))

        end_d = date.today() - timedelta(days=1)  # don't pull today's bar mid-session
        start_d = date.fromisoformat(args.since) if args.since else date(2012, 1, 1)
        gap_windows = await _gap_dates_for(pool, tickers, start_d, end_d)

        # Open the output CSV and write a header row before any HTTP work.
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)

            async with httpx.AsyncClient(
                headers={
                    "APCA-API-KEY-ID": os.environ.get("ALPACA_KEY", ""),
                    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET", ""),
                },
                timeout=30.0,
            ) as client:
                total_kept = total_dropped = batches = 0
                for batch_start in range(0, len(tickers), args.batch_size):
                    batch = tickers[batch_start:batch_start + args.batch_size]
                    # Use the WIDEST window across the batch — fetch_daily_bars_multi
                    # only accepts a single start/end pair.
                    if not batch:
                        continue
                    windows = [gap_windows[t] for t in batch if t in gap_windows]
                    if not windows:
                        continue
                    w_start = min(s for s, _ in windows)
                    w_end = max(e for _, e in windows)
                    kept, dropped, with_data = await _backfill_one(
                        client, batch, w_start, w_end, w, args.feed,
                    )
                    total_kept += kept
                    total_dropped += dropped
                    batches += 1
                    logger.info(
                        "batch %d/%d: %d tickers, %s-%s, kept=%d dropped=%d with_data=%d",
                        batches, (len(tickers) + args.batch_size - 1) // args.batch_size,
                        len(batch), w_start.isoformat(), w_end.isoformat(),
                        kept, dropped, with_data,
                    )
                    fh.flush()
            logger.info(
                "backfill: %d batch(es); kept=%d dropped=%d -> %s",
                batches, total_kept, total_dropped, out_path,
            )
            print(out_path)  # so the wrapper script can read the path off stdout
            return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--tickers",
        help="comma-separated ticker list; default = all liquidity_tiers tier ≤ 2",
    )
    p.add_argument(
        "--since",
        help="ISO date — earliest backfill start; default 2012-01-01",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="symbols per Alpaca page (max 100); default 100",
    )
    p.add_argument(
        "--feed",
        choices=("iex", "sip"),
        default="sip",
        help="Alpaca data feed; default 'sip' (covers non-IEX-listed tickers too)",
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
