"""Apply splits from ``platform.corporate_actions`` to ``platform.prices_daily``.

Why this exists
---------------
Alpaca's IEX free-tier bars endpoint is inconsistent about which symbols
get split-adjusted (notably AAPL is *not* adjusted across the 2020-08-31
4:1 split despite ``adjustment="all"``). This script reads splits from
``platform.corporate_actions`` (populated by
``tpcore.data.ingest_corporate_actions``) and back-adjusts pre-event bars
in ``platform.prices_daily`` for any ticker whose data still looks raw.

Idempotency
-----------
Each split's "already applied?" check uses the close-price ratio across
the split day:

    observed_ratio = close[action_date - 1] / close[action_date]

* If ``observed_ratio >= RATIO_RAW_THRESHOLD`` (default ``1.5``), the data
  is raw and we apply the split.
* Otherwise (the ratio is near 1.0, plus or minus real day-over-day price
  action), we treat it as already adjusted and skip.

A second run is therefore a no-op for any split this script previously
applied. The threshold ``1.5`` works for any forward split of 2:1 or
larger — every forward split since the 1980s has been at least 2:1.

Cumulative splits (e.g. NVDA had a 4:1 in 2021 and a 10:1 in 2024) work
correctly: applying each split divides only its pre-event rows, so the
2021 rows end up divided by 4 × 10 = 40 once both have run.

The volume column is multiplied by the split factor so it stays in
post-split-share units, consistent with the price adjustment.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Ratios at or above this threshold are treated as raw (e.g. 4.0 across an
# AAPL 4:1 split). Any ratio below — including normal split-day price
# action like TSLA's +12.5% in 2020 (ratio ~0.89) — is treated as already
# adjusted and the split is skipped.
RATIO_RAW_THRESHOLD = Decimal("1.5")

_FETCH_AROUND_SQL = """
    SELECT date, close
    FROM platform.prices_daily
    WHERE ticker = $1 AND date <= $2
    ORDER BY date DESC
    LIMIT 2
"""

_UPDATE_PRESPLIT_SQL = """
    UPDATE platform.prices_daily
    SET
        open           = open / $1,
        high           = high / $1,
        low            = low / $1,
        close          = close / $1,
        adjusted_close = adjusted_close / $1,
        volume         = (volume * $1)::bigint
    WHERE ticker = $2 AND date < $3
"""


async def apply_split(
    pool: "asyncpg.Pool",
    ticker: str,
    action_date: date,
    ratio: Decimal,
) -> dict:
    """Adjust pre-action_date bars for ``ticker`` if the data is still raw.

    Returns a dict with at least ``applied`` (bool). When applied=True it
    also includes ``n_rows_updated``, ``before`` (close on the day before),
    and ``after`` (close on action_date). When applied=False, ``reason`` is
    one of ``"missing_bars"``, ``"already_adjusted"``.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FETCH_AROUND_SQL, ticker, action_date)
        # We need at least two bars and the most recent one must be on action_date.
        if len(rows) < 2 or rows[0]["date"] != action_date:
            logger.info(
                "tpcore.apply_splits.skip_missing_bars",
                ticker=ticker,
                action_date=action_date.isoformat(),
                n_rows=len(rows),
            )
            return {"applied": False, "reason": "missing_bars", "ticker": ticker}

        close_after = Decimal(str(rows[0]["close"]))
        close_before = Decimal(str(rows[1]["close"]))
        observed_ratio = close_before / close_after

        if observed_ratio < RATIO_RAW_THRESHOLD:
            logger.debug(
                "tpcore.apply_splits.skip_already_adjusted",
                ticker=ticker,
                action_date=action_date.isoformat(),
                observed_ratio=str(observed_ratio),
                threshold=str(RATIO_RAW_THRESHOLD),
            )
            return {
                "applied": False,
                "reason": "already_adjusted",
                "ticker": ticker,
                "observed_ratio": observed_ratio,
            }

        result = await conn.execute(_UPDATE_PRESPLIT_SQL, ratio, ticker, action_date)

    n_rows = _parse_update_count(result)
    logger.info(
        "tpcore.apply_splits.applied",
        ticker=ticker,
        action_date=action_date.isoformat(),
        ratio=str(ratio),
        n_rows_updated=n_rows,
        before_close=str(close_before),
        after_close=str(close_after),
    )
    return {
        "applied": True,
        "ticker": ticker,
        "n_rows_updated": n_rows,
        "before": close_before,
        "after": close_after,
    }


def _parse_update_count(status: str) -> int:
    """asyncpg's `execute()` returns a string like 'UPDATE 4527'."""
    if isinstance(status, str) and status.startswith("UPDATE"):
        try:
            return int(status.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0


async def apply_all_splits(pool: "asyncpg.Pool", *, only_tickers: list[str] | None = None) -> dict:
    """Apply every split in ``platform.corporate_actions`` to prices_daily.

    Iterates splits in ascending action_date order; cumulative effects are
    correct because each apply_split call only touches rows strictly before
    its own action_date.
    """
    sql = """
        SELECT ticker, action_date, ratio
        FROM platform.corporate_actions
        WHERE action_type = 'split'
    """
    params: list = []
    if only_tickers:
        sql += " AND ticker = ANY($1)"
        params.append(only_tickers)
    sql += " ORDER BY action_date ASC, ticker ASC"

    async with pool.acquire() as conn:
        splits = await conn.fetch(sql, *params)

    summary = {"applied": [], "skipped": []}
    for s in splits:
        outcome = await apply_split(pool, s["ticker"], s["action_date"], Decimal(str(s["ratio"])))
        if outcome["applied"]:
            summary["applied"].append(outcome)
        else:
            summary["skipped"].append(outcome)
    return summary


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _CLIArgs:
    tickers: list[str] | None


def _parse_args(argv: list[str] | None = None) -> _CLIArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--tickers",
        type=lambda s: [t.strip().upper() for t in s.split(",") if t.strip()],
        default=None,
        help="Optional comma-separated tickers. Default: all tickers with splits.",
    )
    a = p.parse_args(argv)
    return _CLIArgs(tickers=a.tickers)


async def amain(args: _CLIArgs) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        summary = await apply_all_splits(pool, only_tickers=args.tickers)
    finally:
        await pool.close()

    print(f"applied: {len(summary['applied'])}  skipped: {len(summary['skipped'])}")
    for a in summary["applied"]:
        print(
            f"  APPLY  {a['ticker']:6s} rows={a['n_rows_updated']:5d} "
            f"before={a['before']} after={a['after']}"
        )
    for s in summary["skipped"]:
        reason = s.get("reason", "?")
        ratio = s.get("observed_ratio", "")
        print(f"  SKIP   {s['ticker']:6s} reason={reason} ratio={ratio}")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "apply_split",
    "apply_all_splits",
    "RATIO_RAW_THRESHOLD",
    "amain",
    "main",
]
