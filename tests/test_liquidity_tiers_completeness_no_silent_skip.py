"""Regression — every active-universe ticker reaches ``liquidity_tiers``.

History (2026-05-21 audit → 2026-05-22 fix):
The ``liquidity_tiers_completeness`` validation check went red with 15
specific active-universe tickers silently missing:

    BMNR BXDC CBRS EMPG FRVO GMRS HAWK LAWR LCLN MAGH MOBI NUTR ODTX PC SUJA

All 15 had ≥1 ``platform.prices_daily`` bar in the trailing 30 NYSE
sessions and ``COALESCE(asset_class, 'stock') = 'stock'``, so by the
check's universe definition they MUST appear in
``platform.liquidity_tiers``. They did not.

Root cause: ``scripts/ops.py::_stage_tier_refresh`` has a 60-day
per-source bootstrap skip-guard to amortise the ~20-30min Abdi-Ranaldo
bootstrap. Tickers that entered the active universe BETWEEN bootstrap
runs (new IPOs, fresh relistings, universe expansions) had zero
``spread_observations`` rows for the active source — so the
per-source ``_AGGREGATE_SQL`` couldn't emit them, and they remained
silently absent from ``liquidity_tiers`` until the next full
bootstrap (up to ~60 days later).

Fix (this PR): ``scripts/assign_liquidity_tiers.assign_tiers`` runs
a second **gap-fill pass** after the aggregation upsert. Any
active-universe stock ticker still missing from
``platform.liquidity_tiers`` is inserted at ``tier=DEFAULT_TIER``,
``provisional=true``, ``observations=0``, with a placeholder
``median_spread_pct`` that lands exactly on DEFAULT_TIER (tested
below). The next full bootstrap overwrites these rows with real
estimates via ``ON CONFLICT (ticker) DO UPDATE``.

This regression locks the fix in place:

* For each of the 15 historically-affected tickers, the gap-fill pass
  emits a placeholder row with the documented tier / flags
  (parametrized below).
* The end-to-end ``check_liquidity_tiers_completeness`` invariant
  passes against a fixture where all 15 are in the active universe
  AND the producer ran ``assign_tiers``.
* Fail-on-main / pass-on-branch contract: stub a producer WITHOUT
  the gap-fill pass and verify the check goes red, then re-run with
  the real producer and verify it goes green.

NOT relying on the live DB. The producer is tested against an
in-memory fake pool that emulates the SQL the real Postgres would
execute (``spread_observations`` aggregation + active-universe
anti-join).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from scripts.assign_liquidity_tiers import (
    _GAP_FILL_PLACEHOLDER_SPREAD,
    DEFAULT_TIER,
    _tier_for,
    assign_tiers,
)
from tpcore.quality.validation.checks.liquidity_tiers_completeness import (
    check_liquidity_tiers_completeness,
)

# ── The 15 tickers from the 2026-05-22 corpus-fitness audit (PR #281
#    row §D #5). Each tagged with the diagnosed category that the
#    fix lands them in. Categorization (from the live DB on
#    2026-05-22):
#
#    NEW         — <20 bars total, brand-new IPO since the last
#                  bootstrap (2026-05-15). Bootstrap could not run
#                  because the 60-day per-source skip-guard fired.
#    LOW-LIQ     — ≥20 bars, BUT fails the bootstrap's coarse_filter
#                  (price <$10 OR avg_vol_20d <1M). The 2026-05-15
#                  full bootstrap ran with the legacy coarse_filter=True
#                  call path, so these tickers were dropped. Even
#                  with coarse_filter=False today, they're not
#                  emitted unless a fresh bootstrap rewrites the
#                  spread_observations table — which it can't until
#                  the per-source skip-guard expires.
#    RELISTED    — long-dormant ticker whose ONLY recent bars are in
#                  the trailing window (HAWK 2018→2026 gap, MOBI
#                  2016→2026, NUTR 2017→2026, MAGH 2017→2025). Same
#                  bootstrap-window pathology as NEW.
#
# Treatment: gap-fill upsert at DEFAULT_TIER + provisional=true +
# observations=0 for ALL of them. Conservative — they're NOT
# claimed to be liquid; the next full bootstrap (when the inner
# 60d gate opens) will overwrite with real estimates.
HISTORICALLY_MISSING_TICKERS: tuple[tuple[str, str], ...] = (
    ("BMNR", "NEW"),       # 10 bars, first 2026-05-08
    ("BXDC", "NEW"),       # 6 bars, first 2026-05-14
    ("CBRS", "NEW"),       # 6 bars, first 2026-05-14
    ("EMPG", "LOW-LIQ"),   # 75 bars, avg_vol=341k <1M
    ("FRVO", "NEW"),       # 7 bars, first 2026-05-13
    ("GMRS", "NEW"),       # 7 bars, first 2026-05-13
    ("HAWK", "RELISTED"),  # 11 recent, 245 old (2018-06-14 last prior)
    ("LAWR", "LOW-LIQ"),   # 75 bars, close=$3.75 <$10
    ("LCLN", "NEW"),       # 2 bars, first 2026-05-20
    ("MAGH", "RELISTED"),  # 87 recent, 83 old (2017-04-03)
    ("MOBI", "RELISTED"),  # 10 recent, 1481 old (2016-11-16)
    ("NUTR", "RELISTED"),  # 44 recent, 4350 old (2017-08-22)
    ("ODTX", "NEW"),       # 10 bars, first 2026-05-08
    ("PC",   "LOW-LIQ"),   # 107 bars, close=$9.40 + vol=393k
    ("SUJA", "NEW"),       # 10 bars, first 2026-05-07
)


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeConn:
    """Smart fake — dispatches on SQL shape.

    * ``platform.spread_observations`` aggregate → per-source aggregation
    * ``active_universe`` + ``lt.ticker IS NULL`` → gap-fill anti-join
    * ``platform.liquidity_tiers`` + ``active_universe_size`` → the
      check's universe-counts probe
    * ``platform.liquidity_tiers`` + ``lt.ticker IS NULL`` → the
      check's anti-join (mirrors gap-fill but consumed by the check)
    """

    def __init__(
        self,
        *,
        owner: _FakePool,
    ) -> None:
        self._owner = owner

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        if "platform.spread_observations" in sql:
            return [
                {
                    "ticker": t,
                    "median_spread_pct": Decimal(str(s)),
                    "p95_spread_pct": Decimal(str(s)) * 2,
                    "observations": n,
                }
                for t, s, n in self._owner.spread_obs
            ]
        if (
            "active_universe" in sql
            and "lt.ticker IS NULL" in sql
            and "ORDER BY au.ticker" in sql
        ):
            # Producer's gap-fill query OR check's anti-join — both
            # share the "active-universe minus liquidity_tiers" shape.
            active = self._owner.active_universe_stock_tickers
            return [
                {"ticker": t}
                for t in sorted(active)
                if t not in self._owner.liquidity_tiers
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql[:160]}")

    async def fetchrow(
        self, sql: str, *args: object
    ) -> dict[str, Any] | None:
        if (
            "platform.liquidity_tiers" in sql
            and "active_universe_size" in sql
        ):
            active = self._owner.active_universe_stock_tickers
            in_tiers = sum(1 for t in active if t in self._owner.liquidity_tiers)
            return {
                "active_universe_size": len(active),
                "in_tiers": in_tiers,
            }
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:160]}")

    async def executemany(
        self, sql: str, rows: list[tuple]
    ) -> None:
        # Mirror the producer's ON CONFLICT (ticker) DO UPDATE.
        for r in rows:
            ticker, tier, median, p95, observations, provisional = r
            self._owner.liquidity_tiers[ticker] = {
                "tier": int(tier),
                "median_spread_pct": Decimal(str(median)),
                "p95_spread_pct": Decimal(str(p95)),
                "observations": int(observations),
                "provisional": bool(provisional),
            }


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    """In-memory model of the platform tables this test cares about.

    * ``active_universe_stock_tickers`` — what the check's
      active-universe CTE would return (stock asset_class + ≥1 bar in
      trailing 30 NYSE sessions, not delisted).
    * ``spread_obs`` — what the producer's aggregation query would
      see — ``[(ticker, median_spread_pct, observations), ...]``.
    * ``liquidity_tiers`` — current state of the destination table.
      Mutated by ``executemany`` (the upsert).
    """

    def __init__(
        self,
        *,
        active_universe_stock_tickers: set[str],
        spread_obs: list[tuple[str, float, int]] | None = None,
        liquidity_tiers: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.active_universe_stock_tickers: set[str] = set(
            active_universe_stock_tickers
        )
        self.spread_obs: list[tuple[str, float, int]] = list(spread_obs or [])
        self.liquidity_tiers: dict[str, dict[str, Any]] = dict(
            liquidity_tiers or {}
        )

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn(owner=self))

    async def close(self) -> None:
        return None


async def _patched_assign_tiers(
    pool: _FakePool, monkeypatch: pytest.MonkeyPatch
) -> dict[int, int]:
    """Run ``assign_tiers`` against a fake pool — patches
    ``build_asyncpg_pool`` so the real ``db_url`` arg is ignored."""
    from scripts import assign_liquidity_tiers

    async def _fake_build(*args: object, **kwargs: object) -> _FakePool:
        return pool

    monkeypatch.setattr(
        assign_liquidity_tiers, "build_asyncpg_pool", _fake_build
    )
    return await assign_tiers(
        db_url="postgresql://noop", sources=["abdi_ranaldo"]
    )


# ── Test 1 — placeholder spread lands on DEFAULT_TIER ──────────────────


def test_gap_fill_placeholder_spread_maps_to_default_tier() -> None:
    """The placeholder ``_GAP_FILL_PLACEHOLDER_SPREAD`` must map via
    ``_tier_for`` to exactly ``DEFAULT_TIER``. The producer asserts
    this at runtime too — this test catches a regression at
    development time instead of in a 7k-ticker production run."""
    assert _tier_for(_GAP_FILL_PLACEHOLDER_SPREAD) == DEFAULT_TIER


# ── Test 2 — each historically-missing ticker is gap-filled ────────────


@pytest.mark.parametrize(
    ("ticker", "category"),
    HISTORICALLY_MISSING_TICKERS,
)
async def test_each_missing_ticker_receives_default_tier_row(
    ticker: str, category: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For each of the 15 historically-missing tickers — when it
    appears in the active universe AND the per-source aggregation
    has zero rows for it — the gap-fill pass MUST insert a row at
    ``tier=DEFAULT_TIER``, ``provisional=true``, ``observations=0``.
    """
    pool = _FakePool(
        active_universe_stock_tickers={ticker, "AAPL", "MSFT"},
        # spread_obs covers ONLY AAPL + MSFT — this is the exact
        # production shape: the affected ticker was added to the
        # active universe AFTER the last bootstrap so the per-source
        # aggregation can't emit it.
        spread_obs=[
            ("AAPL", 0.0002, 100),
            ("MSFT", 0.0010, 80),
        ],
        liquidity_tiers={},
    )
    bucket = await _patched_assign_tiers(pool, monkeypatch)

    row = pool.liquidity_tiers.get(ticker)
    assert row is not None, (
        f"{category} ticker {ticker} was NOT inserted by the gap-fill "
        f"pass — regression of the audit-2026-05-21-#260 defect"
    )
    assert row["tier"] == DEFAULT_TIER, (
        f"{ticker} got tier={row['tier']}, expected DEFAULT_TIER="
        f"{DEFAULT_TIER}"
    )
    assert row["provisional"] is True, (
        f"{ticker} gap-fill must be provisional=True (audit signal)"
    )
    assert row["observations"] == 0, (
        f"{ticker} gap-fill must have observations=0 (audit signal)"
    )
    # The placeholder spread is the audit fingerprint — bumping it
    # without bumping the tier mapping is the kind of silent change
    # that would re-introduce the original defect.
    assert row["median_spread_pct"] == _GAP_FILL_PLACEHOLDER_SPREAD

    # The aggregation pass must still emit AAPL + MSFT correctly.
    assert "AAPL" in pool.liquidity_tiers
    assert "MSFT" in pool.liquidity_tiers
    assert pool.liquidity_tiers["AAPL"]["observations"] == 100

    # Bucket telemetry should include DEFAULT_TIER for the gap-fill.
    assert bucket.get(DEFAULT_TIER, 0) >= 1


# ── Test 3 — end-to-end: producer → check goes GREEN ───────────────────


async def test_check_passes_after_producer_runs_with_all_15(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end contract: a fresh active universe that includes
    ALL 15 historically-missing tickers + a 9k-ticker active base
    starts with the check RED (15 missing). After
    ``assign_tiers`` runs (real aggregation for the 9k base + gap-fill
    for the 15), the check goes GREEN."""
    base = {f"BASE{i:04d}" for i in range(9000)}
    new = {t for t, _ in HISTORICALLY_MISSING_TICKERS}
    assert len(new) == 15
    active = base | new

    # spread_obs only covers the 9k base (the production shape — base
    # tickers were in the universe at the last full bootstrap; the 15
    # newcomers were not).
    spread_obs = [(t, 0.0003, 50) for t in sorted(base)]

    pool = _FakePool(
        active_universe_stock_tickers=active,
        spread_obs=spread_obs,
        liquidity_tiers={},  # nothing pre-existing
    )

    # ── Pre-condition: WITHOUT the producer running, the check is RED ──
    pre_check = await check_liquidity_tiers_completeness(pool)  # type: ignore[arg-type]
    assert pre_check.passed is False
    assert pre_check.failed == len(active)  # everything missing

    # ── Run the producer ──
    bucket = await _patched_assign_tiers(pool, monkeypatch)
    # Every active-universe ticker now has a row.
    assert len(pool.liquidity_tiers) == len(active)
    # 9k aggregated + 15 gap-filled = 9015 in DEFAULT_TIER for our
    # fixture (since base spread = 0.0003 → T1, and the 15 land in
    # DEFAULT_TIER via gap-fill).
    assert bucket.get(DEFAULT_TIER, 0) == 15

    # ── Post-condition: check is GREEN ──
    post_check = await check_liquidity_tiers_completeness(pool)  # type: ignore[arg-type]
    assert post_check.passed is True, [
        f.observed for f in post_check.failures
    ]
    assert post_check.failed == 0
    assert post_check.total == len(active)


# ── Test 4 — fail-on-main contract: WITHOUT gap-fill, check still RED ──


async def test_check_still_red_when_producer_lacks_gap_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-on-main contract: stub the producer to only run the
    aggregation pass (the pre-fix behavior). The check MUST go red
    for the 15 missing tickers. This locks in the contract: the only
    way for the check to go green is for the producer to fill the
    gap — lowering thresholds or filtering the check's universe is
    not a valid fix per the operator's no-fake-green rule."""
    from scripts import assign_liquidity_tiers

    new = {t for t, _ in HISTORICALLY_MISSING_TICKERS}
    active = {"AAPL", "MSFT"} | new
    pool = _FakePool(
        active_universe_stock_tickers=active,
        spread_obs=[("AAPL", 0.0002, 100), ("MSFT", 0.0010, 80)],
        liquidity_tiers={},
    )

    # Monkey-patch _gap_fill_active_universe to a no-op (simulates
    # pre-fix producer that never runs the gap-fill pass).
    async def _no_gap_fill(conn: Any) -> list[str]:
        return []

    monkeypatch.setattr(
        assign_liquidity_tiers,
        "_gap_fill_active_universe",
        _no_gap_fill,
    )
    await _patched_assign_tiers(pool, monkeypatch)

    # Without gap-fill, only AAPL + MSFT got tiered.
    assert set(pool.liquidity_tiers.keys()) == {"AAPL", "MSFT"}

    # Check goes red for the 15.
    result = await check_liquidity_tiers_completeness(pool)  # type: ignore[arg-type]
    assert result.passed is False
    assert result.failed == 15
    missing_in_result = {f.ticker for f in result.failures}
    # All 5 reported failures must be from our 15 (the check caps
    # FailureDetail at MAX_REPORTED=5 but ``failed`` carries the
    # true count).
    assert missing_in_result.issubset(new)
