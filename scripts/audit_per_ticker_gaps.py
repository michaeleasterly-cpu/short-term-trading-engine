"""Per-ticker gap audit for ``platform.prices_daily``.

For every ticker, compare:
  * actual bar count between first_bar_date and last_bar_date
  * expected NYSE session count over that same range (via tpcore.calendar)

A "gap" is any expected session inside a ticker's lifetime that has no
row. Reports per-ticker coverage % and the specific missing dates so
the caller can backfill.

Cheap: one SQL aggregation + one calendar enumeration per ticker.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

from tpcore.calendar import sessions_in_range
from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.audit_per_ticker_gaps")

# exchange_calendars XNYS data starts 2006-05-15 in the installed version.
# Tickers with bars older than this are clamped — we can't validate sessions
# we don't have a calendar for.
_CALENDAR_FLOOR = date(2006, 5, 15)


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        async with pool.acquire() as conn:
            if args.tier_le:
                ticker_filter = "WHERE ticker IN (SELECT ticker FROM platform.liquidity_tiers WHERE tier <= $1)"
                arg_list = [args.tier_le]
            else:
                ticker_filter = ""
                arg_list = []
            # Only count bars within the calendar's supported range —
            # the gap math is meaningless outside it.
            sql = f"""
                SELECT ticker,
                       MIN(date) FILTER (WHERE date >= DATE '{_CALENDAR_FLOOR.isoformat()}') AS first_d,
                       MAX(date) FILTER (WHERE date >= DATE '{_CALENDAR_FLOOR.isoformat()}') AS last_d,
                       COUNT(*)  FILTER (WHERE date >= DATE '{_CALENDAR_FLOOR.isoformat()}') AS bars,
                       BOOL_OR(delisted) AS ever_delisted,
                       MAX(delisting_date) AS delisting_date
                FROM platform.prices_daily
                {ticker_filter}
                GROUP BY ticker
                HAVING COUNT(*) FILTER (WHERE date >= DATE '{_CALENDAR_FLOOR.isoformat()}') > 0
                ORDER BY ticker
            """
            rows = await conn.fetch(sql, *arg_list)

        if not rows:
            print("no tickers found")
            return 0
        print(f"auditing {len(rows)} tickers")

        # Compute expected sessions per ticker via tpcore.calendar.
        # Cap "last_d" at delisting_date when the ticker is delisted —
        # we don't expect bars after delisting.
        gap_summary: list[tuple[str, int, int, int, float, date, date]] = []
        for r in rows:
            ticker = r["ticker"]
            first_d = r["first_d"]
            last_d = r["last_d"]
            if r["ever_delisted"] and r["delisting_date"] is not None:
                last_d = min(last_d, r["delisting_date"])
            # Clamp to the calendar's available range.
            audit_start = max(first_d, _CALENDAR_FLOOR)
            audit_end = min(last_d, date.today())
            if audit_end < audit_start:
                continue
            sessions = sessions_in_range(audit_start, audit_end)
            n_expected = len(sessions)
            n_actual = int(r["bars"])
            gap = n_expected - n_actual
            if gap > 0:
                coverage = n_actual / n_expected if n_expected else 1.0
                gap_summary.append(
                    (ticker, n_actual, n_expected, gap, coverage, first_d, last_d)
                )

        if not gap_summary:
            print("\n🟢 every ticker has 100% session coverage within its lifetime")
            return 0

        # Group by severity. Severity = absolute gap count (not %).
        gap_summary.sort(key=lambda t: -t[3])
        worst = [t for t in gap_summary if t[3] >= 100]
        moderate = [t for t in gap_summary if 10 <= t[3] < 100]
        small = [t for t in gap_summary if t[3] < 10]
        print(f"\n🔴 {len(gap_summary)} ticker(s) with gaps:")
        print(f"   worst (≥100 missing):   {len(worst)}")
        print(f"   moderate (10-99):       {len(moderate)}")
        print(f"   small (1-9):            {len(small)}")

        # Show top 20 worst offenders.
        print("\n=== top 20 worst gaps ===")
        for t in gap_summary[:20]:
            ticker, actual, expected, gap, cov, first_d, last_d = t
            print(
                f"  {ticker:8s}  bars={actual:>5}/{expected:<5} "
                f"gap={gap:>5}  cov={cov:.1%}  range={first_d}..{last_d}"
            )

        # If --emit-missing-csv, dump every (ticker, date) tuple that's missing.
        if args.emit_missing_csv:
            from pathlib import Path
            out = Path(__file__).resolve().parent.parent / "data" / "alpaca_backfill"
            out.mkdir(parents=True, exist_ok=True)
            from datetime import UTC, datetime
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            missing_path = out / f"missing_session_dates_{ts}.csv"
            import csv as _csv
            n_missing = 0
            with missing_path.open("w", newline="") as fh:
                w = _csv.writer(fh)
                w.writerow(["ticker", "date"])
                for t in gap_summary:
                    ticker, _a, _e, _g, _c, first_d, last_d = t
                    # Fetch the actual bar dates for this ticker so we know
                    # which sessions are present vs missing.
                    async with pool.acquire() as conn:
                        bar_dates = {
                            r["date"]
                            for r in await conn.fetch(
                                "SELECT date FROM platform.prices_daily WHERE ticker=$1 AND date BETWEEN $2 AND $3",
                                ticker, first_d, last_d,
                            )
                        }
                    for s in sessions_in_range(first_d, last_d):
                        if s not in bar_dates:
                            w.writerow([ticker, s.isoformat()])
                            n_missing += 1
            print(f"\nwrote {n_missing} missing (ticker, date) rows to {missing_path}")
        return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--tier-le",
        type=int,
        help="restrict to liquidity_tiers tier <= N (default = all tickers)",
    )
    p.add_argument(
        "--emit-missing-csv",
        action="store_true",
        help="write every missing (ticker, date) to data/alpaca_backfill/missing_session_dates_*.csv",
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
