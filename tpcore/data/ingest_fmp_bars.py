"""FMP daily-bars adapter — primary daily OHLCV ingest path (2026-05-22+).

Replaces Alpaca as the primary data source for ``handle_daily_bars``.
FMP returns the full CTA consolidated tape (e.g. AAPL volume ~42M vs
Alpaca IEX's ~1M for the same session) on the operator's $200/year
Starter tier, equivalent to what Alpaca SIP would provide if we paid
for it.

Endpoint: ``GET /stable/historical-price-eod/full?symbol=<S>&from=<d>&to=<d>``.
Per-ticker only — operator's tier has **no batch/bulk** EOD endpoint
(``/batch-eod-historical-price`` and friends return 404/401). Per-call
throughput at 200ms inter-request sleep ≈ 5 req/s ≈ 300 req/min, well
under the Starter 300 req/min ceiling. Full ~7,600-ticker universe
backfill ≈ 25 min wall time.

The adapter mirrors :func:`tpcore.data.ingest_alpaca_bars.fetch_daily_bars_multi`'s
public shape (``dict[symbol] -> list[bar_dict]`` with ``o/h/l/c/v/t``
keys) so the existing ``_handle_daily_bars_explicit`` upsert path and
the CSV-archive collector wire in unchanged. Retries via the shared
``tpcore.outage.with_retry`` decorator (no local tenacity, per the
data-adapter rule).
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time
from typing import Any

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
"""Same base URL as ``tpcore.fmp.fundamentals_adapter`` — kept duplicated
intentionally so this module is a self-contained adapter (no cross-import
into the fundamentals module which carries an httpx-client lifecycle)."""

_FMP_EOD_ENDPOINT = "historical-price-eod/full"

# 300 req/min Starter cap → 200ms inter-request sleep keeps us at ~5 rps
# with margin. Lower than Alpaca's 350ms because FMP responses are
# smaller (one symbol per call) and the cap is the same order.
_RATE_LIMIT_SLEEP_SEC = 0.2


def _fmp_api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise DataProviderOutage(
            "FMP_API_KEY not set in environment — FMP daily-bars adapter cannot start"
        )
    return key


def _to_fmp_symbol(symbol: str) -> str:
    """Translate an Alpaca-style ticker to FMP's symbol vocabulary.

    Class-share suffix is the only known divergence at the Starter
    tier: Alpaca/CTA uses ``.`` (``BRK.B``, ``BF.B``), FMP uses ``-``
    (``BRK-B``, ``BF-B``). Probed live 2026-05-22: ``BRK.B`` returns
    HTTP 402 "Premium Query Parameter" while ``BRK-B`` returns 200 with
    identical OHLCV — same data, different spelling vocabulary, NOT a
    real subscription gap. The translation is required for every
    class-B share to land in the corpus under its Alpaca-canonical
    symbol.
    """
    return symbol.replace(".", "-") if "." in symbol else symbol


def _to_alpaca_shape(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate FMP's EOD response into the ``o/h/l/c/v/t/vw`` dict
    shape that ``handle_daily_bars`` and ``_upsert_bars`` expect from
    Alpaca. Stable ordering: most-recent first → most-recent last so
    the downstream ``bars[-1]`` last-close access in the all_active
    coarse-filter still makes semantic sense.

    Skips rows missing any of the required OHLCV fields rather than
    fabricating zeros — physical-truth gate downstream rejects those
    anyway, and silent zero-fills would mask vendor gaps.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            session_date = date.fromisoformat(row["date"])
        except (KeyError, ValueError, TypeError):
            continue
        o = row.get("open")
        h = row.get("high")
        low = row.get("low")
        c = row.get("close")
        v = row.get("volume")
        if o is None or h is None or low is None or c is None or v is None:
            continue
        # Synthesize a UTC ISO timestamp at midnight UTC of the session
        # date — _upsert_bars only reads .date() from this so the
        # specific time-of-day doesn't matter; midnight UTC is the
        # canonical anchor.
        ts = datetime.combine(session_date, time(0, 0)).isoformat() + "Z"
        out.append({
            "t": ts,
            "o": float(o),
            "h": float(h),
            "l": float(low),
            "c": float(c),
            "v": int(v),
            "vw": float(row.get("vwap")) if row.get("vwap") is not None else None,
        })
    # FMP returns most-recent-first; reverse so the list is chronological
    # to match the Alpaca multi-bar shape (ascending date).
    out.sort(key=lambda b: b["t"])
    return out


@with_retry(max_attempts=4, backoff_base_sec=1.0, backoff_cap_sec=20.0)
async def _fetch_one_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Single FMP EOD call for ``symbol``. Retries transient 429/5xx
    automatically; returns ``[]`` on 404 (ticker not in FMP) so a
    universe-wide pull never crashes on one missing name. Non-404 4xx
    raises ``DataProviderOutage`` immediately (auth, malformed key, etc.).

    ``symbol`` is the **Alpaca-canonical** spelling (e.g. ``BRK.B``);
    the FMP-side translation (``.`` → ``-`` for class-B shares) happens
    inside this function, so the caller and the returned dict always
    speak Alpaca/CTA. See :func:`_to_fmp_symbol`.
    """
    fmp_sym = _to_fmp_symbol(symbol)
    url = f"{FMP_BASE_URL}/{_FMP_EOD_ENDPOINT}"
    params: dict[str, str] = {
        "symbol": fmp_sym,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "apikey": _fmp_api_key(),
    }
    resp = await client.get(url, params=params)
    if resp.status_code == 200:
        body = resp.json()
        # FMP returns either a list of EOD rows or an empty list.
        if isinstance(body, list):
            return _to_alpaca_shape(body)
        # Some FMP endpoints wrap results in {"historical": [...]} —
        # the /stable/historical-price-eod/full surface returns a bare
        # list, but defend against an upstream shape change.
        if isinstance(body, dict) and isinstance(body.get("historical"), list):
            return _to_alpaca_shape(body["historical"])
        return []
    if resp.status_code == 404:
        # Ticker not in FMP — return empty, don't crash the batch.
        logger.info("fmp.bars.ticker_not_found", symbol=symbol)
        return []
    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        # Let @with_retry handle it.
        raise httpx.HTTPStatusError(
            f"FMP daily-bars {symbol} → {resp.status_code}",
            request=resp.request,
            response=resp,
        )
    # 4xx-not-{404,429} — permanent.
    raise DataProviderOutage(
        f"FMP daily-bars {symbol} returned {resp.status_code}: {resp.text[:200]}"
    )


async def fetch_daily_bars_multi(
    client: httpx.AsyncClient,
    symbols: list[str],
    start: date,
    end: date,
    *,
    feed: str = "fmp",  # noqa: ARG001 — accepted for signature parity with Alpaca path
) -> dict[str, list[dict[str, Any]]]:
    """Fetch daily OHLCV bars from FMP for each symbol.

    Public shape mirrors :func:`tpcore.data.ingest_alpaca_bars.fetch_daily_bars_multi`
    exactly so ``_handle_daily_bars_explicit`` can swap adapters without
    changing the surrounding code. The ``feed`` argument is accepted but
    ignored — FMP has one canonical feed (consolidated CTA tape) at
    the operator's tier.

    No batch endpoint at the Starter tier; this fans out per-symbol with
    a 200ms inter-request sleep. For a 7,600-ticker active universe
    that's ~25 min wall time, well under the chunked stage's 3600s
    budget when invoked via ``_force_refresh_chunked``.
    """
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    for symbol in symbols:
        try:
            out[symbol] = await _fetch_one_symbol(client, symbol, start, end)
        except DataProviderOutage as exc:
            # Permanent auth / shape failure — bubble so the caller can
            # decide to abort vs continue. The chunked stage above
            # catches at the chunk boundary; a single-shot caller sees
            # the outage and treats it as a vendor down event.
            logger.error("fmp.bars.permanent_failure", symbol=symbol, error=str(exc)[:200])
            raise
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            # Transient transport error survived @with_retry's 4 attempts
            # (RemoteProtocolError, connection drop, read timeout). One
            # bad-luck ticker should NOT abort the 7,600-ticker universe —
            # log, leave out[symbol] as the empty default, continue.
            logger.warning(
                "fmp.bars.skipped_after_retry_exhausted",
                symbol=symbol,
                error=type(exc).__name__,
                message=str(exc)[:200],
            )
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return out


__all__ = [
    "FMP_BASE_URL",
    "fetch_daily_bars_multi",
]
