"""T2 — under an active LabContext the credibility write goes through
``context.credibility_pool`` (the ONE allowlisted RW handle), not a
second ad-hoc ``asyncpg.create_pool`` inside the isolation boundary
(spec §7.2, H-S3-8). The legacy non-Lab path (no active LabContext)
stays byte-identical — it still opens its own pool.

Offline by construction: the heavy walk-forward seams
(``_runner_for`` / ``_context_loader_for`` / ``_context_runner_for``),
``asyncpg.create_pool`` and ``write_credibility_score`` are
monkeypatched exactly as the SP2 characterization oracle does — no DB.
``tpcore.db.build_asyncpg_pool`` is faked so ``LabContext`` yields
tagged in-memory pools without a socket.

Reality-alignment note (executor): the SP2 oracle
(``scripts/tests/test_search_parameters_characterization.py``) does NOT
expose a reusable ``_install_lab_core_stub_harness`` — per the plan's
Step-1 executor note the equivalent stub fakes are inlined here (the
oracle file is NOT modified). ``_run_lab_core`` defaults
``args.output`` to ``backtests/<engine>_search_results.csv`` (a
real ``Path.mkdir`` in the worktree); a ``tmp_path``-scoped output is
used so the offline test never writes into the repo.

H-S3-10: lazy in-body ``ops``/engine imports (the ``scripts/ops.py``
↔ ``ops/`` ``sys.modules`` collision the SP2 T9/T10 work hit)."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

import pytest

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


def _ns(output) -> argparse.Namespace:
    return argparse.Namespace(
        engine="reversion", trials=4, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=0, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.0, credibility_threshold=0,
        universe_tier_max=None,
    )


def _install_lab_core_stub_harness(monkeypatch, lab_run, *, created_pools,
                                   used_pool_tags):
    """Inlined equivalent of the SP2 oracle's offline harness
    (test_amain_smoke_survived_verdict): stub the engine seams so
    ``_run_lab_core`` reaches the credibility-persist block with a
    non-None ``credibility_rubric``, and record which pool the write
    receives + whether a second ad-hoc RW pool is opened."""
    import asyncpg

    class _FakeRubric:
        score = 80

    class _FakeRunResult:
        credibility_score = 80
        credibility_rubric = _FakeRubric()
        trade_log = [_Trade(entry_date=date(2021, 6, 3), pnl_pct=0.02)
                     for _ in range(8)]

    def _fake_ctx_runner(context, *, overrides=None):
        return _FakeRunResult()

    async def _fake_ctx_loader(*a, **k):
        return object()

    async def _fake_runner(*a, **k):
        return _FakeRunResult()

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _fake_ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _fake_ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for",
                        lambda e: _fake_runner)

    async def _fake_write_credibility_score(pool, *, engine_name, score):
        used_pool_tags.append(getattr(pool, "tag", type(pool).__name__))
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write_credibility_score, raising=True)

    class _CreatePool:
        tag = "create_pool"

        async def close(self) -> None: ...

    async def _fake_create_pool(*a, **k):
        created_pools.append("create_pool")
        return _CreatePool()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
                        raising=True)


class _CtxConn:
    """Append-only data_quality_log fake (mirrors the ledger test's
    _FakeConn). T4 routes the SP-A trial-spend emit through the SAME
    allowlisted credibility pool (H-LL-3 reuse — no second RW pool), so
    that pool's stub must now actually model ``acquire()``/``fetchrow``/
    ``fetchval`` — the ledger emit genuinely exercises the pool, unlike
    the monkeypatched ``write_credibility_score`` which never touched
    it. Behaviour-only: the test's pinned assertions are unchanged."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchrow(self, sql, *params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO platform.data_quality_log"):
            source, ts = params[0], params[1]
            notes = params[6]
            if any(r["source"] == source and r["timestamp"] == ts
                   for r in self._rows):
                return None  # ON CONFLICT DO NOTHING
            self._rows.append(
                {"source": source, "timestamp": ts, "notes": notes})
            return {"?column?": 1}
        raise AssertionError(f"unexpected fetchrow SQL: {s}")

    async def fetchval(self, sql, *params):
        import json
        s = " ".join(sql.split())
        assert "SUM" in s, s
        source, before_ts = params[0], params[1]
        total = 0
        for r in self._rows:
            if r["source"] != source or r["timestamp"] >= before_ts:
                continue
            total += int(json.loads(r["notes"])["trials"])
        return total


class _CtxAcquire:
    def __init__(self, conn) -> None:
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _CtxPool:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.rows: list[dict] = []

    def acquire(self):
        return _CtxAcquire(_CtxConn(self.rows))

    async def close(self) -> None: ...


def _patch_context_pools(monkeypatch):
    async def _fake_build_asyncpg_pool(url, *, read_only, **k):
        return _CtxPool("context_read" if read_only
                        else "context_credibility")

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool",
                        _fake_build_asyncpg_pool, raising=True)


async def test_active_labcontext_write_uses_context_pool_no_second_rw_pool(
        monkeypatch, tmp_path):
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    created_pools: list[str] = []
    used_pool_tags: list[str] = []
    _install_lab_core_stub_harness(
        monkeypatch, lab_run,
        created_pools=created_pools, used_pool_tags=used_pool_tags)
    _patch_context_pools(monkeypatch)

    async with LabContext(db_url="postgres://fake/db"):
        await lab_run._run_lab_core(
            _ns(tmp_path / "o.csv"), candidate="rev_cand")

    assert used_pool_tags == ["context_credibility"], (
        f"under an active LabContext the credibility write must use "
        f"context.credibility_pool; saw {used_pool_tags}")
    assert "create_pool" not in created_pools, (
        "a second RW asyncpg.create_pool was opened inside the Lab "
        "isolation boundary — spec §7.2 violated")


async def test_legacy_path_no_labcontext_byte_identical(
        monkeypatch, tmp_path):
    """Regression fence: with NO active LabContext (the legacy
    ``python scripts/search_parameters.py`` operator path) the
    credibility write stays byte-identical — it still opens its own
    ad-hoc ``asyncpg.create_pool`` and the write goes through THAT
    pool, not any context handle."""
    import ops.lab.run as lab_run

    created_pools: list[str] = []
    used_pool_tags: list[str] = []
    _install_lab_core_stub_harness(
        monkeypatch, lab_run,
        created_pools=created_pools, used_pool_tags=used_pool_tags)
    _patch_context_pools(monkeypatch)

    # No `async with LabContext(...)`: candidate=None ⇒ legacy path.
    await lab_run._run_lab_core(_ns(tmp_path / "o.csv"), candidate=None)

    assert used_pool_tags == ["create_pool"], (
        f"the legacy non-Lab path must stay byte-identical (its own "
        f"ad-hoc pool); saw {used_pool_tags}")
    assert created_pools == ["create_pool"], (
        "legacy path must still open exactly its own ad-hoc pool")


async def test_active_labcontext_but_candidate_none_stays_legacy(
        monkeypatch, tmp_path):
    """Belt-and-braces: the threading keys on ``candidate is not None``
    (a Lab run), NOT merely on an active contextvar. If a LabContext is
    active but ``candidate is None`` (not a Lab candidate run) the
    legacy ad-hoc pool path is preserved byte-identical."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    created_pools: list[str] = []
    used_pool_tags: list[str] = []
    _install_lab_core_stub_harness(
        monkeypatch, lab_run,
        created_pools=created_pools, used_pool_tags=used_pool_tags)
    _patch_context_pools(monkeypatch)

    async with LabContext(db_url="postgres://fake/db"):
        await lab_run._run_lab_core(_ns(tmp_path / "o.csv"),
                                    candidate=None)

    assert used_pool_tags == ["create_pool"], (
        f"candidate=None must use the legacy ad-hoc pool even under an "
        f"active LabContext; saw {used_pool_tags}")
    assert created_pools == ["create_pool"]
