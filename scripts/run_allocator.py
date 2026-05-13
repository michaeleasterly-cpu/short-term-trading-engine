"""CLI for the weekly Allocator rebalance.

Run via ``scripts/run_allocator.sh``. Idempotent on
``(engine, allocation_date)`` — re-running on the same date updates
the existing row in-place.

Flags:
  --platform-capital N   total capital to distribute (default $40,000)
  --enforce-freeze       update risk_state.kill_switch_active on hard freeze
                         (default OFF for paper trading)
  --as-of YYYY-MM-DD     rebalance date (default today UTC)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from decimal import Decimal

from tpcore.allocator import AllocatorService
from tpcore.db import build_asyncpg_pool


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        svc = AllocatorService(
            pool,
            platform_capital=Decimal(args.platform_capital),
            enforce_freeze=args.enforce_freeze,
            as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        )
        decisions = await svc.run_once()
        print(f"\nrebalance complete — {len(decisions)} engine(s)")
        for d in decisions:
            vol = f"σ={d.realized_vol:.2f}" if d.realized_vol is not None else "σ=bootstrap"
            print(
                f"  {d.engine:9s}  weight={d.weight:>7.4f}  "
                f"capital=${d.allocated_capital:>10}  {vol}  "
                f"state={d.freeze_state}  dd={d.drawdown_pct or 0:.1%}"
            )
        return 0
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--platform-capital", default="40000")
    p.add_argument("--enforce-freeze", action="store_true",
                   help="update risk_state.kill_switch_active; default OFF (paper)")
    p.add_argument("--as-of", help="rebalance date YYYY-MM-DD; default today UTC")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
