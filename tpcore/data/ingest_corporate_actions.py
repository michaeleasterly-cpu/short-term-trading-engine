"""Ingest splits and dividends from Alpaca's free ``/v1/corporate-actions``.

Why this exists
---------------
Alpaca's IEX free-tier bars endpoint applies split adjustments inconsistently
across symbols (notably it returns *raw* prices for AAPL across the 2020 4:1
split despite ``adjustment="all"``). The corporate-actions endpoint, in
contrast, returns the actual events for every symbol — so we ingest those
events here and let ``tpcore.data.apply_splits`` back-adjust
``platform.prices_daily`` ourselves.

Endpoint shape (verified 2026-05-10):

    GET https://data.alpaca.markets/v1/corporate-actions
        ?symbols=A,B,C&start=YYYY-MM-DD&end=YYYY-MM-DD&types=forward_split,cash_dividend
    -> {
        "corporate_actions": {
          "forward_splits": [{"symbol", "ex_date", "new_rate", "old_rate", ...}],
          "cash_dividends":  [{"symbol", "ex_date", "rate", ...}],
          ...
        },
        "next_page_token": "..." | null
       }

We normalize each event into a flat record:

    {"ticker", "action_date" (= ex_date), "action_type" ("split"|"dividend"),
     "ratio" (split factor for splits; per-share USD for dividends),
     "raw_data" (full Alpaca object)}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import structlog

from tpcore.data.ingest_alpaca_bars import _alpaca_headers
from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

_ENDPOINT_PATH = "/v1/corporate-actions"
_DATA_BASE = "https://data.alpaca.markets"

# Alpaca's response groups events by type. Keys we know how to parse:
_SPLIT_KEYS = ("forward_splits",)
_DIVIDEND_KEYS = ("cash_dividends",)
# Reverse splits and stock dividends exist but are out of scope for MVP.

DEFAULT_TYPES: tuple[str, ...] = ("forward_split", "cash_dividend")


# ────────────────────────────────────────────────────────────────────────────
# Fetch
# ────────────────────────────────────────────────────────────────────────────


async def fetch_corporate_actions(
    client: httpx.AsyncClient,
    *,
    symbols: list[str],
    start: date,
    end: date,
    types: list[str] | None = None,
) -> list[dict]:
    """Page through ``/v1/corporate-actions`` and return normalized records."""
    params: dict[str, str] = {
        "symbols": ",".join(symbols),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if types:
        params["types"] = ",".join(types)

    out: list[dict] = []
    while True:
        resp = await client.get(_ENDPOINT_PATH, params=params)
        resp.raise_for_status()
        body = resp.json()
        out.extend(_normalize(body.get("corporate_actions") or {}))
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return out


def _normalize(payload: dict) -> list[dict]:
    """Convert Alpaca's per-type lists into a flat list of normalized events."""
    out: list[dict] = []
    for key in _SPLIT_KEYS:
        for raw in payload.get(key, []) or []:
            out.append(_normalize_split(raw))
    for key in _DIVIDEND_KEYS:
        for raw in payload.get(key, []) or []:
            out.append(_normalize_dividend(raw))
    return out


def _normalize_split(raw: dict) -> dict:
    new_rate = Decimal(str(raw["new_rate"]))
    old_rate = Decimal(str(raw["old_rate"]))
    return {
        "ticker": raw["symbol"],
        "action_date": date.fromisoformat(raw["ex_date"]),
        "action_type": "split",
        "ratio": new_rate / old_rate,
        "raw_data": raw,
    }


def _normalize_dividend(raw: dict) -> dict:
    return {
        "ticker": raw["symbol"],
        "action_date": date.fromisoformat(raw["ex_date"]),
        "action_type": "dividend",
        "ratio": Decimal(str(raw["rate"])),
        "raw_data": raw,
    }


# ────────────────────────────────────────────────────────────────────────────
# Persist
# ────────────────────────────────────────────────────────────────────────────


_INSERT_SQL = """
    INSERT INTO platform.corporate_actions (
        ticker, action_date, action_type, ratio, raw_data
    )
    VALUES ($1, $2, $3, $4, $5::jsonb)
    ON CONFLICT (ticker, action_date, action_type) DO NOTHING
"""


async def upsert_corporate_actions(
    pool: "asyncpg.Pool",
    actions: list[dict],
) -> int:
    """Insert each action; returns the count attempted (not the count newly written)."""
    if not actions:
        return 0
    rows = [
        (
            a["ticker"],
            a["action_date"],
            a["action_type"],
            a["ratio"],
            json.dumps(a["raw_data"], default=str),
        )
        for a in actions
    ]
    async with pool.acquire() as conn:
        await conn.executemany(_INSERT_SQL, rows)
    return len(rows)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _CLIArgs:
    symbols: list[str]
    start: date
    end: date
    types: list[str]
    chunk_size: int


def _parse_args(argv: list[str] | None = None) -> _CLIArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--symbols",
        type=lambda s: [t.strip().upper() for t in s.split(",") if t.strip()],
        required=True,
        help="Comma-separated tickers.",
    )
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date.today())
    p.add_argument(
        "--types",
        type=lambda s: [t.strip() for t in s.split(",") if t.strip()],
        default=list(DEFAULT_TYPES),
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=20,
        help="Symbols per API call (Alpaca accepts comma-separated lists; chunk to avoid URL length issues).",
    )
    a = p.parse_args(argv)
    return _CLIArgs(
        symbols=a.symbols, start=a.start, end=a.end, types=a.types, chunk_size=a.chunk_size
    )


async def amain(args: _CLIArgs) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    headers = _alpaca_headers()
    pool = await build_asyncpg_pool(db_url)
    total_actions = 0
    try:
        async with httpx.AsyncClient(headers=headers, base_url=_DATA_BASE, timeout=60.0) as client:
            for i in range(0, len(args.symbols), args.chunk_size):
                chunk = args.symbols[i : i + args.chunk_size]
                actions = await fetch_corporate_actions(
                    client,
                    symbols=chunk,
                    start=args.start,
                    end=args.end,
                    types=args.types,
                )
                if actions:
                    await upsert_corporate_actions(pool, actions)
                total_actions += len(actions)
                logger.info(
                    "tpcore.corporate_actions.chunk_done",
                    chunk_size=len(chunk),
                    n_actions=len(actions),
                )
    finally:
        await pool.close()
    logger.info("tpcore.corporate_actions.run_done", total=total_actions)
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "fetch_corporate_actions",
    "upsert_corporate_actions",
    "amain",
    "main",
    "DEFAULT_TYPES",
]
