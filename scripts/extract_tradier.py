"""One-shot Tradier extractor — CSV output only, no DB writes.

Pulls four buckets of data from the Tradier production API and lands
them as CSV files in ``data/tradier_export/`` (or ``--out-dir``):

    Tier 1  options chains          (highest value, most unique)
    Tier 1  corporate calendars
    Tier 2  pre-2020 daily bars     (extends Alpaca's 2020-07+ history)
    Tier 3  company profiles
    Tier 3  current quote snapshot

Each tier's writer streams rows incrementally so a crash mid-run leaves
partial-but-valid CSVs. After every step we re-tally the directory's
total size; once it crosses ``--max-size-mb`` (default 250) the run
stops and writes a manifest from whatever's on disk.

The script never touches Postgres. The user uploads selected CSVs by
hand once Tradier is closed.

Run::

    TRADIER_PRODUCTION_TOKEN=... python scripts/extract_tradier.py
    python scripts/extract_tradier.py --max-size-mb 100
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

TRADIER_BASE = "https://api.tradier.com"

# Universe used by Sigma + Reversion + Vector. Bars for these names extend
# Alpaca's 2020-07 history backwards.
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

# Liquid optionable subset Tradier always returns chains for. Listed in priority
# order — we fan out to the rest of DEFAULT_UNIVERSE after these are captured
# (subject to the size budget).
OPTIONS_PRIORITY: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT",
)

# Defensive throttle — Tradier production allows 120 req/min; we sleep 0.6s
# between requests to leave headroom for retry/backoff.
INTER_REQUEST_SLEEP_S = 0.6

# Bar-history boundary. Alpaca data starts ~2020-07; Tradier is asked for
# everything strictly before this date.
BARS_END_DATE = date(2019, 12, 31)
BARS_START_DATE = date(1990, 1, 1)

logger = logging.getLogger("scripts.extract_tradier")


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _retrieved_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return sum(p.stat().st_size for p in path.iterdir() if p.is_file()) / 1_000_000


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / 1_000_000 if path.exists() else 0.0


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | None:
    """GET path with retry/backoff on transient failures. Returns parsed JSON or None."""
    try:
        resp = await client.get(path, params=params or {})
    except (httpx.RequestError, httpx.HTTPError) as exc:
        logger.warning("tradier.network_error path=%s params=%s err=%s", path, params, exc)
        return None
    if resp.status_code == 429:
        # Rate-limited — sleep extra and try once more.
        logger.warning("tradier.rate_limited path=%s sleeping 5s", path)
        await asyncio.sleep(5.0)
        try:
            resp = await client.get(path, params=params or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("tradier.retry_failed path=%s err=%s", path, exc)
            return None
    if resp.status_code != 200:
        logger.warning(
            "tradier.http_error path=%s params=%s status=%s body=%s",
            path, params, resp.status_code, resp.text[:200],
        )
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("tradier.non_json_response path=%s body=%s", path, resp.text[:200])
        return None


def _ensure_list(x: Any) -> list:
    """Tradier returns a single dict (not a list) when only one element comes back."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


class _CsvSink:
    """Append-mode CSV writer with deferred header. Closes itself in __exit__."""

    def __init__(self, path: Path, columns: list[str]) -> None:
        self.path = path
        self.columns = columns
        self._fh = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh, quoting=csv.QUOTE_MINIMAL)
        self._writer.writerow(columns)
        self.rows = 0

    def write(self, row: Iterable[Any]) -> None:
        self._writer.writerow([_csv_cell(v) for v in row])
        self.rows += 1

    def close(self) -> None:
        self._fh.close()


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # Avoid scientific notation for typical price decimals; let Python's
        # default repr handle weird floats.
        if abs(v) < 1e15:
            return f"{v:.6f}".rstrip("0").rstrip(".") if "." in f"{v:.6f}" else str(int(v))
    return str(v)


# ────────────────────────────────────────────────────────────────────────────
# Tier 1a — Options chains
# ────────────────────────────────────────────────────────────────────────────


OPTIONS_COLUMNS = [
    "ticker", "expiration_date", "strike", "option_type",
    "bid", "ask", "last", "volume", "open_interest", "retrieved_at",
]


async def _expirations(client: httpx.AsyncClient, ticker: str) -> list[str]:
    body = await _get(
        client,
        "/v1/markets/options/expirations",
        {"symbol": ticker, "includeAllRoots": "true"},
    )
    if not body:
        return []
    dates = (body.get("expirations") or {}).get("date") or []
    return _ensure_list(dates)


async def _chain_for_expiration(
    client: httpx.AsyncClient, ticker: str, expiration: str
) -> list[dict]:
    body = await _get(
        client,
        "/v1/markets/options/chains",
        {"symbol": ticker, "expiration": expiration},
    )
    if not body:
        return []
    options = (body.get("options") or {}).get("option") or []
    return _ensure_list(options)


async def extract_options(
    client: httpx.AsyncClient,
    out_dir: Path,
    *,
    priority_tickers: tuple[str, ...],
    fallback_tickers: tuple[str, ...],
    options_size_cap_mb: float,
    overall_size_cap_mb: float,
) -> dict:
    """Stream options chains into ``tradier_options.csv``.

    Returns a dict summary: ``{tickers_captured, expirations, rows, failed}``.
    Stops adding new tickers once the file exceeds ``options_size_cap_mb``;
    in-progress tickers are still finished. Stops the whole tier early if
    ``overall_size_cap_mb`` is hit.
    """
    out = out_dir / "tradier_options.csv"
    sink = _CsvSink(out, OPTIONS_COLUMNS)
    summary = {
        "tickers_captured": [],
        "expirations": 0,
        "rows": 0,
        "failed": [],
    }
    try:
        # Priority first; then any remaining names from the fallback list (de-duped).
        priority_set = set(priority_tickers)
        ordered = list(priority_tickers) + [t for t in fallback_tickers if t not in priority_set]

        new_tickers_paused = False
        for i, ticker in enumerate(ordered, 1):
            if _dir_size_mb(out_dir) >= overall_size_cap_mb:
                logger.warning("options.tier_size_cap_hit ticker=%s halting tier", ticker)
                break
            if _file_size_mb(out) >= options_size_cap_mb and not new_tickers_paused:
                logger.warning(
                    "options.options_cap_hit at %.1f MB — pausing new-ticker scan",
                    _file_size_mb(out),
                )
                new_tickers_paused = True
            if new_tickers_paused:
                # Skip new tickers, allow continuation work — but we never started one
                # mid-loop, so this just means stop the whole tier here.
                break

            expirations = await _expirations(client, ticker)
            await asyncio.sleep(INTER_REQUEST_SLEEP_S)
            if not expirations:
                summary["failed"].append((ticker, "no_expirations_or_not_optionable"))
                logger.info("options.skipped ticker=%s reason=no_expirations", ticker)
                continue

            ticker_rows = 0
            for exp in expirations:
                options = await _chain_for_expiration(client, ticker, exp)
                await asyncio.sleep(INTER_REQUEST_SLEEP_S)
                for o in options:
                    sink.write([
                        ticker,
                        o.get("expiration_date"),
                        o.get("strike"),
                        (o.get("option_type") or "").upper(),
                        o.get("bid"),
                        o.get("ask"),
                        o.get("last"),
                        o.get("volume"),
                        o.get("open_interest"),
                        _retrieved_at(),
                    ])
                    ticker_rows += 1
                summary["expirations"] += 1

            summary["tickers_captured"].append(ticker)
            summary["rows"] += ticker_rows
            logger.info(
                "options.ticker_done [%d/%d] %s expirations=%d rows=%d file=%.2f MB",
                i, len(ordered), ticker, len(expirations), ticker_rows, _file_size_mb(out),
            )
    finally:
        sink.close()
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Tier 1b — Corporate calendars (earnings, dividends, splits)
# ────────────────────────────────────────────────────────────────────────────


CALENDARS_COLUMNS = [
    "ticker", "event_date", "event_type", "description", "retrieved_at",
]


async def extract_calendars(
    client: httpx.AsyncClient,
    out_dir: Path,
    tickers: tuple[str, ...],
) -> dict:
    """Pull corporate calendars in batches — small, simple, low-risk."""
    out = out_dir / "tradier_calendars.csv"
    sink = _CsvSink(out, CALENDARS_COLUMNS)
    summary = {"tickers_with_events": 0, "rows": 0, "failed": []}
    try:
        # Batch up to 25 symbols per call (Tradier's beta endpoint accepts many).
        for batch in _chunks(tickers, 25):
            body = await _get(
                client,
                "/v1/beta/markets/fundamentals/calendars",
                {"symbols": ",".join(batch)},
            )
            await asyncio.sleep(INTER_REQUEST_SLEEP_S)
            if not body:
                summary["failed"].extend((t, "fetch_failed") for t in batch)
                continue
            for entry in _ensure_list(body):
                ticker = entry.get("request") or entry.get("symbol")
                results = _ensure_list(entry.get("results") or [])
                wrote_for_ticker = False
                for r in results:
                    tables = r.get("tables") or {}
                    events = _ensure_list(tables.get("corporate_calendars"))
                    for ev in events:
                        sink.write([
                            ticker,
                            ev.get("begin_date_time", "")[:10] or ev.get("date", ""),
                            ev.get("event_type") or ev.get("event_status") or "",
                            ev.get("event") or "",
                            _retrieved_at(),
                        ])
                        summary["rows"] += 1
                        wrote_for_ticker = True
                if wrote_for_ticker:
                    summary["tickers_with_events"] += 1
    finally:
        sink.close()
    logger.info(
        "calendars.done rows=%d tickers_with_events=%d failed=%d",
        summary["rows"], summary["tickers_with_events"], len(summary["failed"]),
    )
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Tier 2a — Pre-2020 daily bars
# ────────────────────────────────────────────────────────────────────────────


BARS_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume"]


async def extract_bars(
    client: httpx.AsyncClient,
    out_dir: Path,
    tickers: tuple[str, ...],
    *,
    bars_size_cap_mb: float,
    overall_size_cap_mb: float,
) -> dict:
    """Daily bars for ``tickers`` from ``BARS_START_DATE`` to ``BARS_END_DATE``.

    This is *purely* the pre-Alpaca slice — Alpaca free-tier IEX data already
    covers 2020-07 forward, so we don't waste rows on duplicate history.
    """
    out = out_dir / "tradier_bars.csv"
    sink = _CsvSink(out, BARS_COLUMNS)
    summary = {"tickers_captured": [], "rows": 0, "failed": []}
    try:
        for i, ticker in enumerate(tickers, 1):
            if _dir_size_mb(out_dir) >= overall_size_cap_mb:
                logger.warning("bars.tier_size_cap_hit at %s — halting tier", ticker)
                break
            if _file_size_mb(out) >= bars_size_cap_mb:
                logger.warning(
                    "bars.bars_cap_hit at %.1f MB — halting bars extraction",
                    _file_size_mb(out),
                )
                break
            body = await _get(
                client,
                "/v1/markets/history",
                {
                    "symbol": ticker,
                    "interval": "daily",
                    "start": BARS_START_DATE.isoformat(),
                    "end": BARS_END_DATE.isoformat(),
                },
            )
            await asyncio.sleep(INTER_REQUEST_SLEEP_S)
            if not body:
                summary["failed"].append((ticker, "fetch_failed"))
                continue
            days = (body.get("history") or {}).get("day") or []
            days = _ensure_list(days)
            if not days:
                summary["failed"].append((ticker, "no_days"))
                logger.info("bars.skipped ticker=%s reason=no_pre_2020_data", ticker)
                continue
            for d in days:
                sink.write([
                    ticker,
                    d.get("date"),
                    d.get("open"),
                    d.get("high"),
                    d.get("low"),
                    d.get("close"),
                    d.get("volume"),
                ])
                summary["rows"] += 1
            summary["tickers_captured"].append(ticker)
            logger.info(
                "bars.ticker_done [%d/%d] %s rows=%d span=%s..%s file=%.2f MB",
                i, len(tickers), ticker, len(days),
                days[0].get("date"), days[-1].get("date"), _file_size_mb(out),
            )
    finally:
        sink.close()
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Tier 3a — Company profiles
# ────────────────────────────────────────────────────────────────────────────


PROFILES_COLUMNS = [
    "ticker", "company_name", "sector", "industry",
    "market_cap", "employees", "description", "retrieved_at",
]


async def extract_profiles(
    client: httpx.AsyncClient,
    out_dir: Path,
    tickers: tuple[str, ...],
) -> dict:
    out = out_dir / "tradier_profiles.csv"
    sink = _CsvSink(out, PROFILES_COLUMNS)
    summary = {"rows": 0, "failed": []}
    try:
        for batch in _chunks(tickers, 25):
            body = await _get(
                client,
                "/v1/beta/markets/fundamentals/company",
                {"symbols": ",".join(batch)},
            )
            await asyncio.sleep(INTER_REQUEST_SLEEP_S)
            if not body:
                summary["failed"].extend((t, "fetch_failed") for t in batch)
                continue
            for entry in _ensure_list(body):
                ticker = entry.get("request") or entry.get("symbol")
                results = _ensure_list(entry.get("results") or [])
                for r in results:
                    tables = r.get("tables") or {}
                    profiles = _ensure_list(tables.get("company_profile"))
                    asset_class = _ensure_list(tables.get("asset_classification"))
                    overview = profiles[0] if profiles else {}
                    classification = asset_class[0] if asset_class else {}
                    sink.write([
                        ticker,
                        overview.get("company_name") or overview.get("name"),
                        classification.get("sector") or overview.get("sector"),
                        classification.get("industry") or overview.get("industry"),
                        overview.get("market_cap"),
                        overview.get("total_employee_number") or overview.get("employees"),
                        (overview.get("business_description") or overview.get("description") or "").replace("\n", " "),
                        _retrieved_at(),
                    ])
                    summary["rows"] += 1
    finally:
        sink.close()
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Tier 3b — Current quote snapshot
# ────────────────────────────────────────────────────────────────────────────


QUOTES_COLUMNS = ["ticker", "bid", "ask", "last", "retrieved_at"]


async def extract_quotes(
    client: httpx.AsyncClient,
    out_dir: Path,
    tickers: tuple[str, ...],
) -> dict:
    out = out_dir / "tradier_quotes.csv"
    sink = _CsvSink(out, QUOTES_COLUMNS)
    summary = {"rows": 0, "failed": []}
    try:
        for batch in _chunks(tickers, 50):
            body = await _get(
                client,
                "/v1/markets/quotes",
                {"symbols": ",".join(batch)},
            )
            await asyncio.sleep(INTER_REQUEST_SLEEP_S)
            if not body:
                summary["failed"].extend((t, "fetch_failed") for t in batch)
                continue
            quotes = _ensure_list((body.get("quotes") or {}).get("quote") or [])
            ts = _retrieved_at()
            for q in quotes:
                sink.write([
                    q.get("symbol"),
                    q.get("bid"),
                    q.get("ask"),
                    q.get("last"),
                    ts,
                ])
                summary["rows"] += 1
    finally:
        sink.close()
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Manifest
# ────────────────────────────────────────────────────────────────────────────


def write_manifest(out_dir: Path, summaries: dict[str, dict]) -> Path:
    """Write a human-readable summary + upload recommendation."""
    manifest = out_dir / "tradier_manifest.txt"
    lines: list[str] = []
    lines.append(f"Tradier extraction manifest — generated {_retrieved_at()}\n")
    lines.append("=" * 72)
    lines.append("")
    total_size = 0.0
    for fname in sorted(out_dir.iterdir()):
        if fname.suffix != ".csv":
            continue
        size_mb = _file_size_mb(fname)
        total_size += size_mb
        # Quick row count via line count (header is row 1 of the file).
        with fname.open() as f:
            row_count = max(0, sum(1 for _ in f) - 1)
        lines.append(f"  {fname.name:32s}  {size_mb:7.2f} MB   {row_count:>10,d} data rows")
    lines.append("")
    lines.append(f"  TOTAL                           {total_size:7.2f} MB")
    lines.append("")
    lines.append("Per-tier details")
    lines.append("-" * 72)
    for label, summary in summaries.items():
        lines.append(f"  {label}:")
        for k, v in summary.items():
            if isinstance(v, list):
                lines.append(f"    {k}: {len(v)} entries")
                if k == "failed" and v:
                    for t, reason in v[:5]:
                        lines.append(f"      - {t}: {reason}")
                    if len(v) > 5:
                        lines.append(f"      … {len(v)-5} more")
            else:
                lines.append(f"    {k}: {v}")
        lines.append("")
    lines.append("Recommended upload order (tightest budget first)")
    lines.append("-" * 72)
    lines.append("  1. tradier_options.csv     — unique data, no Alpaca equivalent (S2 engine input)")
    lines.append("  2. tradier_calendars.csv   — small, cross-checks FMP earnings dates")
    lines.append("  3. tradier_bars.csv        — extends Alpaca's 2020-07 boundary backwards")
    lines.append("  4. tradier_profiles.csv    — convenience metadata; small, low priority")
    lines.append("  5. tradier_quotes.csv      — point-in-time IEX cross-check; only useful while fresh")
    lines.append("")
    manifest.write_text("\n".join(lines))
    return manifest


# ────────────────────────────────────────────────────────────────────────────
# Glue
# ────────────────────────────────────────────────────────────────────────────


def _chunks(seq: Iterable, n: int) -> Iterable[tuple]:
    chunk: list = []
    for item in seq:
        chunk.append(item)
        if len(chunk) >= n:
            yield tuple(chunk)
            chunk = []
    if chunk:
        yield tuple(chunk)


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.getenv("TRADIER_PRODUCTION_TOKEN")
    if not token:
        print("TRADIER_PRODUCTION_TOKEN not set in environment", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    summaries: dict[str, dict] = {}

    async with httpx.AsyncClient(
        base_url=TRADIER_BASE,
        headers=headers,
        timeout=30.0,
    ) as client:
        # ── Tier 1a ──────────────────────────────────────────────────────
        logger.info("tier1a.options.start")
        summaries["Tier 1a — options"] = await extract_options(
            client,
            out_dir,
            priority_tickers=OPTIONS_PRIORITY,
            fallback_tickers=DEFAULT_UNIVERSE,
            options_size_cap_mb=args.options_cap_mb,
            overall_size_cap_mb=args.max_size_mb,
        )
        _print_size_status(out_dir, args.max_size_mb, "Tier 1a complete")
        if _dir_size_mb(out_dir) >= args.max_size_mb:
            logger.warning("max_size_cap_hit — skipping remaining tiers")
            write_manifest(out_dir, summaries)
            return 0

        # ── Tier 1b ──────────────────────────────────────────────────────
        logger.info("tier1b.calendars.start")
        summaries["Tier 1b — calendars"] = await extract_calendars(
            client, out_dir, DEFAULT_UNIVERSE
        )
        _print_size_status(out_dir, args.max_size_mb, "Tier 1b complete")
        if _dir_size_mb(out_dir) >= args.max_size_mb:
            logger.warning("max_size_cap_hit — skipping remaining tiers")
            write_manifest(out_dir, summaries)
            return 0

        # ── Tier 2a ──────────────────────────────────────────────────────
        logger.info("tier2a.bars.start")
        summaries["Tier 2a — bars (pre-2020)"] = await extract_bars(
            client,
            out_dir,
            DEFAULT_UNIVERSE,
            bars_size_cap_mb=args.bars_cap_mb,
            overall_size_cap_mb=args.max_size_mb,
        )
        _print_size_status(out_dir, args.max_size_mb, "Tier 2a complete")
        if _dir_size_mb(out_dir) >= args.max_size_mb:
            logger.warning("max_size_cap_hit — skipping remaining tiers")
            write_manifest(out_dir, summaries)
            return 0

        # ── Tier 3a ──────────────────────────────────────────────────────
        logger.info("tier3a.profiles.start")
        summaries["Tier 3a — profiles"] = await extract_profiles(
            client, out_dir, DEFAULT_UNIVERSE
        )
        _print_size_status(out_dir, args.max_size_mb, "Tier 3a complete")

        # ── Tier 3b ──────────────────────────────────────────────────────
        logger.info("tier3b.quotes.start")
        summaries["Tier 3b — quotes snapshot"] = await extract_quotes(
            client, out_dir, DEFAULT_UNIVERSE
        )

    # Final manifest
    manifest = write_manifest(out_dir, summaries)
    print(f"\nManifest → {manifest}")
    print(manifest.read_text())
    return 0


def _print_size_status(out_dir: Path, max_size_mb: float, label: str) -> None:
    size = _dir_size_mb(out_dir)
    headroom = max_size_mb - size
    files = sorted(p.name for p in out_dir.iterdir() if p.suffix == ".csv")
    print(f"\n[{label}] dir={size:.2f} MB / cap={max_size_mb:.0f} MB / headroom={headroom:.2f} MB")
    for fname in files:
        print(f"  - {fname}: {_file_size_mb(out_dir/fname):.2f} MB")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out-dir", default="data/tradier_export")
    p.add_argument("--max-size-mb", type=float, default=250.0,
                   help="Hard ceiling — extraction stops when the directory hits this size.")
    p.add_argument("--options-cap-mb", type=float, default=150.0,
                   help="Soft cap for options chains. New tickers stop at this size; in-progress finishes.")
    p.add_argument("--bars-cap-mb", type=float, default=100.0,
                   help="Soft cap for bars file. Stops at this size.")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
