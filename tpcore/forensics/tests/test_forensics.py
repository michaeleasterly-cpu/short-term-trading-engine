"""Unit tests for Forensics detectors + service idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tpcore.aar import AARRow
from tpcore.forensics.service import (
    DRAWDOWN_DAYS_THRESHOLD,
    DRAWDOWN_PCT_THRESHOLD,
    LOSS_CLUSTER_K,
    ForensicsService,
    ForensicsTrigger,
    TriggerKind,
    detect_drawdown_period,
    detect_loss_cluster,
    detect_outlier_losses,
)


@pytest.fixture(autouse=True)
def _isolated_sprints_dir(tmp_path, monkeypatch):
    """Redirect SPRINTS_DIR to a per-test tmp_path so persist_trigger's
    dossier write never leaks into the real ``docs/sprints/``. Without
    this, any test that exercises persist_trigger or run() leaves a
    `-sigma-42.md` / `-sigma-99.md` file behind."""
    monkeypatch.setattr("tpcore.forensics.dossier.SPRINTS_DIR", tmp_path)


def _aar(
    pnl: float | str,
    *,
    day_offset: int = 0,
    engine: str = "sigma",
    trade_id: str | None = None,
    ticker: str = "AAPL",
) -> AARRow:
    return AARRow(
        engine=engine,
        trade_id=trade_id or f"{ticker}_{day_offset}",
        ticker=ticker,
        pnl_net=Decimal(str(pnl)),
        exit_ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day_offset),
        entry_ts=None,
        exit_reason=None,
    )


# ── outlier_loss ────────────────────────────────────────────────────────


def test_outlier_loss_returns_empty_when_sample_too_small() -> None:
    aars = [_aar(p, day_offset=i) for i, p in enumerate([100, -1000])]
    assert detect_outlier_losses(aars) == []


def test_outlier_loss_flags_trade_below_threshold() -> None:
    # 10 tight gains around +10, then one big loss. With N=11 the outlier
    # doesn't dominate σ enough to hide itself, so it should clear 3σ.
    pnls = [10] * 10 + [-100]
    aars = [_aar(p, day_offset=i, trade_id=f"T{i}") for i, p in enumerate(pnls)]
    triggers = detect_outlier_losses(aars)
    assert len(triggers) == 1
    assert triggers[0].trigger_kind == TriggerKind.OUTLIER_LOSS
    assert triggers[0].payload["trade_id"] == "T10"
    assert Decimal(triggers[0].payload["pnl_net"]) == Decimal("-100")


def test_outlier_loss_skips_when_stdev_zero() -> None:
    # All identical PnLs → stdev=0 → no sigma to compute.
    aars = [_aar(50, day_offset=i, trade_id=f"T{i}") for i in range(6)]
    assert detect_outlier_losses(aars) == []


# ── loss_cluster ────────────────────────────────────────────────────────


def test_loss_cluster_fires_on_kth_consecutive_loss() -> None:
    pnls = [-10, -20, -30]  # exactly K=3 losses
    aars = [_aar(p, day_offset=i, trade_id=f"T{i}") for i, p in enumerate(pnls)]
    triggers = detect_loss_cluster(aars)
    assert len(triggers) == 1
    assert triggers[0].trigger_kind == TriggerKind.LOSS_CLUSTER
    assert triggers[0].payload["streak_length"] == LOSS_CLUSTER_K
    assert triggers[0].payload["trade_ids"] == ["T0", "T1", "T2"]


def test_loss_cluster_streak_reset_by_winner() -> None:
    # Two losses, a winner, two losses — never reaches K=3.
    pnls = [-10, -20, 50, -30, -40]
    aars = [_aar(p, day_offset=i) for i, p in enumerate(pnls)]
    assert detect_loss_cluster(aars) == []


def test_loss_cluster_refires_at_2k_consecutive() -> None:
    # 6 consecutive losses → fires at trade 3 AND trade 6 (every K-th step).
    pnls = [-10] * 6
    aars = [_aar(p, day_offset=i, trade_id=f"T{i}") for i, p in enumerate(pnls)]
    triggers = detect_loss_cluster(aars)
    assert len(triggers) == 2
    assert triggers[0].payload["streak_length"] == 3
    assert triggers[1].payload["streak_length"] == 6


# ── drawdown_period ─────────────────────────────────────────────────────


def test_drawdown_period_fires_after_threshold_days() -> None:
    # Build equity climb to +100 then sustained decline by 15% of peak
    # for > DRAWDOWN_DAYS_THRESHOLD days.
    aars: list[AARRow] = []
    # Climb to peak=100 by day 0.
    aars.append(_aar(100, day_offset=0, trade_id="climb"))
    # Now lose 20 over a long window — peak=100, equity=80, dd=20%.
    for i in range(DRAWDOWN_DAYS_THRESHOLD + 2):
        aars.append(_aar(-1, day_offset=1 + i, trade_id=f"dd{i}"))
    triggers = detect_drawdown_period(aars)
    assert len(triggers) >= 1
    t = triggers[0]
    assert t.trigger_kind == TriggerKind.DRAWDOWN_PERIOD
    assert float(t.payload["drawdown_pct"]) >= float(DRAWDOWN_PCT_THRESHOLD)


def test_drawdown_period_no_fire_when_recovery_fast() -> None:
    # Climb to 100, dip to 90 (10%), recover by day 5 — no sustained DD.
    aars = [
        _aar(100, day_offset=0, trade_id="peak"),
        _aar(-5, day_offset=1, trade_id="d1"),
        _aar(-5, day_offset=2, trade_id="d2"),
        _aar(20, day_offset=3, trade_id="recover"),
    ]
    assert detect_drawdown_period(aars) == []


def test_drawdown_period_no_fire_when_peak_nonpositive() -> None:
    # Net negative from trade 1 — % calc is meaningless without a real peak.
    aars = [_aar(-1, day_offset=i, trade_id=f"T{i}") for i in range(20)]
    assert detect_drawdown_period(aars) == []


def test_drawdown_period_fires_once_per_peak() -> None:
    # Sustained drawdown shouldn't fire every day for the same peak.
    aars: list[AARRow] = [_aar(100, day_offset=0, trade_id="peak")]
    for i in range(DRAWDOWN_DAYS_THRESHOLD + 10):
        aars.append(_aar(-1, day_offset=1 + i, trade_id=f"d{i}"))
    triggers = detect_drawdown_period(aars)
    assert len(triggers) == 1


# ── ForensicsService.persist_trigger idempotency ───────────────────────


class _FakeConn:
    """Fake conn for the Plan-2 dql_store path (data_quality_log
    kind='forensics_trigger'). The EXISTS check binds
    (kind_const, trigger_kind, fingerprint); the INSERT binds
    (kind_const, source, fired_at, notes_json)."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, str]] = []
        self.inserted: list[tuple[str, str]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO platform.data_quality_log" in sql:
            # args = (kind_const, source, fired_at, notes_json)
            self.inserted.append((args[1], args[3]))
            return 42
        # EXISTS check: args = (kind_const, trigger_kind, fingerprint).
        _kind_const, trigger_kind, fingerprint = args
        if (trigger_kind, fingerprint) in self.fired:
            return 1
        self.fired.append((trigger_kind, fingerprint))
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        # set_dossier_path UPDATE — no-op for the idempotency test.
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_persist_trigger_skips_when_fingerprint_already_fired() -> None:
    conn = _FakeConn()
    service = ForensicsService(pool=_FakePool(conn))  # type: ignore[arg-type]
    trigger = ForensicsTrigger(
        trigger_kind=TriggerKind.OUTLIER_LOSS,
        engine="sigma",
        fingerprint="sigma|YUMC_x",
        payload={"fingerprint": "sigma|YUMC_x"},
    )
    first = await service.persist_trigger(trigger)
    second = await service.persist_trigger(trigger)
    assert first == "42"  # uuid id stringified (here the fake returns 42)
    assert second is None
    # Only one INSERT, despite two persist calls.
    assert len(conn.inserted) == 1


# ── error handling / self-heal in run() ─────────────────────────────────


class _FailingReader:
    def __init__(self, by_engine: dict[str, list[AARRow]]) -> None:
        self.by_engine = by_engine

    async def fetch_all_grouped(self) -> dict[str, list[AARRow]]:
        return self.by_engine


class _FlakyConn:
    """Persist call fails the first time, succeeds the second — simulates
    a transient DB blip that should NOT stop the rest of the loop."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO platform.data_quality_log" in sql:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection reset by peer")
            return 99
        # EXISTS check: always say "doesn't exist" so persist is tried.
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_run_continues_when_single_persist_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failed INSERT must not stop subsequent triggers from being persisted."""
    aars = [
        AARRow(
            engine="sigma",
            trade_id=f"T{i}",
            ticker="X",
            pnl_net=Decimal("-10"),
            exit_ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
            entry_ts=None,
            exit_reason=None,
        )
        for i in range(6)
    ]
    conn = _FlakyConn()
    service = ForensicsService(pool=_FakePool(conn))  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_reader", _FailingReader({"sigma": aars}))

    counts = await service.run()
    # 6 consecutive losses → loss_cluster fires at trade 3 and trade 6.
    # First persist raises; second succeeds. So we expect 1 inserted out
    # of 2 detected, and the service did NOT raise.
    assert sum(counts.values()) == 1
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_run_returns_zero_when_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the AAR fetch itself fails (DB down), return zero-counts and don't raise."""
    conn = _FakeConn()
    service = ForensicsService(pool=_FakePool(conn))  # type: ignore[arg-type]

    class _BrokenReader:
        async def fetch_all_grouped(self) -> dict[str, list[AARRow]]:
            raise RuntimeError("pool acquire timeout")

    monkeypatch.setattr(service, "_reader", _BrokenReader())
    counts = await service.run()
    assert all(v == 0 for v in counts.values())
    assert len(conn.inserted) == 0
