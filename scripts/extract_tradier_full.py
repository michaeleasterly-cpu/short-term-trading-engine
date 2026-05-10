"""Wide Tradier extractor — every accessible US equity & ETF, daily bars to CSV.

Companion to ``scripts/extract_tradier.py`` (which is scoped to the 50-name
backtest universe). This one walks the full Tradier-tradable universe via
``/v1/markets/lookup`` and pulls daily history from 2000-01-01 to today
for each name, streaming results into a single CSV.

The script does **not** touch Postgres. It produces a flat file the
operator can audit, hash, and ingest later.

Behavior:
* One symbol-enumeration call → ``data/tradier_export/tradier_symbols_full.csv``
  (saved on first run, reused on resume so the universe is stable across
  restarts).
* Bars are streamed into ``data/tradier_export/tradier_bars_full.csv`` as
  each symbol completes — a crash mid-run leaves a partial-but-valid CSV.
* Resumable: on startup we scan the existing bars CSV for distinct
  tickers and skip those, so re-running picks up where it left off.
* Rate limit: 0.5s sleep between bars requests (~120 req/min ceiling),
  with a 5-second backoff on HTTP 429.

Run::

    TRADIER_PRODUCTION_TOKEN=... python scripts/extract_tradier_full.py
    python scripts/extract_tradier_full.py --max-symbols 100   # smoke test
    python scripts/extract_tradier_full.py --resume            # default; skips done

Token env var: ``TRADIER_PRODUCTION_TOKEN`` (project standard) — also
accepts ``TRADIER_TOKEN`` as a convenience alias.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import structlog

TRADIER_BASE = "https://api.tradier.com"
DEFAULT_OUT_DIR = Path("data/tradier_export")
SYMBOLS_CSV_NAME = "tradier_symbols_full.csv"
BARS_CSV_NAME = "tradier_bars_full.csv"

INTER_REQUEST_SLEEP_S = 0.5  # 0.5s ≈ 120 req/min ceiling
RATE_LIMIT_BACKOFF_S = 5.0
EXCHANGES = "N,Q,A"  # NYSE, NASDAQ, AMEX
SYMBOL_TYPES = "stock,etf"

BARS_START = date(2000, 1, 1)


def _configure_logging(level: int = logging.INFO) -> structlog.stdlib.BoundLogger:
    """structlog → stdlib bridge so the script's logs render as plain text in CI/foreground.

    ``KeyValueRenderer`` keeps the line greppable while still being structured,
    which matches the tone of the existing ``scripts/extract_tradier.py``.
    """
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    # httpx logs every GET at INFO; in an 8.6k-symbol run that's noise that
    # drowns out our own progress events. Demote to WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.KeyValueRenderer(key_order=["timestamp", "level", "event"]),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    return structlog.get_logger("scripts.extract_tradier_full")


logger: structlog.stdlib.BoundLogger = structlog.get_logger("scripts.extract_tradier_full")


def _ensure_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict | None:
    """GET ``path`` with one 429-aware retry. Returns parsed JSON or None on failure."""
    try:
        resp = await client.get(path, params=params or {})
    except (httpx.RequestError, httpx.HTTPError) as exc:
        logger.warning("tradier.network_error", path=path, params=params, error=str(exc))
        return None
    if resp.status_code == 429:
        logger.warning("tradier.rate_limited", path=path, sleep=RATE_LIMIT_BACKOFF_S)
        await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
        try:
            resp = await client.get(path, params=params or {})
        except Exception as exc:  # noqa: BLE001 - log + skip is the only sane move here
            logger.warning("tradier.retry_failed", path=path, error=str(exc))
            return None
    if resp.status_code != 200:
        logger.warning(
            "tradier.http_error",
            path=path,
            status=resp.status_code,
            body=resp.text[:200],
        )
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("tradier.non_json_response", path=path, body=resp.text[:200])
        return None


# ---------------------------------------------------------------------------
# Symbol enumeration
# ---------------------------------------------------------------------------


SYMBOLS_COLUMNS = ["symbol", "exchange", "type", "description"]


async def fetch_universe(client: httpx.AsyncClient) -> list[dict]:
    """Pull every stock + ETF on NYSE/NASDAQ/AMEX in a single call.

    Tradier's ``/v1/markets/lookup`` returns the full filtered list when
    ``q`` is omitted. ~8.6k symbols today; one call, ~1MB JSON.
    """
    body = await _get(client, "/v1/markets/lookup", {"exchanges": EXCHANGES, "types": SYMBOL_TYPES})
    if not body:
        return []
    securities = body.get("securities") or {}
    return _ensure_list(securities.get("security") or [])


def write_symbols_csv(out: Path, symbols: list[dict]) -> int:
    """Write the universe to disk so reruns can read from cache."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        w.writerow(SYMBOLS_COLUMNS)
        for s in symbols:
            w.writerow(
                [
                    s.get("symbol", ""),
                    s.get("exchange", ""),
                    s.get("type", ""),
                    (s.get("description") or "").replace("\n", " "),
                ]
            )
    return len(symbols)


def read_symbols_csv(path: Path) -> list[str]:
    """Read just the ``symbol`` column from a previously-written symbols CSV."""
    out: list[str] = []
    with path.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            sym = (row.get("symbol") or "").strip()
            if sym:
                out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Bars extraction
# ---------------------------------------------------------------------------


BARS_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume"]


def already_done_tickers(bars_csv: Path) -> set[str]:
    """Distinct ticker set already present in ``bars_csv`` (for resumability).

    Reading the whole file is fine — at ~3GB worst case this still streams
    in <60s, which is trivial relative to an 80-minute extraction. We
    don't index by partial completion: if a ticker has *any* row the
    extractor assumes it's done. (A crash mid-symbol would leave that
    symbol partially written; the operator can re-run after deleting
    the offending rows or accept that it's done.)
    """
    if not bars_csv.exists():
        return set()
    seen: set[str] = set()
    with bars_csv.open(newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        try:
            next(r)  # header
        except StopIteration:
            return seen
        for row in r:
            if row:
                seen.add(row[0])
    return seen


async def fetch_bars(client: httpx.AsyncClient, symbol: str, end: date) -> list[dict]:
    """Daily bars for ``symbol`` from ``BARS_START`` through ``end`` (inclusive)."""
    body = await _get(
        client,
        "/v1/markets/history",
        {
            "symbol": symbol,
            "interval": "daily",
            "start": BARS_START.isoformat(),
            "end": end.isoformat(),
        },
    )
    if not body:
        return []
    history = body.get("history") or {}
    return _ensure_list(history.get("day") or [])


async def extract_all_bars(
    client: httpx.AsyncClient,
    bars_csv: Path,
    symbols: list[str],
    *,
    end: date,
    max_symbols: int | None,
) -> dict[str, Any]:
    done = already_done_tickers(bars_csv)
    work = [s for s in symbols if s not in done]
    if max_symbols is not None:
        work = work[:max_symbols]

    logger.info(
        "extract.start",
        total=len(symbols),
        already_done=len(done),
        to_process=len(work),
        max_symbols=max_symbols,
    )

    bars_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not bars_csv.exists() or bars_csv.stat().st_size == 0
    summary = {
        "tickers_fetched": 0,
        "tickers_no_data": 0,
        "tickers_failed": 0,
        "rows_appended": 0,
    }
    with bars_csv.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        if write_header:
            w.writerow(BARS_COLUMNS)
            fh.flush()

        for i, symbol in enumerate(work, 1):
            try:
                days = await fetch_bars(client, symbol, end)
            except Exception as exc:  # noqa: BLE001 - per-symbol isolation; log + continue
                logger.warning("extract.symbol_exception", symbol=symbol, error=str(exc))
                summary["tickers_failed"] += 1
                await asyncio.sleep(INTER_REQUEST_SLEEP_S)
                continue

            if not days:
                summary["tickers_no_data"] += 1
                logger.info(
                    "extract.no_data",
                    progress=f"{i}/{len(work)}",
                    symbol=symbol,
                )
                await asyncio.sleep(INTER_REQUEST_SLEEP_S)
                continue

            row_count = 0
            for d in days:
                w.writerow(
                    [
                        symbol,
                        d.get("date"),
                        d.get("open"),
                        d.get("high"),
                        d.get("low"),
                        d.get("close"),
                        d.get("volume"),
                    ]
                )
                row_count += 1
            fh.flush()  # incremental durability — a crash mid-run loses ≤1 symbol
            summary["tickers_fetched"] += 1
            summary["rows_appended"] += row_count

            # Periodic progress so a long run doesn't look hung.
            if i % 50 == 0 or i == len(work):
                size_mb = bars_csv.stat().st_size / 1_000_000
                logger.info(
                    "extract.progress",
                    progress=f"{i}/{len(work)}",
                    last_symbol=symbol,
                    rows_so_far=summary["rows_appended"],
                    file_mb=round(size_mb, 2),
                )
            else:
                logger.info(
                    "extract.symbol_done",
                    progress=f"{i}/{len(work)}",
                    symbol=symbol,
                    rows=row_count,
                    span=f"{days[0].get('date')}..{days[-1].get('date')}",
                )

            await asyncio.sleep(INTER_REQUEST_SLEEP_S)

    return summary


# ---------------------------------------------------------------------------
# Glue
# ---------------------------------------------------------------------------


def _resolve_token() -> str | None:
    """Honor the project-standard env var first, then accept the convenience alias."""
    return os.getenv("TRADIER_PRODUCTION_TOKEN") or os.getenv("TRADIER_TOKEN")


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="Stop after processing this many *new* symbols. Useful for smoke tests.",
    )
    p.add_argument(
        "--refresh-symbols",
        action="store_true",
        help="Re-enumerate the universe even if data/tradier_export/tradier_symbols_full.csv exists.",
    )
    p.add_argument(
        "--end",
        type=date.fromisoformat,
        default=date.today(),
        help="End date for bars (default: today).",
    )
    return p.parse_args(list(argv) if argv is not None else None)


async def amain(args: argparse.Namespace) -> int:
    global logger
    logger = _configure_logging()

    token = _resolve_token()
    if not token:
        print(
            "TRADIER_PRODUCTION_TOKEN (or TRADIER_TOKEN) not set in environment.",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols_csv = out_dir / SYMBOLS_CSV_NAME
    bars_csv = out_dir / BARS_CSV_NAME

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(base_url=TRADIER_BASE, headers=headers, timeout=30.0) as client:
        if args.refresh_symbols or not symbols_csv.exists():
            logger.info("symbols.fetch_start")
            universe = await fetch_universe(client)
            if not universe:
                logger.error("symbols.fetch_returned_empty")
                return 3
            count = write_symbols_csv(symbols_csv, universe)
            logger.info("symbols.fetched", count=count, path=str(symbols_csv))
        else:
            logger.info("symbols.cached", path=str(symbols_csv))

        symbols = read_symbols_csv(symbols_csv)
        if not symbols:
            logger.error("symbols.empty_csv", path=str(symbols_csv))
            return 4

        summary = await extract_all_bars(
            client,
            bars_csv,
            symbols,
            end=args.end,
            max_symbols=args.max_symbols,
        )

    size_mb = bars_csv.stat().st_size / 1_000_000 if bars_csv.exists() else 0.0
    logger.info(
        "extract.done",
        path=str(bars_csv),
        file_mb=round(size_mb, 2),
        **summary,
    )
    print(f"\nbars CSV: {bars_csv}  ({size_mb:.2f} MB)")
    print(f"  fetched={summary['tickers_fetched']}  no_data={summary['tickers_no_data']}  failed={summary['tickers_failed']}  rows_appended={summary['rows_appended']}")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
