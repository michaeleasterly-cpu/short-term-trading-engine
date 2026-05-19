"""Centralised client_order_id builders + parsers for cross-engine isolation.

The platform runs the engines (momentum, reversion, vector, sentinel;
Sigma archived 2026-05-16) against a single Alpaca paper account. The
``sg_`` prefix is retained below so historical Sigma orders remain
attributable — it is never minted for new orders. Without sub-account
isolation the only way
to attribute an order back to its originating engine is by inspecting the
``client_order_id`` field stamped at submission time.

This module is the single source of truth for that scheme so that two
engines running concurrently (or in the same process, or after a process
restart) **cannot** mistake each other's orders for their own.

### Canonical format

``<engine>_<TICKER>_<TS>[_tierN]``

Two-character engine prefixes — chosen so no prefix is a prefix of another:

* ``mo_`` — momentum (single order per ticker per rebalance)
* ``sg_`` — sigma (Tier 1 + Tier 2 OCO bracket) — archived, historical-only
* ``rv_`` — reversion (Tier 1 + Tier 2 OCO bracket)
* ``vc_`` — vector (parent + take-profit + stop-loss bracket)

### Legacy formats (still accepted for in-flight orders)

* ``mo_<TICKER>_<TS>`` — same as new
* ``<TICKER>_<TS>_tier1`` / ``_tier2`` — old sigma + reversion shape
* ``vector_<TICKER>_<TS>`` — old vector parent prefix

Parsers accept both indefinitely; the cost is one extra string check per
order and it lets positions opened today survive rollout.

### Why two-character prefixes

Uniform width avoids prefix-of-prefix collisions (``re_`` would clash with
Python ``re`` in greps; ``rev_`` is fine but mixing ``vector_`` and ``mo_``
at different widths makes the registry asymmetrical). Two chars keeps the
total well under Alpaca's 128-char ``client_order_id`` limit even with
long tickers and 10-digit epochs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

# ────────────────────────────────────────────────────────────────────────
# Registry — the only place engine names map to / from prefixes
# ────────────────────────────────────────────────────────────────────────

ENGINE_PREFIX: dict[str, str] = {
    "momentum": "mo_",
    "sigma": "sg_",  # archived 2026-05-16 — kept for historical attribution only
    "reversion": "rv_",
    "vector": "vc_",
    "sentinel": "sn_",
    "canary": "ca_",  # pipeline-exercise heartbeat engine
    "catalyst": "ct_",  # SP-F: insider-cluster swing engine (LAB until operator ECR)
}

# Legacy → canonical engine. Parsers walk this AFTER checking ENGINE_PREFIX
# so new format wins on any ambiguity.
LEGACY_PREFIX: dict[str, str] = {
    "vector_": "vector",  # old vector parent prefix
}

LEGACY_TIER_SUFFIX = ("_tier1", "_tier2")

# Sanity: prefixes must be mutually exclusive (no engine's prefix can start
# with another engine's prefix). Enforced at import so the test suite + a
# stray production change both catch a collision immediately.
def _assert_no_prefix_collisions() -> None:
    all_prefixes = list(ENGINE_PREFIX.values()) + list(LEGACY_PREFIX.keys())
    for i, p1 in enumerate(all_prefixes):
        for p2 in all_prefixes[i + 1 :]:
            if p1.startswith(p2) or p2.startswith(p1):
                raise RuntimeError(
                    f"order_ids prefix collision: '{p1}' and '{p2}' — one "
                    "is a prefix of the other; attribution would be ambiguous."
                )


_assert_no_prefix_collisions()


# ────────────────────────────────────────────────────────────────────────
# Builders
# ────────────────────────────────────────────────────────────────────────


def _ts(constructed_at: datetime | None = None) -> int:
    """Stable integer epoch — same shape every engine has used."""
    return int((constructed_at or datetime.now(UTC)).timestamp())


def build_close_id(engine: str, ticker: str, as_of: date) -> str:
    """Stable per-close dedupe key for a batch-engine position close.

    Batch engines (momentum/sentinel) submit ONE day-market order per
    ticker per rebalance and carry no per-trade entry record — so the
    only identity that is (a) stable and (b) *identically derivable* by
    BOTH close paths (the scheduler rebalance-sell loop and the
    trade-monitor stream) for the same real close is
    ``(engine, ticker, rebalance-date)``. This is the ``trade_id`` fed
    to ``RiskGovernor.record_close`` so the ``risk_close_ledger``
    ``(engine, trade_id)`` PK arbitrates a close to AT MOST one
    decrement (#251 B1). ``as_of`` is the rebalance ``date`` (its
    ISO ``YYYY-MM-DD`` string is used — deterministic, path-independent).
    """
    prefix = ENGINE_PREFIX.get(engine)
    if prefix is None:
        raise ValueError(f"unknown engine '{engine}'; expected one of {sorted(ENGINE_PREFIX)}")
    return f"{prefix}{ticker}_close_{as_of.isoformat()}"


def build_cid(
    engine: str,
    ticker: str,
    *,
    constructed_at: datetime | None = None,
    tier: str | None = None,
) -> str:
    """Build a canonical ``client_order_id`` for ``engine``.

    ``tier`` is the OCO leg label for sigma/reversion (``"tier1"`` or
    ``"tier2"``). Pass ``None`` for momentum and vector parent orders.
    Bracket child orders (TP/SL) live in Alpaca's order-tree linkage; we
    don't stamp them ourselves, the broker auto-generates child cids.
    """
    prefix = ENGINE_PREFIX.get(engine)
    if prefix is None:
        raise ValueError(f"unknown engine '{engine}'; expected one of {sorted(ENGINE_PREFIX)}")
    base = f"{prefix}{ticker}_{_ts(constructed_at)}"
    if tier is not None:
        if tier not in ("tier1", "tier2"):
            raise ValueError(f"invalid tier '{tier}'; expected 'tier1' or 'tier2'")
        base = f"{base}_{tier}"
    return base


# ────────────────────────────────────────────────────────────────────────
# Parsers — accept both canonical and legacy formats
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedCid:
    """Structured view of a client_order_id.

    ``engine`` is None when no engine prefix or legacy pattern matches —
    that means the cid did not originate from one of this platform's
    engines (manual order, smoke test, etc.).
    """

    engine: str | None
    tier: str | None  # "tier1" / "tier2" / None
    trade_key: str | None  # the ``<TICKER>_<TS>`` chunk; ties tier1/tier2 together


def engine_for_cid(client_order_id: str | None) -> str | None:
    """Return the engine name owning ``client_order_id``, or None."""
    return parse_cid(client_order_id).engine


def parse_cid(client_order_id: str | None) -> ParsedCid:
    """Decompose a client_order_id into ``(engine, tier, trade_key)``.

    Order of checks (first match wins):

    1. Canonical ``<engine_prefix>...`` — covers all new orders.
    2. Legacy ``vector_...`` — vector pre-prefix-migration.
    3. Legacy ``<TICKER>_<TS>_tier1|2`` — sigma/reversion pre-migration.
       Cannot disambiguate sigma vs reversion from the cid alone; returns
       ``engine=None`` so the caller can fall back to in-process state
       (the order_manager's ``_trade_assessments`` map).
    """
    if not client_order_id:
        return ParsedCid(engine=None, tier=None, trade_key=None)

    # Canonical: <engine_prefix>... — return immediately.
    for engine, prefix in ENGINE_PREFIX.items():
        if client_order_id.startswith(prefix):
            return _parse_canonical(client_order_id, engine, prefix)

    # Legacy vector parent.
    for legacy_prefix, engine in LEGACY_PREFIX.items():
        if client_order_id.startswith(legacy_prefix):
            return _parse_canonical(client_order_id, engine, legacy_prefix)

    # Legacy sigma/reversion tier suffix — engine unknowable from cid alone.
    for tier_suffix in LEGACY_TIER_SUFFIX:
        if client_order_id.endswith(tier_suffix):
            tier = tier_suffix.lstrip("_")  # "tier1" / "tier2"
            trade_key = client_order_id[: -len(tier_suffix)]
            return ParsedCid(engine=None, tier=tier, trade_key=trade_key)

    return ParsedCid(engine=None, tier=None, trade_key=None)


def _parse_canonical(cid: str, engine: str, prefix: str) -> ParsedCid:
    rest = cid[len(prefix) :]
    tier: str | None = None
    for tier_suffix in LEGACY_TIER_SUFFIX:
        if rest.endswith(tier_suffix):
            tier = tier_suffix.lstrip("_")
            rest = rest[: -len(tier_suffix)]
            break
    return ParsedCid(engine=engine, tier=tier, trade_key=rest)


def is_engine_cid(client_order_id: str | None, engine: str) -> bool:
    """True iff ``client_order_id`` belongs to ``engine``.

    Uses canonical-prefix matching primarily. Returns False for legacy
    tier-suffix cids (their engine can't be determined from the cid
    alone) — callers that need to handle those must consult in-process
    state.
    """
    if not client_order_id:
        return False
    prefix = ENGINE_PREFIX.get(engine)
    if prefix is not None and client_order_id.startswith(prefix):
        return True
    # Legacy vector parent — still attribute to vector.
    if engine == "vector":
        for legacy_prefix, mapped in LEGACY_PREFIX.items():
            if mapped == "vector" and client_order_id.startswith(legacy_prefix):
                return True
    return False


__all__ = [
    "ENGINE_PREFIX",
    "LEGACY_PREFIX",
    "ParsedCid",
    "build_cid",
    "engine_for_cid",
    "is_engine_cid",
    "parse_cid",
]
