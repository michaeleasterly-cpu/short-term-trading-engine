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


# ETF markers split into two categories:
#
# * **Absolute** — substring alone is sufficient to call something an
#   ETF. Standalone words like " ETF", " ETN" or unambiguous issuer
#   suffixes ("iShares Core ..." always means iShares).
# * **Anchored** — issuer prefixes that MUST co-occur with a fund/ETF
#   anchor word ("Fund", "ETF", "Trust", "Series") elsewhere in the
#   name. Without the anchor they match the issuer's other businesses
#   too (e.g. "JPMorgan " on its own matches the bank).
_ETF_NAME_MARKERS_ABSOLUTE = (
    " ETF", " ETN", "iShares", "SPDR", "Vanguard", "ProShares",
    "Direxion", "Invesco", "WisdomTree", "VanEck",
    "Schwab U.S.", "Global X", "ARK ",
    "Bill Fund", "Bond Fund", "Index Fund",
)
_ETF_NAME_MARKERS_ANCHORED_ISSUERS = (
    "JPMorgan ", "PIMCO ", "First Trust ",
)
_ETF_ANCHOR_WORDS = ("ETF", "FUND", "TRUST", "SERIES")

_INVERSE_NAME_MARKERS = (
    "Inverse", "Short ", "-1x", "-2x", "-3x", "Bear ",
    " UltraShort", " UltraPro Short", "ProShares Short",
)

_LEVERAGE_PATTERN = re.compile(r"\b([1-3])[xX]\b")

# SPAC markers — blank-check companies that legitimately lack
# ``fundamentals_quarterly`` because there's no operating business
# until merger. Two flavors:
#   * "Acquisition" keyword anywhere in the name (covers "Acquisition
#     III Corp", "Acquisition I Co", etc. — Roman numerals between)
#   * "Class A Ordinary Shares" trailer — common SPAC share class
#   * Generic SPAC keywords
_SPAC_KEYWORDS = (
    "ACQUISITION",
    "SPAC ",
    "SPECIAL PURPOSE ACQUISITION",
    "BLANK CHECK",
    "MERGER CORP",
    "COMBINATION CORP",
    "CLASS A ORDINARY SHARES",  # SPAC shareholder class trailer
    "EQUITY PARTNERS",  # Cantor Equity Partners etc.
)

# SPAC ticker suffix markers — units (.U / U) and warrants (.W / W /
# WS / RW) traded alongside the underlying SPAC ticker.
_SPAC_TICKER_SUFFIXES = ("U", "W", "WS", "RW", "WW", ".U", ".W", "-U", "-W")

# Fund markers — closed-end funds, BDCs, preferred shares, notes, and
# structured products. These have NO ``fundamentals_quarterly`` rows
# because FMP doesn't model debt/preferred instruments as equities.
_FUND_KEYWORDS = (
    "NOTES DUE",
    "SENIOR NOTES",
    "SUBORDINATED NOTES",
    "TERM PREFERRED",
    "PERPETUAL PREFERRED",
    "PREFERRED STOCK",
    "PREFERRED SHARES",  # SPME and other variants
    "DEPOSITARY SHARES",
    "TRUST CERT",
    "TR CERT",  # JBK uses the abbreviated "Tr Cert"
    "STRATS",
    "PPLUS",
    "CORTS",
    "INVESTMENT CORP",  # BDCs (Bain Capital GSS, Carlyle Credit, etc.)
    "CREDIT FUND",
    "INCOME FUND",
    "DIVERSIFIED VALUE FUND",
    "ECONOMIC FUND",   # AKAF: "The Frontier Economic Fund"
    "OPPORTUNITIES FUND",  # Thornburg American Opportunities Fund
    "GROWTH FUND",   # Thornburg Focus Growth Fund + similar mutual funds
    "VALUE FUND",
    "STRUCTURED PRODUCTS",
    "FIXED-INCOME SECURITIES",
)


def _classify_from_name(
    name: str, ticker: str = ""
) -> tuple[str, bool | None, Decimal | None]:
    """Return ``(asset_class, etf_inverse_or_None, leverage_or_None)``.

    Classifier order (first match wins, so most specific markers go first):

    1. **SPAC by name**: name contains "Acquisition", "Class A Ordinary
       Shares", or similar blank-check keyword.
    2. **SPAC by ticker suffix**: 4+ char ticker ending in U/W/WS/RW
       (units and warrants).
    3. **Fund by name**: notes, preferred shares, BDCs, structured
       products — anything FMP doesn't model as equity fundamentals.
    4. **ETF by name**: contains "ETF", a known issuer, or generic
       "Bill Fund"/"Bond Fund" pattern. Sets etf_inverse + leverage.
    5. **Stock**: anything else (the operating-company default).
    """
    if not name and not ticker:
        return "stock", None, None
    upper = (name or "").upper()

    # 1. SPAC by name.
    if any(kw in upper for kw in _SPAC_KEYWORDS):
        return "spac", None, None

    # 2. SPAC by ticker suffix (warrants/units). Skip 3-char tickers.
    if ticker and len(ticker) >= 4:
        for sfx in _SPAC_TICKER_SUFFIXES:
            if ticker.endswith(sfx):
                return "spac", None, None

    # 3. Fund / preferred / notes / structured products.
    if any(kw in upper for kw in _FUND_KEYWORDS):
        return "fund", None, None

    # 4. ETF markers — absolute (any match wins) OR anchored
    # (issuer prefix that needs an anchor word elsewhere).
    is_etf = any(m.upper() in upper for m in _ETF_NAME_MARKERS_ABSOLUTE)
    if not is_etf:
        for issuer in _ETF_NAME_MARKERS_ANCHORED_ISSUERS:
            if issuer.upper() in upper and any(a in upper for a in _ETF_ANCHOR_WORDS):
                is_etf = True
                break
    if is_etf:
        is_inverse = any(m.upper() in upper for m in _INVERSE_NAME_MARKERS)
        lev_match = _LEVERAGE_PATTERN.search(name)
        leverage = Decimal(lev_match.group(1)) if lev_match else None
        return "etf", is_inverse, leverage

    return "stock", None, None


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
    stats = {"stocks": 0, "etfs": 0, "inverse": 0, "spacs": 0, "funds": 0}
    for a in assets:
        symbol = a.get("symbol") or ""
        name = a.get("name") or ""
        if not symbol:
            continue
        asset_class, etf_inverse, leverage = _classify_from_name(name, symbol)
        if asset_class == "etf":
            stats["etfs"] += 1
            if etf_inverse:
                stats["inverse"] += 1
        elif asset_class == "spac":
            stats["spacs"] += 1
        elif asset_class == "fund":
            stats["funds"] += 1
        else:
            stats["stocks"] += 1
        rows.append((
            symbol, asset_class, etf_inverse, leverage, None, "alpaca_name",
        ))
    n = await upsert_classifications(pool, rows)

    # Follow-up: T1+T2 tickers Alpaca's bulk /v2/assets didn't return
    # (delisted-but-still-trading, special status) get a per-ticker
    # lookup. Caught NZUS / similar gaps on 2026-05-14.
    async with pool.acquire() as conn:
        unclassified = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2 AND tc.ticker IS NULL
            """
        )
    follow_up_rows: list[tuple] = []
    follow_up_stats = {"resolved": 0, "still_unclassified": 0}
    if unclassified:
        async with httpx.AsyncClient(
            base_url=alpaca_base_url, headers=alpaca_headers, timeout=30.0
        ) as client:
            for r in unclassified:
                sym = r["ticker"]
                try:
                    resp = await client.get(f"/v2/assets/{sym}")
                    if resp.status_code != 200:
                        follow_up_stats["still_unclassified"] += 1
                        continue
                    a = resp.json()
                    name = a.get("name") or ""
                    asset_class, etf_inverse, leverage = _classify_from_name(name, sym)
                    follow_up_rows.append((
                        sym, asset_class, etf_inverse, leverage, None, "alpaca_per_ticker",
                    ))
                    follow_up_stats["resolved"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("classify_tickers.per_ticker_failed", symbol=sym, error=str(exc))
                    follow_up_stats["still_unclassified"] += 1
    if follow_up_rows:
        await upsert_classifications(pool, follow_up_rows)
        n += len(follow_up_rows)

    logger.info(
        "tpcore.classify_tickers.done",
        upserted=n, **stats, **follow_up_stats,
    )
    return {"rows": n, **stats, **follow_up_stats}


__all__ = [
    "classify_all_tickers",
    "fetch_alpaca_assets",
    "upsert_classifications",
]
