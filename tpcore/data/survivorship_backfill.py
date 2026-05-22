"""Survivorship-bias backfill for ``platform.prices_daily``.

The corpus audit on 2026-05-22 (PR #281) found 18 of 20 known historical
delistings completely absent from ``platform.prices_daily``. Every
backtest credibility score is therefore biased toward survivors —
the autonomous Lab (task #25) cannot trust its DSR / n_trials scoring
until the survivorship gap is closed.

This module enumerates KNOWN delisted US-equity tickers across five
sources (existing corpus markers, historical-corpus orphans, an
operator-curated known-event manifest, the validation fixtures already
in-tree, and — when available at the operator's FMP Starter tier —
the ``/stable/symbol-change`` and ``/stable/delisted-companies``
endpoints), then per-ticker GETs FMP's
``/stable/historical-price-eod/full`` over each ticker's trading life
and upserts every bar into ``platform.prices_daily`` with
``delisted=true`` + ``delisting_date`` set to the ticker's final bar.

The orchestration is intentionally split from the existing
``tpcore.data.ingest_fmp_bars`` module — that module's
``fetch_daily_bars_multi`` is the per-ticker transport, reused as-is;
this module adds (a) universe enumeration, (b) per-ticker resumable
progress via ``application_log`` events, and (c) delisted-marker
upsert. No changes to the existing FMP adapter.

Wired into ``scripts/ops.py`` as two stages:

* ``historical_delisted_universe`` — one-shot operator backfill.
  Run once after PR merges to populate the corpus.
* ``daily_delisted_universe_check`` — nightly delta probe. Identifies
  tickers that were T1/T2 yesterday but have no bar for today; if FMP
  also has no bar, marks them ``delisted=true`` with today as the
  delisting date.

Per the stream-long-running-output rule: every per-ticker completion
emits a ``SURVIVORSHIP_BACKFILL_TICKER_DONE`` event to
``application_log`` so a crash mid-run keeps completed work — the
resume probe queries the log for tickers already done before kicking
off the next pass.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from tpcore.data.ingest_fmp_bars import (
    FMP_BASE_URL,
    _to_fmp_symbol,
    fetch_daily_bars_multi,
)
from tpcore.outage import DataProviderOutage

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Operator-curated known-delisting manifest
# ──────────────────────────────────────────────────────────────────────
#
# Operator-visible "the system knows about X" anchor list — keeps the
# critical known delistings explicit in source. The full universe
# enumeration is wider; this set is the floor the sentinel test pins
# against so a regression cannot silently shrink the survivorship
# coverage even if the corpus query goes empty.
#
# Each entry is (ticker, expected_last_trading_date_iso, note). The
# date is a guideline — the actual delisting_date written is whatever
# FMP returns as the symbol's final bar. ±5 trading days tolerance is
# the sentinel test rule.
#
# Sources: operator instructions; cross-checked against
# tpcore/quality/validation/fixtures/delistings.yaml (already in-tree)
# and public delisting / M&A press releases.
KNOWN_DELISTINGS: tuple[tuple[str, str, str], ...] = (
    # 2020 — pandemic-era bankruptcies
    # (HTZGQ, WLLBQ, LK removed per validation fixtures audit
    # 2026-05-10 — no free-tier bar coverage; will re-add only when
    # FMP Starter actually returns bars for these post-petition tickers)
    # 2021
    ("WORK",   "2021-07-21", "Slack — acquired by Salesforce (CRM)"),
    # 2022
    ("TWTR",   "2022-10-27", "Twitter — taken private by Elon Musk acquisition"),
    ("FB",     "2022-06-09", "Facebook — ticker change to META"),
    # 2023 — regional banking crisis
    ("SIVB",   "2023-03-10", "SVB Financial — FDIC takeover (post-bankruptcy SIVBQ)"),
    ("SBNY",   "2023-03-12", "Signature Bank — FDIC takeover"),
    ("FRC",    "2023-05-01", "First Republic Bank — FDIC takeover, sold to JPMorgan"),
    # 2023 — large-cap acquisitions
    ("ATVI",   "2023-10-13", "Activision Blizzard — acquired by Microsoft"),
    ("VMW",    "2023-11-22", "VMware — acquired by Broadcom"),
    # 2024 — large-cap acquisitions + bankruptcies
    ("SPLK",   "2024-03-18", "Splunk — acquired by Cisco"),
    ("FTCH",   "2023-12-18", "Farfetch — acquired by Coupang"),
    ("TUP",    "2024-09-17", "Tupperware Brands — Chapter 11"),
    # Earlier well-known acquisitions kept for coverage breadth
    ("ABMD",   "2022-12-22", "Abiomed — acquired by Johnson & Johnson"),
    ("ANSS",   "2024-09-25", "Ansys — pending acquisition by Synopsys (placeholder)"),
    ("ALXN",   "2021-07-21", "Alexion Pharmaceuticals — acquired by AstraZeneca"),
    ("CERN",   "2022-06-08", "Cerner — acquired by Oracle"),
    ("XLNX",   "2022-02-14", "Xilinx — acquired by AMD"),
    ("MGI",    "2023-06-21", "MoneyGram — taken private by Madison Dearborn"),
    ("FISV",   "2023-07-21", "Fiserv — ticker change to FI"),
    ("DISCA",  "2022-04-08", "Discovery — merger forming Warner Bros. Discovery (WBD)"),
    ("VIAC",   "2022-02-16", "ViacomCBS — ticker change to PARA (Paramount Global)"),
)
"""Operator-curated known delistings 2020-2024. The sentinel test pins
the corpus against this list; the universe enumeration is a superset."""


# ──────────────────────────────────────────────────────────────────────
# Universe enumeration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _DelistingCandidate:
    """One enumerated potentially-delisted ticker.

    ``source`` is the enumeration source that surfaced this candidate
    (corpus_marker / corpus_orphan / known_manifest / fixture /
    fmp_symbol_change / fmp_delisted_companies). The source is recorded
    in the per-ticker progress event so we can audit which source
    dominates the discovered universe.
    """

    ticker: str
    source: str
    # Optional hint — most sources don't carry a delisting date; we
    # infer the actual date from FMP's final bar at backfill time.
    hint_delisting_date: date | None = None


async def _enumerate_corpus_markers(pool: asyncpg.Pool) -> list[_DelistingCandidate]:
    """Tickers the corpus already flags ``delisted=true``.

    The audit found this set sparse, but it IS the canonical "things we
    already know about" floor. Including them in the universe ensures
    the backfill refreshes their full history rather than just trusting
    whatever partial coverage the previous (likely Alpaca-IEX) ingest
    produced.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE delisted = true
            ORDER BY ticker
            """
        )
    return [_DelistingCandidate(r["ticker"], "corpus_marker") for r in rows]


async def _enumerate_corpus_orphans(pool: asyncpg.Pool) -> list[_DelistingCandidate]:
    """Historical-corpus orphans — tickers that had pre-2020 bars but
    are NOT in today's ``platform.liquidity_tiers``.

    These are silent disappearances: a ticker that traded actively for
    a decade pre-2020 and then vanished is almost certainly delisted /
    merged / ticker-changed. We don't know the delisting date here;
    FMP's final-bar date carries the truth.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT pd.ticker
            FROM platform.prices_daily pd
            WHERE pd.date < DATE '2020-01-01'
              AND pd.delisted = false
              AND NOT EXISTS (
                  SELECT 1 FROM platform.liquidity_tiers lt
                  WHERE lt.ticker = pd.ticker
              )
            ORDER BY pd.ticker
            """
        )
    return [_DelistingCandidate(r["ticker"], "corpus_orphan") for r in rows]


def _enumerate_known_manifest() -> list[_DelistingCandidate]:
    """The operator-curated KNOWN_DELISTINGS anchor list."""
    out: list[_DelistingCandidate] = []
    for ticker, hint_iso, _note in KNOWN_DELISTINGS:
        try:
            hint = date.fromisoformat(hint_iso)
        except ValueError:
            hint = None
        out.append(_DelistingCandidate(ticker, "known_manifest", hint))
    return out


def _enumerate_fixture_delistings() -> list[_DelistingCandidate]:
    """Tickers already in ``tpcore/quality/validation/fixtures/delistings.yaml``.

    Best-effort: if the fixture isn't readable (e.g. relocated, missing
    pyyaml), we return an empty list and rely on the other sources. The
    fixture is the validation-suite anchor so any drift is caught there.
    """
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "tpcore" / "quality" / "validation" / "fixtures" / "delistings.yaml"
    )
    if not fixture_path.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # pyyaml is in the dev deps but defend against a stripped env.
        return []
    try:
        entries = yaml.safe_load(fixture_path.read_text()) or []
    except Exception:
        return []
    out: list[_DelistingCandidate] = []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        if not isinstance(ticker, str):
            continue
        hint_raw = entry.get("delisting_date")
        hint: date | None = None
        if isinstance(hint_raw, date):
            hint = hint_raw
        elif isinstance(hint_raw, str):
            try:
                hint = date.fromisoformat(hint_raw)
            except ValueError:
                hint = None
        out.append(_DelistingCandidate(ticker, "fixture", hint))
        # alt_tickers ride the same enumeration — every observed
        # historical spelling is worth a backfill attempt.
        for alt in entry.get("alt_tickers") or []:
            if isinstance(alt, str):
                out.append(_DelistingCandidate(alt, "fixture", hint))
    return out


async def _probe_fmp_symbol_change(
    client: httpx.AsyncClient,
) -> list[_DelistingCandidate]:
    """Best-effort probe of FMP's ``/stable/symbol-change`` endpoint.

    This endpoint may be gated at the Starter tier — operator's
    instructions explicitly say "probe; if 401-gated, skip this source".
    The probe sends one HEAD-equivalent GET; on any non-200 we return
    an empty list rather than crashing the enumeration. This keeps the
    pipeline resilient to tier-dependent endpoint availability.
    """
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        return []
    url = f"{FMP_BASE_URL}/symbol-change"
    try:
        resp = await client.get(url, params={"apikey": api_key}, timeout=15.0)
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        logger.info(
            "survivorship.fmp_symbol_change.unavailable",
            status=resp.status_code,
        )
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    if not isinstance(body, list):
        return []
    out: list[_DelistingCandidate] = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        # FMP's symbol-change response uses oldSymbol → newSymbol; the
        # OLD symbol is what's missing from our corpus (it stopped
        # trading under that name).
        old = entry.get("oldSymbol") or entry.get("old_symbol")
        if isinstance(old, str) and old:
            hint = None
            for key in ("date", "changeDate", "effectiveDate"):
                raw = entry.get(key)
                if isinstance(raw, str):
                    try:
                        hint = date.fromisoformat(raw[:10])
                        break
                    except ValueError:
                        continue
            out.append(_DelistingCandidate(old.upper(), "fmp_symbol_change", hint))
    return out


async def _probe_fmp_delisted_companies(
    client: httpx.AsyncClient,
) -> list[_DelistingCandidate]:
    """Best-effort probe of FMP's ``/stable/delisted-companies`` endpoint.

    Same gating rule as ``_probe_fmp_symbol_change`` — non-200 means
    "skip silently". Where available, this is the canonical, vendor-
    maintained delisted-symbol roster.
    """
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        return []
    url = f"{FMP_BASE_URL}/delisted-companies"
    try:
        resp = await client.get(url, params={"apikey": api_key}, timeout=15.0)
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        logger.info(
            "survivorship.fmp_delisted_companies.unavailable",
            status=resp.status_code,
        )
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    if not isinstance(body, list):
        return []
    out: list[_DelistingCandidate] = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        hint = None
        for key in ("delistedDate", "delisted_date", "date"):
            raw = entry.get(key)
            if isinstance(raw, str):
                try:
                    hint = date.fromisoformat(raw[:10])
                    break
                except ValueError:
                    continue
        out.append(_DelistingCandidate(symbol.upper(), "fmp_delisted_companies", hint))
    return out


async def enumerate_delisted_universe(
    pool: asyncpg.Pool,
    *,
    probe_fmp: bool = True,
) -> list[_DelistingCandidate]:
    """Combine every enumeration source into a deduplicated universe.

    Dedup keeps the EARLIEST source that surfaced a ticker (corpus
    markers first, then orphans, then the curated manifest, then
    fixtures, then FMP probes) so the source attribution in the
    progress events tells us which path was load-bearing.

    ``probe_fmp=False`` skips the two external FMP endpoint probes —
    use this in unit tests to avoid live network calls. The default
    True is correct for the ops stage.
    """
    candidates: list[_DelistingCandidate] = []
    candidates.extend(await _enumerate_corpus_markers(pool))
    candidates.extend(await _enumerate_corpus_orphans(pool))
    candidates.extend(_enumerate_known_manifest())
    candidates.extend(_enumerate_fixture_delistings())

    if probe_fmp:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                candidates.extend(await _probe_fmp_symbol_change(client))
                candidates.extend(await _probe_fmp_delisted_companies(client))
        except httpx.HTTPError as exc:
            logger.warning("survivorship.fmp_probe_failed", error=str(exc)[:200])

    # Dedup: first-seen wins. Skip empty / single-char tickers (FMP
    # occasionally returns those as noise on the symbol-change feed).
    seen: dict[str, _DelistingCandidate] = {}
    for c in candidates:
        sym = c.ticker.strip().upper()
        if len(sym) < 1 or len(sym) > 8:
            continue
        if sym in seen:
            continue
        seen[sym] = _DelistingCandidate(sym, c.source, c.hint_delisting_date)
    out = sorted(seen.values(), key=lambda x: x.ticker)
    logger.info(
        "survivorship.universe_enumerated",
        total=len(out),
        sources={
            src: sum(1 for c in out if c.source == src)
            for src in {c.source for c in out}
        },
    )
    return out


# ──────────────────────────────────────────────────────────────────────
# Resumability — read prior-run ticker completion from application_log
# ──────────────────────────────────────────────────────────────────────


PROGRESS_EVENT_TYPE = "SURVIVORSHIP_BACKFILL_TICKER_DONE"
"""Per-ticker completion event. ``data->>'ticker'`` carries the symbol;
``data->>'bars_written'`` the per-ticker row count. The resume probe
selects DISTINCT ticker from the past N days and skips those tickers
on the next run so a crash mid-backfill doesn't lose completed work."""


async def already_completed_tickers(
    pool: asyncpg.Pool, *, lookback_days: int = 30,
) -> set[str]:
    """Return tickers already marked done in the last N days.

    The 30-day default is far longer than any backfill run; it's there
    so an interrupted multi-day operator workflow resumes correctly.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT data->>'ticker' AS ticker
            FROM platform.application_log
            WHERE event_type = $1
              AND recorded_at >= now() - ($2::int * INTERVAL '1 day')
            """,
            PROGRESS_EVENT_TYPE,
            lookback_days,
        )
    return {r["ticker"] for r in rows if r["ticker"]}


# ──────────────────────────────────────────────────────────────────────
# Per-ticker backfill — FMP fetch + delisted-marker upsert
# ──────────────────────────────────────────────────────────────────────


_DEFAULT_BACKFILL_START = date(2010, 1, 1)
"""FMP Starter typically has 15 years of EOD history; 2010 captures
the post-GFC era which is what every backtest in this codebase covers."""


def _upsert_sql() -> str:
    """The delisted-bar upsert SQL.

    Identical column ordering to ``tpcore.data.ingest_alpaca_bars._upsert_bars``
    so the existing physical-truth invariants on ``prices_daily``
    remain in force, with the single material difference that we hard-
    code ``delisted=true`` and set ``delisting_date`` to the final-bar
    date passed in. ``source = 'fmp'`` to keep the provenance audit
    trail honest.
    """
    return """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $9, 'fmp')
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = true,
            delisting_date = EXCLUDED.delisting_date,
            source = 'fmp'
    """


def _physical_truth_rows(
    symbol: str,
    bars: list[dict[str, Any]],
    delisting_date: date,
) -> list[tuple]:
    """Translate FMP-shape bars to upsert rows, rejecting bad bars.

    Mirrors the gate in ``tpcore.data.ingest_alpaca_bars._upsert_bars``
    — close > 0, OHLC consistent, volume >= 0, no future dates. Bad
    rows are dropped (not zero-filled) so the corpus stays clean.
    """
    today = datetime.now(UTC).date()
    out: list[tuple] = []
    for b in bars:
        try:
            ts = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        session_date = ts.date()
        o = b.get("o")
        h = b.get("h")
        low = b.get("l")
        c = b.get("c")
        v = b.get("v")
        if o is None or h is None or low is None or c is None or v is None:
            continue
        try:
            of, hf, lf, cf = float(o), float(h), float(low), float(c)
        except (TypeError, ValueError):
            continue
        if cf <= 0 or cf > 1e8 or of <= 0 or hf <= 0 or lf <= 0:
            continue
        if hf < max(of, cf, lf) or lf > min(of, cf, hf):
            continue
        if session_date > today:
            continue
        out.append((
            symbol, session_date, of, hf, lf, cf, int(v),
            cf,  # adjusted_close — FMP returns adjusted close in /full
            delisting_date,
        ))
    return out


async def backfill_one_ticker(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    *,
    start: date = _DEFAULT_BACKFILL_START,
    end: date | None = None,
) -> int:
    """Fetch FMP history for one symbol, upsert as delisted bars.

    Writes a ``SURVIVORSHIP_BACKFILL_TICKER_DONE`` event on every
    successful per-ticker call so the resume probe sees the work. A
    permanent FMP failure (DataProviderOutage) propagates — the
    stage-level catch logs it and continues to the next ticker per the
    stream-long-running-output rule.

    Returns the number of bars written (0 if FMP has no data for the
    symbol — common for very-recent IPOs that subsequently delisted
    inside the same year).
    """
    end_date = end or datetime.now(UTC).date()
    bars_by_symbol = await fetch_daily_bars_multi(
        client, [symbol], start, end_date,
    )
    bars = bars_by_symbol.get(symbol, [])
    if not bars:
        # No FMP data for this symbol — emit the progress event with
        # bars_written=0 so the resume probe doesn't re-fetch on the
        # next run (a re-fetch will get the same empty result).
        await db_log.log(
            PROGRESS_EVENT_TYPE,
            f"survivorship backfill: {symbol} returned 0 bars from FMP",
            severity="INFO",
            data={"ticker": symbol, "bars_written": 0, "fmp_symbol": _to_fmp_symbol(symbol)},
        )
        return 0
    # Final FMP bar IS the delisting date — FMP's /historical-price-eod
    # response naturally truncates at the last trading day.
    final_iso = str(bars[-1].get("t", ""))
    try:
        delisting_date = datetime.fromisoformat(
            final_iso.replace("Z", "+00:00"),
        ).date()
    except ValueError:
        delisting_date = end_date
    rows = _physical_truth_rows(symbol, bars, delisting_date)
    if not rows:
        await db_log.log(
            PROGRESS_EVENT_TYPE,
            f"survivorship backfill: {symbol} had bars but all rejected by physical-truth gate",
            severity="WARNING",
            data={"ticker": symbol, "bars_written": 0, "fmp_bars": len(bars)},
        )
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(_upsert_sql(), rows)
    await db_log.log(
        PROGRESS_EVENT_TYPE,
        f"survivorship backfill: {symbol} ← {len(rows)} bars (delisted={delisting_date})",
        severity="INFO",
        data={
            "ticker": symbol,
            "bars_written": len(rows),
            "delisting_date": delisting_date.isoformat(),
        },
    )
    return len(rows)


async def backfill_universe(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    universe: list[str],
    *,
    start: date = _DEFAULT_BACKFILL_START,
    end: date | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Backfill every ticker in ``universe``.

    Resumable by default — queries ``application_log`` for tickers
    already completed in the past 30 days and skips them. Per-ticker
    permanent failures are logged and the run continues; the final
    return dict carries the per-source counters and the failure list.
    """
    if resume:
        done = await already_completed_tickers(pool)
        pending = [t for t in universe if t not in done]
        skipped = len(universe) - len(pending)
    else:
        pending = list(universe)
        skipped = 0
    total_bars = 0
    failures: list[str] = []
    succeeded: list[str] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for symbol in pending:
            try:
                bars = await backfill_one_ticker(
                    pool, client, db_log, symbol,
                    start=start, end=end,
                )
            except DataProviderOutage as exc:
                logger.error(
                    "survivorship.ticker_outage",
                    ticker=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:outage")
                continue
            except Exception as exc:  # noqa: BLE001 — keep the run moving
                logger.error(
                    "survivorship.ticker_failed",
                    ticker=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            total_bars += bars
            succeeded.append(symbol)
    return {
        "universe_size": len(universe),
        "resumed_skipped": skipped,
        "tickers_attempted": len(pending),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failures),
        "bars_written": total_bars,
        "failures_sample": failures[:20],
    }


# ──────────────────────────────────────────────────────────────────────
# Newly-delisted nightly check — detect T1/T2 → silent disappearance
# ──────────────────────────────────────────────────────────────────────


async def detect_newly_delisted(
    pool: asyncpg.Pool, *, yesterday: date | None = None,
) -> list[str]:
    """Return tickers that had a bar 5+ trading days ago but missed
    every session since.

    Conservative: 5 sessions of absence is the floor — a single missed
    session is far more likely a vendor glitch than a delisting. The
    canonical recovery path for a single-session vendor miss is
    ``daily_bars --param repair_coverage=true``, not a delisting mark.

    Returns the ticker list. Caller decides whether to mark them
    delisted (the nightly ops stage probes FMP one more time before
    writing the marker).
    """
    yest = yesterday or (datetime.now(UTC).date() - timedelta(days=1))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH last_bar AS (
                SELECT pd.ticker, MAX(pd.date) AS last_date
                FROM platform.prices_daily pd
                WHERE pd.delisted = false
                GROUP BY pd.ticker
            )
            SELECT lb.ticker
            FROM last_bar lb
            JOIN platform.liquidity_tiers lt ON lt.ticker = lb.ticker
            WHERE lb.last_date < $1::date - 5
              AND lt.tier IN (1, 2)
            ORDER BY lb.ticker
            """,
            yest,
        )
    return [r["ticker"] for r in rows]


async def mark_delisted(
    pool: asyncpg.Pool, ticker: str, delisting_date: date,
) -> bool:
    """Promote a ticker to ``delisted=true`` with the given date.

    Returns True if any row was updated. Idempotent — re-running with
    the same date is a no-op.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE platform.prices_daily
               SET delisted = true,
                   delisting_date = $2
             WHERE ticker = $1
               AND (delisted = false OR delisting_date IS NULL)
            """,
            ticker, delisting_date,
        )
    # asyncpg returns "UPDATE N" — strip to int.
    try:
        n = int(result.split()[-1])
    except (ValueError, IndexError):
        n = 0
    return n > 0


__all__ = [
    "KNOWN_DELISTINGS",
    "PROGRESS_EVENT_TYPE",
    "already_completed_tickers",
    "backfill_one_ticker",
    "backfill_universe",
    "detect_newly_delisted",
    "enumerate_delisted_universe",
    "mark_delisted",
]
