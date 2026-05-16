"""Universe tradeability primitives — shared, engine-agnostic.

These were defined in ``momentum.models`` and imported by
``tpcore.universe.prescreener`` — a tpcore→engine import that violates
the layering invariant (**tpcore must never import an engine; engines
import only via tpcore**). They are generic "is this a real, tradeable
common stock" rules with zero momentum-specific logic, so they belong
here. ``momentum.models`` now re-exports from this module (engine→
tpcore is the correct direction); all call sites are unchanged.
"""
from __future__ import annotations

from decimal import Decimal

# Match the validated backtest universe (T1+T2). Phase 1's
# parameter-search edge was earned on T1+T2 specifically; widening
# live to T3 requires re-running the search at that tier first.
MAX_TIER_FOR_TRADING = 2

# Even at T1+T2 the universe contains warrants, units, preferred
# shares, and sub-$5 names that must not be traded (smoke 2026-05-13
# surfaced XBPEW/BBLGW/NAMSW warrants + a $0.06 position). The same
# filter must apply in live setup_detection AND backtest so they agree.
MIN_PRICE_FLOOR = Decimal("5.00")  # SEC "penny stock" line; < $5 → drop

# Tickers containing any of these separators are non-common share
# classes (preferreds `.PR`, units `.U`, rights `=R`, dual `-A`/`-B`).
# Common stocks on US exchanges never contain these.
TICKER_SEPARATOR_CHARS: tuple[str, ...] = (".", "-", "/", "=")

# Warrants typically end in W/WS on US exchanges. The 5+-char guard
# avoids false positives like CDW / ZWS (3-char real common stocks).
WARRANT_SUFFIXES: tuple[str, ...] = ("W", "WS")
WARRANT_MIN_TICKER_LEN = 5


def is_tradeable_common_stock(ticker: str, last_close: Decimal) -> bool:
    """Return False for warrants, units, preferreds, and sub-$5 names.

    Conservative — filters the obvious offenders without rejecting
    real common stocks. Deliberately does NOT filter a single trailing
    `P` (too many false positives like APLS, EART)."""
    if last_close < MIN_PRICE_FLOOR:
        return False
    if any(sep in ticker for sep in TICKER_SEPARATOR_CHARS):
        return False
    if len(ticker) >= WARRANT_MIN_TICKER_LEN:
        for suffix in WARRANT_SUFFIXES:
            if ticker.endswith(suffix):
                return False
    return True


__all__ = [
    "MAX_TIER_FOR_TRADING",
    "MIN_PRICE_FLOOR",
    "TICKER_SEPARATOR_CHARS",
    "WARRANT_SUFFIXES",
    "WARRANT_MIN_TICKER_LEN",
    "is_tradeable_common_stock",
]
