"""SP-A — cross-candidate n_trials ledger: unit + contract + integration.

Collected path (``tpcore/tests`` is in pyproject ``testpaths``). The
``scripts/ops.py`` vs ``ops/`` package collision (SP2-T9/T10) is acute
once a test imports ``ops.lab.run``: a non-package ``ops`` cached by an
earlier full-suite test would shadow ``ops.lab.run``. Mirror
``tpcore/tests/test_engine_sdlc_cli.py``: evict any cached non-package
``ops`` at module load and keep every ``ops.lab`` / ``ops`` import
lazy/in-body.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test
# so ``import ops.lab.run`` resolves the real ops/ package.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]


# ── In-memory fake pool: mirrors the append-only data_quality_log
#    contract verbatim — INSERT … ON CONFLICT (source,timestamp) DO
#    NOTHING RETURNING 1, plus the cumulative SUM. The real
#    DataQualityWriter.write SQL (tpcore/quality/data_quality.py:48) is
#    exercised against this; no socket. ──────────────────────────────
class _FakeConn:
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
        s = " ".join(sql.split())
        source, before_ts = params[0], params[1]
        # Plan-fake fix (aligned to the REAL parameterized API, behavior
        # pinned unchanged): cumulative_n_trials binds the ledger source
        # as $1 (the tpcore/supervisor_state.py precedent — never inline
        # a source into SQL), so the namespace assertion checks the bound
        # source param, not the SQL text. The four cumulative equality
        # assertions below remain byte-identical.
        assert "SUM" in s, s
        assert str(source).startswith("lab_trial_ledger."), source
        import json
        total = 0
        for r in self._rows:
            if r["source"] != source or r["timestamp"] >= before_ts:
                continue
            total += int(json.loads(r["notes"])["trials"])
        return total


class _Acquire:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


class _FakePool:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))


async def test_record_trial_spend_writes_one_locked_row():
    from tpcore.lab.ledger import (
        LEDGER_SCHEMA_VERSION,
        ledger_source,
        record_trial_spend,
    )
    pool = _FakePool()
    ts = await record_trial_spend(
        pool, target="reversion", candidate="rev_cand",
        trials=40, seed=7)
    assert isinstance(ts, datetime) and ts.tzinfo is not None
    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row["source"] == ledger_source("reversion") == \
        "lab_trial_ledger.reversion"
    import json
    payload = json.loads(row["notes"])
    assert payload == {
        "schema": LEDGER_SCHEMA_VERSION,
        "target_engine": "reversion",
        "candidate": "rev_cand",
        "trials": 40,
        "seed": 7,
        "run_outcome": "sampled",
    }


async def test_cumulative_sums_only_prior_rows_for_that_target():
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend
    pool = _FakePool()
    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    # 3 reversion runs + 1 vector run, distinct timestamps.
    for i, (tgt, n) in enumerate(
            [("reversion", 40), ("reversion", 50),
             ("vector", 99), ("reversion", 10)]):
        await record_trial_spend(
            pool, target=tgt, candidate=f"c{i}", trials=n, seed=i)
        pool.rows[-1]["timestamp"] = base + timedelta(seconds=i)
    # cumulative for reversion strictly BEFORE base+10s == 40+50+10
    assert await cumulative_n_trials(
        pool, "reversion", base + timedelta(seconds=10)) == 100
    # before the first reversion row → 0
    assert await cumulative_n_trials(pool, "reversion", base) == 0
    # unknown target → 0
    assert await cumulative_n_trials(
        pool, "momentum", base + timedelta(seconds=99)) == 0
    # vector isolated from reversion
    assert await cumulative_n_trials(
        pool, "vector", base + timedelta(seconds=99)) == 99


async def test_notes_payload_shape_is_frozen_schema_1():
    """The notes JSON vocabulary is frozen (schema:1) — a drift fails
    the build, mirroring the supervisor_state schema:1 locked-vocabulary
    discipline. If a field is added/removed/renamed, THIS test must be
    updated in the same commit (an explicit, reviewed contract delta)."""
    import json

    from tpcore.lab.ledger import LEDGER_SCHEMA_VERSION, record_trial_spend
    pool = _FakePool()
    await record_trial_spend(
        pool, target="vector", candidate=None, trials=12, seed=3,
        run_outcome="sampled")
    payload = json.loads(pool.rows[0]["notes"])
    assert set(payload) == {
        "schema", "target_engine", "candidate",
        "trials", "seed", "run_outcome",
    }, f"notes vocabulary drifted: {sorted(payload)}"
    assert payload["schema"] == LEDGER_SCHEMA_VERSION == 1
    assert payload["candidate"] is None  # candidate may be null (legacy/None)
    assert isinstance(payload["trials"], int)
    assert isinstance(payload["seed"], int)
