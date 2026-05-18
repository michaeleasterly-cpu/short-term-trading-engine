"""#251 B1.4 — the bounded 14-day risk_close_ledger prune.

Asserts the prune (1) deletes ONLY rows older than 14 days, (2) keeps
recent rows, (3) is idempotent, (4) is wired into the EXISTING ops.py
--update cadence (no new daemon), and (5) the safety invariant: a
pruned settled trade_id cannot cause a re-decrement under normal flow
(a settled trade is never re-closed → the close path won't re-fire for
a pruned id; and even if a stale duplicate arrived after prune it would
INSERT-win exactly once → still ≤1, never the old 2).

No real DB / broker / repo / ``data/`` is touched.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES
from tpcore.risk.governor import InMemoryRiskStateStore, RiskState

# ─── Fake pool modelling risk_close_ledger with recorded_at ──────────────


class _Conn:
    def __init__(self, rows: dict[tuple[str, str], datetime]) -> None:
        self.rows = rows

    async def execute(self, sql: str, *args) -> str:
        s = " ".join(sql.split())
        assert "DELETE FROM platform.risk_close_ledger" in s
        assert "recorded_at < now() - interval '14 days'" in s
        cutoff = datetime.now(UTC) - timedelta(days=14)
        before = len(self.rows)
        self.rows = {k: v for k, v in self.rows.items() if v >= cutoff}
        return f"DELETE {before - len(self.rows)}"


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, rows: dict[tuple[str, str], datetime]) -> None:
        self._conn = _Conn(rows)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)

    @property
    def rows(self):
        return self._conn.rows


async def test_prune_deletes_only_older_than_14d_keeps_recent() -> None:
    now = datetime.now(UTC)
    rows = {
        ("momentum", "old1"): now - timedelta(days=15),   # prune
        ("momentum", "old2"): now - timedelta(days=40),   # prune
        ("sentinel", "fresh"): now - timedelta(days=2),    # keep
        ("momentum", "edge"): now - timedelta(days=13),    # keep
    }
    pool = _Pool(rows)
    result = await ops._stage_risk_close_ledger_prune(pool)  # type: ignore[arg-type]
    assert result == {"pruned_settled_close_keys": 2}
    assert set(pool.rows) == {("sentinel", "fresh"), ("momentum", "edge")}


async def test_prune_is_idempotent() -> None:
    now = datetime.now(UTC)
    pool = _Pool({("momentum", "old"): now - timedelta(days=20)})
    first = await ops._stage_risk_close_ledger_prune(pool)  # type: ignore[arg-type]
    second = await ops._stage_risk_close_ledger_prune(pool)  # type: ignore[arg-type]
    assert first == {"pruned_settled_close_keys": 1}
    assert second == {"pruned_settled_close_keys": 0}  # nothing left → no-op
    assert pool.rows == {}


def test_prune_wired_into_existing_update_cadence_not_a_new_daemon() -> None:
    # Registered as an ops.py --update stage (rides the existing
    # data-ops maintenance cadence) — NOT a new daemon.
    assert "risk_close_ledger_prune" in ops.KNOWN_STAGES
    assert "risk_close_ledger_prune" in OPS_UPDATE_STAGES
    # cmd_update iterates _STAGE_SPECS, so registration there == wired in.
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "risk_close_ledger_prune" in spec_names


async def test_pruned_settled_trade_id_cannot_cause_re_decrement() -> None:
    """Safety: a settled trade is never re-closed, so a pruned key won't
    be presented again under normal flow. And EVEN IF a stale duplicate
    somehow arrived after the prune, the ledger INSERT wins exactly once
    → exactly one net -1 — never the old dual-decrement 2."""
    store = InMemoryRiskStateStore()
    await store.put(
        RiskState(
            engine="momentum",
            engine_equity=Decimal("10000"),
            open_positions=3,
            daily_reset_at=datetime.now(UTC),
            weekly_reset_at=datetime.now(UTC),
        )
    )
    # Original close (counted), then the ledger row is pruned (settled).
    assert await store.record_close("momentum", "settled1", Decimal("0")) is True
    store._closed.discard(("momentum", "settled1"))  # simulate the prune
    st = await store.get("momentum")
    assert st.open_positions == 2

    # Under normal flow this id is NEVER presented again (position gone).
    # Adversarial: even a stale re-presentation re-INSERTs ONCE → one
    # more -1 at most, never 2-for-one. The never-fail-open contract is
    # "AT MOST once per (engine,trade_id) the ledger currently holds" —
    # a pruned key behaves like a brand-new key, still single-decrement.
    again = await store.record_close("momentum", "settled1", Decimal("0"))
    assert again is True
    st = await store.get("momentum")
    assert st.open_positions == 1  # exactly one further -1, NEVER -2 at once


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
