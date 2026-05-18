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

import inspect
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


async def test_no_reset_path_monotone_and_conflict_is_dropped_not_doubled():
    """MAKE-OR-BREAK · T-NORESET. The cumulative count is monotone and
    has NO reset entrypoint:

    1. The ledger module's public surface exposes ONLY append
       (``record_trial_spend``) + sum (``cumulative_n_trials``) +
       pure vocabulary helpers — no UPDATE/DELETE/reset/zero function,
       no kwarg that reduces the SUM.
    2. The module source contains no UPDATE/DELETE SQL against
       ``data_quality_log`` and no DELETE/TRUNCATE at all.
    3. Re-emitting the SAME (source, timestamp) is ``ON CONFLICT DO
       NOTHING`` — no error, no double-count (the count stays equal,
       never grows on the dup, never raises).

    H-LL-8 (accepted residual, documented HERE not silently): a
    same-microsecond ``(source, timestamp)`` collision drops one count
    (``ON CONFLICT DO NOTHING``). This is fail-safe toward UNDER-count
    ONLY and is not adversarially reachable — timestamps are
    ``datetime.now(UTC)`` per distinct run; an adversary forcing a
    collision also drops their OWN run's count, which cannot reduce
    their penalty below honest. Accepted; asserted no-error/no-double
    below.
    """
    import tpcore.lab.ledger as ledger
    from tpcore.lab.ledger import (
        cumulative_n_trials,
        record_trial_spend,
    )

    # (1) public surface = append + sum + pure vocabulary only.
    assert set(ledger.__all__) == {
        "LEDGER_SCHEMA_VERSION", "LEDGER_SOURCE_PREFIX",
        "ledger_source", "record_trial_spend", "cumulative_n_trials",
    }
    funcs = {n for n, o in vars(ledger).items()
             if callable(o) and not n.startswith("_")
             and getattr(o, "__module__", "") == ledger.__name__}
    assert funcs == {"ledger_source", "record_trial_spend",
                     "cumulative_n_trials"}, funcs
    for banned in ("reset", "delete", "clear", "zero", "rollback",
                   "decrement", "purge"):
        assert not any(banned in f.lower() for f in funcs), banned
    # record_trial_spend has no kwarg that could lower the SUM.
    sig = inspect.signature(ledger.record_trial_spend)
    assert set(sig.parameters) == {
        "pool", "target", "candidate", "trials", "seed", "run_outcome",
    }

    # (2) no UPDATE/DELETE/TRUNCATE SQL anywhere in the module.
    src = inspect.getsource(ledger).upper()
    assert "UPDATE PLATFORM.DATA_QUALITY_LOG" not in src
    assert "DELETE FROM" not in src
    assert "TRUNCATE" not in src
    assert "ON CONFLICT (SOURCE, TIMESTAMP) DO NOTHING" in (
        # the contract is enforced by DataQualityWriter.write; assert the
        # ledger relies on it (no own-rolled mutable write path).
        inspect.getsource(
            __import__("tpcore.quality.data_quality",
                       fromlist=["DataQualityWriter"]).DataQualityWriter
        ).upper()
    )

    # (3) duplicate (source, timestamp) → dropped, not doubled, no raise.
    pool = _FakePool()
    ts = await record_trial_spend(
        pool, target="reversion", candidate="c", trials=40, seed=0)
    # cumulative AFTER the first spend counts it exactly once.
    after_first = ts + timedelta(microseconds=1)
    cum_one = await cumulative_n_trials(pool, "reversion", after_first)
    assert cum_one == 40
    rows_before = len(pool.rows)

    # Force a GENUINE same-(source, timestamp) collision through the
    # exact write path record_trial_spend uses (DataQualityWriter.write)
    # so the ON CONFLICT DO NOTHING branch is actually exercised:
    # MUST NOT raise, MUST NOT append a row, MUST NOT double-count.
    from decimal import Decimal

    from tpcore.quality.data_quality import (
        DataQualityScore,
        DataQualityWriter,
    )
    dup = DataQualityScore(
        source=ledger.ledger_source("reversion"),
        timestamp=ts,  # SAME (source, timestamp) as the row above
        latency_ms=0,
        missing_bars=0,
        stale=False,
        confidence=Decimal(0),
        notes=pool.rows[0]["notes"],
    )
    wrote = await DataQualityWriter(pool).write(dup)  # no exception
    assert wrote is False  # ON CONFLICT DO NOTHING → no new row
    assert len(pool.rows) == rows_before  # dropped, not appended
    # no double-count: cumulative is unchanged by the dropped collision.
    assert await cumulative_n_trials(pool, "reversion", after_first) == 40

    # (b) monotone: each genuine (distinct-ts) spend only ever grows the
    # cumulative — it is a SUM over an append-only log, never decreases.
    # Distinct, strictly-increasing timestamps are forced (mirroring the
    # cumulative test) so the H-LL-8 same-microsecond drop — proven above
    # — does not perturb the additive-growth assertion here.
    prev = await cumulative_n_trials(pool, "reversion", after_first)
    spaced = ts + timedelta(seconds=10)
    for i, n in enumerate((10, 25, 5)):
        await record_trial_spend(
            pool, target="reversion", candidate="c", trials=n, seed=0)
        pool.rows[-1]["timestamp"] = spaced + timedelta(seconds=i)
        cur = await cumulative_n_trials(
            pool, "reversion", spaced + timedelta(seconds=i, microseconds=1))
        assert cur >= prev, (cur, prev)  # never decreases
        assert cur == prev + n  # strictly additive — no reset path
        prev = cur
