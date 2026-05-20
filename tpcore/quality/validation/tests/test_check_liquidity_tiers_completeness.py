"""Tests for the zero-tolerance liquidity_tiers_completeness invariant.

The check is "every active-universe ticker has a row in
``platform.liquidity_tiers``" where active universe =
``ticker_classifications.asset_class = 'stock'`` ∩ (≥1 prices_daily bar
in the trailing 30 NYSE sessions). It runs two SQL queries:

* anti-join — active-universe tickers missing from liquidity_tiers
* 2-counts — active_universe_size + in_tiers

The fake pool below stages a single in-memory model for both:

* ``classifications`` — ``{ticker: asset_class}`` (asset_class='stock'
  is the qualifying value, anything else excludes the ticker)
* ``recent_prices_tickers`` — set of tickers with a bar in the trailing
  30 NYSE sessions
* ``tiers`` — set of tickers present in ``liquidity_tiers`` (any tier)

The fake intersects classifications + recent_prices_tickers to derive
the active universe in-memory and then performs the anti-join + counts
in Python. Mirrors the test_check_sec_insider_monotone fake-pool
pattern.
"""
from __future__ import annotations

from typing import Any

from tpcore.quality.validation.checks.liquidity_tiers_completeness import (
    CHECK_NAME,
    MAX_REPORTED,
    check_liquidity_tiers_completeness,
    compute_liquidity_tiers_repair_targets,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    def _active_universe(self) -> list[str]:
        """Intersection of stock asset_class + recent prices_daily."""
        stocks = {
            t for t, ac in self._owner.classifications.items()
            if ac == "stock"
        }
        # Tickers absent from classifications default to 'stock' (the
        # check uses COALESCE(asset_class, 'stock')).
        coalesced = set(self._owner.recent_prices_tickers)
        for t in self._owner.recent_prices_tickers:
            if t in self._owner.classifications:
                if self._owner.classifications[t] == "stock":
                    coalesced.add(t)
                else:
                    coalesced.discard(t)
            # else: COALESCE -> 'stock' -> stays in
        return sorted(coalesced & (stocks | {
            t for t in self._owner.recent_prices_tickers
            if t not in self._owner.classifications
        }))

    async def fetch(
        self, sql: str, *args: object
    ) -> list[dict[str, Any]]:
        sql_lower = sql.lower()
        # Anti-join: active-universe tickers MISSING from liquidity_tiers.
        if (
            "platform.liquidity_tiers" in sql_lower
            and "lt.ticker is null" in sql_lower
        ):
            active = self._active_universe()
            missing = [t for t in active if t not in self._owner.tiers]
            return [{"ticker": t} for t in missing]
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(
        self, sql: str, *args: object
    ) -> dict[str, Any] | None:
        sql_lower = sql.lower()
        if (
            "platform.liquidity_tiers" in sql_lower
            and "active_universe_size" in sql_lower
        ):
            active = self._active_universe()
            in_tiers = sum(1 for t in active if t in self._owner.tiers)
            return {
                "active_universe_size": len(active),
                "in_tiers": in_tiers,
            }
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(
        self,
        *,
        classifications: dict[str, str] | None = None,
        recent_prices_tickers: set[str] | None = None,
        tiers: set[str] | None = None,
    ) -> None:
        self.classifications: dict[str, str] = dict(classifications or {})
        self.recent_prices_tickers: set[str] = set(recent_prices_tickers or set())
        self.tiers: set[str] = set(tiers or set())

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


# ── C1 — active universe ⊆ tiers → PASS ──────────────────────────────


async def test_C1_active_universe_subset_of_tiers_passes() -> None:
    """Every active-universe ticker has a row in liquidity_tiers."""
    pool = _Pool(
        classifications={"AAPL": "stock", "MSFT": "stock", "GOOG": "stock"},
        recent_prices_tickers={"AAPL", "MSFT", "GOOG"},
        tiers={"AAPL", "MSFT", "GOOG", "OLD_DELISTED"},
    )
    result = await check_liquidity_tiers_completeness(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.name == CHECK_NAME
    assert result.total == 3  # active_universe_size


# ── C2 — one active-universe ticker missing → FAIL ───────────────────


async def test_C2_one_missing_active_ticker_fails() -> None:
    """ANY active-universe ticker missing from liquidity_tiers → FAIL."""
    pool = _Pool(
        classifications={"AAPL": "stock", "MSFT": "stock", "NEW": "stock"},
        recent_prices_tickers={"AAPL", "MSFT", "NEW"},
        tiers={"AAPL", "MSFT"},  # NEW missing
    )
    result = await check_liquidity_tiers_completeness(pool)
    assert result.passed is False
    assert result.failed == 1
    fail = result.failures[0]
    assert fail.ticker == "NEW"
    assert fail.reason == "missing_from_liquidity_tiers"


# ── C3 — non-stock asset_class missing → PASS (legitimate) ───────────


async def test_C3_non_stock_missing_passes() -> None:
    """ETFs / SPACs / funds are not tiered — their absence is legit."""
    pool = _Pool(
        classifications={
            "AAPL": "stock",
            "SPY": "etf",          # ETF — legitimately not tiered
            "BARK_SPAC": "spac",   # SPAC — legitimately not tiered
            "VFINX": "fund",       # fund — legitimately not tiered
        },
        recent_prices_tickers={"AAPL", "SPY", "BARK_SPAC", "VFINX"},
        tiers={"AAPL"},  # ONLY the stock — others legitimately absent
    )
    result = await check_liquidity_tiers_completeness(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    # Only the single stock is in the active universe.
    assert result.total == 1


# ── C4 — stale (no recent bars) ticker missing → PASS (legitimate) ───


async def test_C4_stale_ticker_missing_passes() -> None:
    """A stock with no bar in the trailing 30 NYSE sessions is NOT in
    the active universe — its absence from liquidity_tiers is legit."""
    pool = _Pool(
        classifications={
            "AAPL": "stock",
            "DORMANT": "stock",  # stock, but no recent bars
        },
        recent_prices_tickers={"AAPL"},  # DORMANT NOT in recent prices
        tiers={"AAPL"},  # DORMANT legitimately absent
    )
    result = await check_liquidity_tiers_completeness(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.total == 1


# ── C5 — multi-ticker missing — sample capped, count full ────────────


async def test_C5_many_missing_capped_sample_full_count() -> None:
    """If many active-universe tickers are missing, the FailureDetail
    list is capped at MAX_REPORTED but CheckResult.failed carries the
    TRUE count and the observed message reports it."""
    classifications = {f"T{i:02d}": "stock" for i in range(12)}
    recent = {f"T{i:02d}" for i in range(12)}
    # Only the first 2 are in tiers; T02..T11 (10 tickers) missing.
    tiers = {"T00", "T01"}
    pool = _Pool(
        classifications=classifications,
        recent_prices_tickers=recent,
        tiers=tiers,
    )
    result = await check_liquidity_tiers_completeness(pool)
    assert result.passed is False
    assert result.failed == 10  # true count of missing
    assert len(result.failures) == MAX_REPORTED == 5
    # The observed-text on every reported failure carries the full count.
    for f in result.failures:
        assert "Total missing: 10 / 12 active tickers" in f.observed


# ── C6 — healer symmetry: empty on clean ─────────────────────────────


async def test_C6_repair_targets_empty_on_clean() -> None:
    pool = _Pool(
        classifications={"AAPL": "stock", "MSFT": "stock"},
        recent_prices_tickers={"AAPL", "MSFT"},
        tiers={"AAPL", "MSFT"},
    )
    targets = await compute_liquidity_tiers_repair_targets(pool)
    assert targets == []


async def test_C6b_repair_targets_lists_missing_active() -> None:
    """Healer returns exactly the missing-active-universe ticker set —
    detector + healer share _evaluate, cannot disagree by
    construction."""
    pool = _Pool(
        classifications={
            "AAPL": "stock",
            "MSFT": "stock",
            "GOOG": "stock",
            "SPY": "etf",  # legitimately absent — not a target
        },
        recent_prices_tickers={"AAPL", "MSFT", "GOOG", "SPY"},
        tiers={"AAPL"},  # MSFT + GOOG missing; SPY legitimately absent
    )
    targets = await compute_liquidity_tiers_repair_targets(pool)
    assert set(targets) == {"MSFT", "GOOG"}
