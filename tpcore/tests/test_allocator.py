"""Tests for the Allocator service.

Focus on the pure-function decision logic — the SQL persistence path
is integration-tested via the live DB invocation.
"""

from __future__ import annotations

from decimal import Decimal

from tpcore.allocator.service import (
    HARD_FREEZE_SOFT_SESSIONS,
    MIN_AARS_FOR_VOL,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    AllocatorService,
    _EngineHistory,
)


def _hist(
    engine: str,
    *,
    pnls: list[float] | None = None,
    aar_count: int | None = None,
    peak_override: float | None = None,
    current_override: float | None = None,
    soft_streak: int = 0,
) -> _EngineHistory:
    pnls = pnls or []
    seed = 10_000.0
    eq = seed
    curve: list[float] = []
    for p in pnls:
        eq += p
        curve.append(eq)
    peak = peak_override if peak_override is not None else (max(curve) if curve else seed)
    current = current_override if current_override is not None else (curve[-1] if curve else seed)
    return _EngineHistory(
        engine=engine,
        aar_count=aar_count if aar_count is not None else len(pnls),
        daily_pnls=pnls,
        equity_curve=curve,
        peak_equity=peak,
        current_equity=current,
        soft_frozen_sessions=soft_streak,
    )


# ── Bootstrap (equal weight, no σ yet) ──────────────────────────────────


def test_bootstrap_equal_weights_when_all_engines_below_min_aar():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    hs = [_hist(e, pnls=[1.0, -1.0]) for e in svc._engines]
    decisions = svc._decide(hs)
    weights = {d.engine: d.weight for d in decisions}
    assert all(d.realized_vol is None for d in decisions), "no engine has enough AARs"
    # Equal-ish weight (after [0.10, 0.50] cap iteration, 4 engines → 0.25 each).
    for w in weights.values():
        assert WEIGHT_FLOOR <= w <= WEIGHT_CEILING
    total = sum(weights.values())
    assert Decimal("0.999") <= total <= Decimal("1.001"), f"weights sum {total}"


def test_bootstrap_allocates_proportional_capital():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    hs = [_hist(e, pnls=[1.0]) for e in svc._engines]
    decisions = svc._decide(hs)
    total = sum(d.allocated_capital for d in decisions)
    # All-active 4-engine equal weights → $10,000 each.
    assert Decimal("39990") <= total <= Decimal("40010"), f"total ${total} drift"


# ── Vol weighting (σ-based, after MIN_AARS_FOR_VOL fills) ────────────────


def test_inverse_vol_when_two_engines_have_enough_history():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    # sigma: low vol (10 swings between -5 and +5).
    sigma_h = _hist("sigma", pnls=[5.0, -5.0] * 25, aar_count=MIN_AARS_FOR_VOL + 5)
    # reversion: high vol (-50 / +50).
    rev_h = _hist("reversion", pnls=[50.0, -50.0] * 25, aar_count=MIN_AARS_FOR_VOL + 5)
    # vector, momentum: bootstrap (insufficient AARs).
    vec_h = _hist("vector", pnls=[1.0])
    mom_h = _hist("momentum", pnls=[1.0])
    decisions = {d.engine: d for d in svc._decide([sigma_h, rev_h, vec_h, mom_h])}
    assert decisions["sigma"].realized_vol is not None
    assert decisions["reversion"].realized_vol is not None
    assert decisions["vector"].realized_vol is None
    assert decisions["momentum"].realized_vol is None
    # Lower-vol engine gets at least as much weight as the higher-vol
    # engine (inverse-vol means smaller σ → bigger 1/σ → bigger weight).
    assert decisions["sigma"].weight >= decisions["reversion"].weight


# ── Floor / Ceiling caps ────────────────────────────────────────────────


def test_weight_cap_prevents_one_engine_dominating():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    # Sigma has near-zero vol → without cap would get ~100%.
    sigma_h = _hist("sigma", pnls=[0.001] * 50, aar_count=MIN_AARS_FOR_VOL + 5)
    others = [_hist(e, pnls=[5.0, -5.0] * 25, aar_count=MIN_AARS_FOR_VOL + 5)
              for e in ("reversion", "vector", "momentum")]
    decisions = {d.engine: d for d in svc._decide([sigma_h] + others)}
    assert decisions["sigma"].weight <= WEIGHT_CEILING + Decimal("0.001"), \
        f"sigma {decisions['sigma'].weight} broke ceiling {WEIGHT_CEILING}"
    # All-others combined still take ≥ 50%.
    other_sum = sum(decisions[e].weight for e in ("reversion", "vector", "momentum"))
    assert other_sum >= Decimal("0.49")


# ── Freeze logic ────────────────────────────────────────────────────────


def test_soft_freeze_at_15pct_drawdown():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    # Construct a history with a 16% drawdown.
    h = _hist("sigma", pnls=[1.0], peak_override=10_000.0, current_override=8_400.0)
    decisions = {d.engine: d for d in svc._decide([h])}
    assert decisions["sigma"].freeze_state == "soft_frozen"
    assert decisions["sigma"].weight == Decimal("0")
    assert decisions["sigma"].allocated_capital == Decimal("0")


def test_hard_freeze_at_25pct_drawdown():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    h = _hist("sigma", pnls=[1.0], peak_override=10_000.0, current_override=7_400.0)
    decisions = {d.engine: d for d in svc._decide([h])}
    assert decisions["sigma"].freeze_state == "hard_frozen"


def test_hard_freeze_from_persistent_soft():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    h = _hist("sigma", pnls=[1.0], peak_override=10_000.0, current_override=8_500.0,
              soft_streak=HARD_FREEZE_SOFT_SESSIONS)
    decisions = {d.engine: d for d in svc._decide([h])}
    assert decisions["sigma"].freeze_state == "hard_frozen"


def test_frozen_engine_capital_redistributes_to_active_engines():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"),
                            engines=("sigma", "reversion"))  # type: ignore[arg-type]
    # Sigma soft-frozen; Reversion active.
    sigma_h = _hist("sigma", pnls=[1.0], peak_override=10_000.0, current_override=8_400.0)
    rev_h = _hist("reversion", pnls=[1.0], peak_override=10_000.0, current_override=10_000.0)
    decisions = {d.engine: d for d in svc._decide([sigma_h, rev_h])}
    # Reversion gets the full eligible pool (clamped to ceiling).
    assert decisions["sigma"].allocated_capital == Decimal("0")
    # Reversion gets ≥ floor; capital ≥ 4000.
    assert decisions["reversion"].allocated_capital >= Decimal("4000")


def test_active_when_drawdown_below_threshold():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    h = _hist("sigma", pnls=[1.0], peak_override=10_000.0, current_override=9_500.0)  # 5% DD
    decisions = {d.engine: d for d in svc._decide([h])}
    assert decisions["sigma"].freeze_state == "active"


# ── Weight normalization ────────────────────────────────────────────────


def test_normalize_and_cap_converges_on_uniform_input():
    out = AllocatorService._normalize_and_cap({"a": Decimal("1"), "b": Decimal("1"),
                                                 "c": Decimal("1"), "d": Decimal("1")})
    assert all(WEIGHT_FLOOR <= v <= WEIGHT_CEILING for v in out.values())
    assert abs(sum(out.values()) - Decimal("1")) <= Decimal("0.0001")


def test_normalize_and_cap_respects_ceiling_with_extreme_weight():
    out = AllocatorService._normalize_and_cap({
        "huge": Decimal("100"),
        "tiny1": Decimal("0.0001"),
        "tiny2": Decimal("0.0001"),
    })
    assert out["huge"] <= WEIGHT_CEILING + Decimal("0.001")


# ── _load_histories via AARReader (covers the tpcore.aar refactor) ──────


from datetime import UTC, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402


class _ConnStub:
    """asyncpg-shaped conn that returns canned rows + risk_state seeds."""

    def __init__(self, aars_by_engine: dict[str, list[dict]], seeds: dict[str, float]) -> None:
        self.aars_by_engine = aars_by_engine
        self.seeds = seeds

    async def fetch(self, sql: str, *args):
        # _load_histories no longer hits aar_events via raw SQL — AARReader
        # owns that. This stub only handles whatever residual queries remain.
        engine = args[0] if args else None
        if "WHERE engine = $1" in sql and "platform.aar_events" in sql:
            return self.aars_by_engine.get(engine, [])
        return []

    async def fetchval(self, sql: str, *args):
        if "engine_equity" in sql:
            return self.seeds.get(args[0])
        return None


class _PoolStub:
    def __init__(self, conn: _ConnStub) -> None:
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_load_histories_uses_aar_reader_and_buckets_by_session() -> None:
    """Confirms _load_histories routes through AARReader and sums PnLs per session."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Three sigma trades on two distinct exit-dates; two on day 0, one on day 1.
    sigma_rows = [
        {
            "engine": "sigma",
            "trade_id": f"T{i}",
            "ticker": "X",
            "aar_data": {
                "pnl_net": str(p),
                "exit_ts": (base + timedelta(days=offset)).isoformat(),
            },
            "recorded_at": base + timedelta(days=offset, hours=i),
        }
        for i, (p, offset) in enumerate([(10, 0), (5, 0), (-3, 1)])
    ]
    conn = _ConnStub({"sigma": sigma_rows}, seeds={"sigma": 10_000.0})

    svc = AllocatorService(
        pool=_PoolStub(conn),  # type: ignore[arg-type]
        platform_capital=Decimal("40000"),
        engines=("sigma",),
    )

    histories = await svc._load_histories()
    assert len(histories) == 1
    sigma = histories[0]
    assert sigma.aar_count == 3
    # Day 0: +10 + 5 = +15; Day 1: -3. So daily_pnls = [15.0, -3.0].
    assert sigma.daily_pnls == [15.0, -3.0]
    # Allocator reconstructs the curve so it ENDS at the current seed
    # ($10k). Start of window = 10_000 - 12 = 9988; day-0 = 10003; day-1 = 10000.
    assert sigma.equity_curve == [10_003.0, 10_000.0]
