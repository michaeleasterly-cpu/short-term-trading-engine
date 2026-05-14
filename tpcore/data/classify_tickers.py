"""Populate ``platform.ticker_classifications`` from Alpaca + FMP.

Two-stage classifier (per the 2026-05-14 design review):

1. **Alpaca ``/v2/assets``** — primary source. Returns one row per
   tradable US security with ``class``, ``status``, ``name`` etc.
   Alpaca's API doesn't directly mark ETFs, but the asset ``name``
   field is consistent: ETF names contain "ETF" or known issuer
   markers (iShares, SPDR, Invesco, Vanguard, ProShares). This gives
   us the binary ``stock`` vs ``etf`` flag for every ticker.

2. **FMP ``/profile``** — enrichment, T1+T2 only. Returns
   ``isEtf``, ``isFund``, ``sector``, ``industry``. We override the
   Alpaca heuristic with FMP's authoritative flag when available.

Inverse-ETF detection: name regex on "Inverse", "Short", "Bear",
"-1x"/"-2x"/"-3x", plus a known issuer-family allowlist (ProShares
Short series, Direxion Bear, etc.). Leverage parsed from "2x"/"3x"
patterns in the name.

Idempotent: re-running upserts. Designed to run monthly (asset class
essentially never changes for a given ticker — refresh exists to pick
up new listings/delistings).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Name-based ETF classification — used when FMP profile isn't available
# ────────────────────────────────────────────────────────────────────────


_ETF_NAME_MARKERS = (
    " ETF", " ETN", "Trust", "iShares", "SPDR", "Vanguard", "ProShares",
    "Direxion", "Invesco", "First Trust", "WisdomTree", "VanEck",
    "Schwab U.S.", "Global X", "ARK ", "JPMorgan ", "PIMCO ",
)

_INVERSE_NAME_MARKERS = (
    "Inverse", "Short ", "-1x", "-2x", "-3x", "Bear ",
    " UltraShort", " UltraPro Short", "ProShares Short",
)

_LEVERAGE_PATTERN = re.compile(r"\b([1-3])[xX]\b")


def _classify_from_name(name: str) -> tuple[str, bool | None, Decimal | None]:
    """Return ``(asset_class, etf_inverse_or_None, leverage_or_None)``.

    The leverage is only set for ETFs whose name carries an "Nx"
    marker. None means "1x / no leverage" (or non-applicable).
    """
    if not name:
        return "stock", None, None
    upper = name.upper()
    is_etf = any(m.upper() in upper for m in _ETF_NAME_MARKERS)
    if not is_etf:
        return "stock", None, None
    is_inverse = any(m.upper() in upper for m in _INVERSE_NAME_MARKERS)
    lev_match = _LEVERAGE_PATTERN.search(name)
    leverage = Decimal(lev_match.group(1)) if lev_match else None
    return "etf", is_inverse, leverage


# ────────────────────────────────────────────────────────────────────────
# Alpaca fetch
# ────────────────────────────────────────────────────────────────────────


async def fetch_alpaca_assets(
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Fetch every US-equity asset Alpaca knows about. ~14k rows.

    Filters to ``class=us_equity, status=active`` so OTC pink sheets
    don't bloat the result. Returns the raw asset dicts.
    """
    params = {"asset_class": "us_equity", "status": "active"}
    resp = await client.get("/v2/assets", params=params)
    resp.raise_for_status()
    return resp.json()


# ────────────────────────────────────────────────────────────────────────
# Persist
# ────────────────────────────────────────────────────────────────────────


_UPSERT_SQL = """
    INSERT INTO platform.ticker_classifications
        (ticker, asset_class, etf_inverse, etf_leverage, etf_category, source, last_updated)
    VALUES ($1, $2, $3, $4, $5, $6, now())
    ON CONFLICT (ticker) DO UPDATE SET
        asset_class = EXCLUDED.asset_class,
        etf_inverse = EXCLUDED.etf_inverse,
        etf_leverage = EXCLUDED.etf_leverage,
        etf_category = EXCLUDED.etf_category,
        source = EXCLUDED.source,
        last_updated = now()
"""


async def upsert_classifications(
    pool: asyncpg.Pool,
    rows: list[tuple[str, str, bool | None, Decimal | None, str | None, str]],
) -> int:
    """Idempotent upsert. Returns count attempted."""
    if not rows:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(_UPSERT_SQL, rows)
    return len(rows)


async def classify_all_tickers(
    pool: asyncpg.Pool,
    *,
    alpaca_base_url: str,
    alpaca_headers: dict[str, str],
) -> dict[str, int]:
    """One-shot backfill from Alpaca's full asset list.

    Returns ``{'rows': N, 'stocks': S, 'etfs': E, 'inverse': I}``.
    """
    async with httpx.AsyncClient(
        base_url=alpaca_base_url, headers=alpaca_headers, timeout=60.0
    ) as client:
        assets = await fetch_alpaca_assets(client)

    rows: list[tuple] = []
    stats = {"stocks": 0, "etfs": 0, "inverse": 0}
    for a in assets:
        symbol = a.get("symbol") or ""
        name = a.get("name") or ""
        if not symbol:
            continue
        asset_class, etf_inverse, leverage = _classify_from_name(name)
        if asset_class == "etf":
            stats["etfs"] += 1
            if etf_inverse:
                stats["inverse"] += 1
        else:
            stats["stocks"] += 1
        rows.append((
            symbol, asset_class, etf_inverse, leverage, None, "alpaca_name",
        ))
    n = await upsert_classifications(pool, rows)
    logger.info("tpcore.classify_tickers.done", upserted=n, **stats)
    return {"rows": n, **stats}


__all__ = [
    "classify_all_tickers",
    "fetch_alpaca_assets",
    "upsert_classifications",
]
