"""Tests for AllocatorService risk_state prune (stale non-live engines).

The allocator is the canonical owner of ``platform.risk_state`` writes.
When an engine is archived (e.g. ``sigma`` on 2026-05-16) it is removed
from the allocator's managed engine set, but its ``risk_state`` row
lingers. ``run_once`` must idempotently DELETE rows for engines NOT in
its managed set, audit-logged via the same ``DBLogHandler`` path as
``ALLOCATOR_REBALANCED``/``ALLOCATOR_SKIPPED``.

All tests use FakePool stand-ins — no live DB required.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from tpcore.allocator.service import AllocatorService, _EngineHistory

# ── Fake pool that records writes + serves a configurable read state ──

# Managed engine set for these tests: sigma is intentionally EXCLUDED so
# its seeded risk_state row is the stale row to be pruned.
_LIVE = ("reversion", "vector", "momentum", "sentinel")


class _FakeConn:
    def __init__(self, fake_pool: _FakePool) -> None:
        self._p = fake_pool

    def transaction(self):
        outer = self

        class _TxCM:
            async def __aenter__(self_inner):
                outer._p.tx_depth += 1
                return self_inner

            async def __aexit__(self_inner, *_):
                outer._p.tx_depth -= 1
                return None

        return _TxCM()

    async def execute(self, sql: str, *args) -> None:
        self._p.executes.append((sql, args))

    async def fetchval(self, sql: str, *args) -> Any:
        sql_lower = sql.lower()
        if "platform.allocations" in sql_lower and "weight" in sql_lower:
            return self._p.prior_weights.get(args[0])
        if "engine_equity" in sql_lower and "risk_state" in sql_lower:
            return None
        return None

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        sql_lower = sql.lower()
        if "ticker = 'spy'" in sql_lower or "ticker='spy'" in sql_lower:
            return self._p.spy_bars
        if "delete from platform.risk_state" in sql_lower:
            # Asserts the prune ran inside the _persist transaction.
            assert self._p.tx_depth > 0, "prune DELETE must run inside transaction"
            # New allowlist semantics: WHERE engine = ANY($1) — args[0] is
            # the explicit archived-engine allowlist; delete ONLY those.
            allowlist = set(args[0])
            pruned = [
                {"engine": e}
                for e in sorted(self._p.risk_state)
                if e in allowlist
            ]
            for row in pruned:
                self._p.risk_state.discard(row["engine"])
            self._p.prune_calls.append((sql, args, [r["engine"] for r in pruned]))
            return pruned
        return []


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(
        self,
        *,
        risk_state: set[str],
        prior_weights: dict[str, Decimal] | None = None,
        spy_bars: list[dict[str, Any]] | None = None,
    ) -> None:
        self.risk_state = set(risk_state)
        self.prior_weights = prior_weights or {}
        self.spy_bars = spy_bars or []
        self.executes: list[tuple] = []
        self.prune_calls: list[tuple] = []
        self.tx_depth = 0
        self.conn = _FakeConn(self)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _spy_bars_trending() -> list[dict[str, Any]]:
    import numpy as np
    n = 30
    base = np.linspace(100.0, 150.0, n)
    return [
        {"date": date(2026, 1, 1), "high": float(b + 0.3),
         "low": float(b - 0.3), "close": float(b)}
        for b in base
    ]


def _make_service(pool: _FakePool) -> AllocatorService:
    return AllocatorService(
        pool=pool,  # type: ignore[arg-type]
        engines=_LIVE,
        platform_capital=Decimal("40000"),
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        as_of=date(2026, 5, 17),
    )


def _empty_histories():
    async def _inner() -> list[_EngineHistory]:
        return [
            _EngineHistory(
                engine=e, aar_count=0, daily_pnls=[], equity_curve=[],
                peak_equity=10_000.0, current_equity=10_000.0,
                soft_frozen_sessions=0,
            )
            for e in _LIVE
        ]
    return _inner


def _prune_log_writes(pool: _FakePool) -> list[tuple]:
    out = []
    for sql, args in pool.executes:
        if "INSERT INTO platform.application_log" not in sql:
            continue
        if args[2] == "ALLOCATOR_PRUNED_RISK_STATE":
            out.append((sql, args))
    return out


async def test_prune_deletes_stale_sigma_and_keeps_live_engines() -> None:
    """A stale ``sigma`` risk_state row is deleted; live rows survive."""
    pool = _FakePool(
        risk_state={"sigma", "reversion", "vector", "momentum", "sentinel"},
        prior_weights={},
        spy_bars=_spy_bars_trending(),
    )
    svc = _make_service(pool)
    svc._load_histories = _empty_histories()  # type: ignore[method-assign]  # noqa: SLF001

    await svc.run_once()

    # sigma removed; the four live engines retained.
    assert pool.risk_state == {"reversion", "vector", "momentum", "sentinel"}
    assert len(pool.prune_calls) == 1
    assert pool.prune_calls[0][2] == ["sigma"]


async def test_prune_emits_single_audit_row_with_sigma_in_payload() -> None:
    """Exactly one ALLOCATOR_PRUNED_RISK_STATE audit row, sigma in payload."""
    pool = _FakePool(
        risk_state={"sigma", "reversion", "vector", "momentum", "sentinel"},
        prior_weights={},
        spy_bars=_spy_bars_trending(),
    )
    svc = _make_service(pool)
    svc._load_histories = _empty_histories()  # type: ignore[method-assign]  # noqa: SLF001

    await svc.run_once()

    prune_logs = _prune_log_writes(pool)
    assert len(prune_logs) == 1
    _, args = prune_logs[0]
    # DBLogHandler INSERT args: (engine, run_id, event_type, severity, message, data)
    assert args[0] == "allocator"
    assert args[2] == "ALLOCATOR_PRUNED_RISK_STATE"
    assert args[3] == "INFO"
    payload = json.loads(args[5])
    assert "sigma" in payload["pruned_engines"]


async def test_prune_is_idempotent_second_run_no_delete_no_audit() -> None:
    """Second run prunes nothing and emits no spurious prune-audit row."""
    pool = _FakePool(
        risk_state={"sigma", "reversion", "vector", "momentum", "sentinel"},
        prior_weights={},
        spy_bars=_spy_bars_trending(),
    )
    svc = _make_service(pool)
    svc._load_histories = _empty_histories()  # type: ignore[method-assign]  # noqa: SLF001

    await svc.run_once()
    assert len(_prune_log_writes(pool)) == 1
    first_prune_calls = len(pool.prune_calls)

    # Second run — clean state, nothing to prune.
    svc2 = _make_service(pool)
    svc2._load_histories = _empty_histories()  # type: ignore[method-assign]  # noqa: SLF001
    await svc2.run_once()

    # The DELETE statement may still execute, but it returns zero rows and
    # MUST NOT emit a second audit row.
    assert pool.risk_state == {"reversion", "vector", "momentum", "sentinel"}
    assert len(_prune_log_writes(pool)) == 1, "no second prune-audit row"
    # The prune DELETE that actually removed rows happened only once.
    rows_pruned = [c for c in pool.prune_calls[first_prune_calls:] if c[2]]
    assert rows_pruned == [], "second run pruned no rows"


def _make_service_production_default(pool: _FakePool) -> AllocatorService:
    """Construct AllocatorService EXACTLY as production does — WITHOUT passing ``engines=``, so ``self._engines`` is the ``__init__`` default (sigma-free since 2026-05-16; the prune path separately targets archived_engines())."""
    return AllocatorService(
        pool=pool,  # type: ignore[arg-type]
        platform_capital=Decimal("40000"),
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        as_of=date(2026, 5, 17),
    )


def _empty_histories_for(engines: tuple[str, ...]):
    async def _inner() -> list[_EngineHistory]:
        return [
            _EngineHistory(
                engine=e, aar_count=0, daily_pnls=[], equity_curve=[],
                peak_equity=10_000.0, current_equity=10_000.0,
                soft_frozen_sessions=0,
            )
            for e in engines
        ]
    return _inner


async def test_production_default_prunes_sigma_keeps_sentinel() -> None:
    """CRITICAL regression: with the PRODUCTION DEFAULT engine set (no
    ``engines=`` kwarg), the prune must delete archived ``sigma`` and
    must NOT delete live ``sentinel`` (sentinel is absent from the
    default ``self._engines``). The buggy ``WHERE engine <> ALL(...)``
    keyed off ``self._engines`` did the exact opposite: kept sigma,
    deleted sentinel.
    """
    pool = _FakePool(
        risk_state={"sigma", "reversion", "vector", "momentum", "sentinel"},
        prior_weights={},
        spy_bars=_spy_bars_trending(),
    )
    svc = _make_service_production_default(pool)
    # The default managed set the allocator upserts rows for.
    svc._load_histories = _empty_histories_for(  # type: ignore[method-assign]  # noqa: SLF001
        ("sigma", "reversion", "vector", "momentum")
    )

    await svc.run_once()

    # sigma (archived) deleted; sentinel (live, NOT in self._engines) survives.
    assert "sigma" not in pool.risk_state, "archived sigma must be pruned"
    assert "sentinel" in pool.risk_state, (
        "CRITICAL: live sentinel must NOT be pruned even though it is "
        "absent from the allocator's default managed engine set"
    )
    assert {"reversion", "vector", "momentum"} <= pool.risk_state
    assert len(pool.prune_calls) == 1
    assert pool.prune_calls[0][2] == ["sigma"]

    # Exactly one prune-audit row, payload lists sigma (not sentinel).
    prune_logs = _prune_log_writes(pool)
    assert len(prune_logs) == 1
    _, args = prune_logs[0]
    payload = json.loads(args[5])
    assert payload["pruned_engines"] == ["sigma"]
    assert "sentinel" not in payload["pruned_engines"]

    # Idempotent: a fresh service on the now-clean table prunes nothing
    # and emits no new prune-audit row.
    svc2 = _make_service_production_default(pool)
    svc2._load_histories = _empty_histories_for(  # type: ignore[method-assign]  # noqa: SLF001
        ("sigma", "reversion", "vector", "momentum")
    )
    await svc2.run_once()
    assert "sentinel" in pool.risk_state
    assert len(_prune_log_writes(pool)) == 1, "no second prune-audit row"
    second = [c for c in pool.prune_calls[1:] if c[2]]
    assert second == [], "second run pruned no rows"


async def test_no_prune_when_no_stale_rows_no_audit() -> None:
    """If risk_state already only has live engines, no audit row at all."""
    pool = _FakePool(
        risk_state={"reversion", "vector", "momentum", "sentinel"},
        prior_weights={},
        spy_bars=_spy_bars_trending(),
    )
    svc = _make_service(pool)
    svc._load_histories = _empty_histories()  # type: ignore[method-assign]  # noqa: SLF001

    await svc.run_once()

    assert pool.risk_state == {"reversion", "vector", "momentum", "sentinel"}
    assert len(_prune_log_writes(pool)) == 0
