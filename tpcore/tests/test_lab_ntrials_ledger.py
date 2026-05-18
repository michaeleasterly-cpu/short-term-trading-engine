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

import argparse
import inspect
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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


# ── SP-A T4 (T-MONO) — _run_lab_core emit-at-sample + cumulative read +
#    monotone-harder. The offline harness is inlined here verbatim per
#    the SP2/SP3 precedent (the SP2 oracle exports no reusable harness;
#    the threaded-lab test inlines its own — we do the same so the
#    oracle file stays unmodified). ─────────────────────────────────────


@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


def _ns(output, *, trials=40, seed=0):
    return argparse.Namespace(
        engine="reversion", trials=trials, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None,
    )


class _SharedLedgerPool:
    """One in-memory data_quality_log shared across simulated runs —
    the cross-run memory the ledger relies on. Mirrors the append-only
    + SUM contract (same as _FakePool but reusable across runs)."""
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))

    async def close(self) -> None: ...


def _install_offline_harness(monkeypatch, lab_run, *, returns,
                             cred_score=80):
    """Stub the heavy engine seams so ``_run_lab_core`` reaches the DSR
    code with a fixed held-period-returns slice and a non-None
    credibility rubric — exactly the offline pattern
    ``tpcore/tests/test_lab_credibility_pool_threaded.py`` uses (the
    SP2 oracle exposes no reusable harness; inline our own; the oracle
    file is NOT modified)."""
    class _Rubric:
        score = cred_score

    class _RunResult:
        credibility_score = cred_score
        credibility_rubric = _Rubric()
        # one trade per return on a distinct in-window entry_date →
        # period_returns_from_trades == returns (grouping is by
        # entry_date; distinct dates ⇒ no period collapse). timedelta
        # arithmetic (NOT date(2022, 1, 3 + i) — day would overflow
        # past 31). All dates land inside [2022-01-01, 2022-12-31] for
        # ≤ ~360 returns.
        trade_log = [
            _Trade(
                entry_date=date(2022, 1, 3) + timedelta(days=i),
                pnl_pct=r,
            )
            for i, r in enumerate(returns)
        ]

    def _ctx_runner(context, *, overrides=None):
        return _RunResult()

    async def _ctx_loader(*a, **k):
        return object()

    async def _runner(*a, **k):
        return _RunResult()

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    async def _fake_write_cred(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write_cred, raising=True)


async def test_second_candidate_same_target_gets_strictly_larger_n_trials(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-MONO. Two Lab runs against the SAME target on a
    shared ledger: run 2's n_trials fed to compute_dsr_for_verdict is
    strictly greater than run 1's, cumulative grows, and (fixed returns)
    DSR(run2) <= DSR(run1) — the gate is monotone-harder by construction
    (per-target keying, H-LL-2)."""
    # A moderately strong fixed return slice (same for both runs).
    import numpy as np

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    rng = np.random.default_rng(0)
    returns = [float(x) for x in rng.normal(0.015, 0.01, 40)]

    seen_n_trials: list[int] = []
    real_dsr = lab_run.compute_dsr_for_verdict

    def _spy_dsr(r, *, n_trials):
        seen_n_trials.append(n_trials)
        return real_dsr(r, n_trials=n_trials)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy_dsr)
    _install_offline_harness(monkeypatch, lab_run, returns=returns)

    shared = _SharedLedgerPool()

    async def _fake_build(url, *, read_only, **k):
        # LabContext's read_pool + credibility_pool both resolve to the
        # shared in-memory ledger pool (the credibility pool is the RW
        # handle the ledger emit reuses — H-LL-3).
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        core1 = await lab_run._run_lab_core(
            _ns(tmp_path / "r1.csv", trials=40, seed=1),
            candidate="rev_cand_a")
    async with LabContext(db_url="postgres://fake/db"):
        core2 = await lab_run._run_lab_core(
            _ns(tmp_path / "r2.csv", trials=50, seed=2),
            candidate="rev_cand_b")

    # Both reached the DSR call.
    assert not isinstance(core1, int) and not isinstance(core2, int)
    assert len(seen_n_trials) == 2
    # Run 1: cumulative(0) + 40 == 40. Run 2: cumulative(40) + 50 == 90.
    assert seen_n_trials[0] == 40
    assert seen_n_trials[1] == 90
    assert seen_n_trials[1] > seen_n_trials[0]      # strictly larger
    assert core2.effective_n_trials == 90           # carried on the spine
    assert core1.effective_n_trials == 40
    # Monotone-harder: more trials ⇒ DSR no higher on identical returns.
    assert core2.dsr <= core1.dsr


def test_run_py_ledger_callsite_is_append_only_no_reset():
    """CARRY-FORWARD from the T3 review (closes the structural
    blind-spot the T3 reviewer flagged for "T4's reviewer to note").

    T3's T-NORESET source-scan is module-scoped to
    ``tpcore/lab/ledger.py`` ONLY. A raw DELETE/UPDATE against
    ``data_quality_log`` / ``lab_trial_ledger.*`` added to
    ``ops/lab/run.py`` would silently defeat the ledger and T3 would
    NOT catch it. This extends the append-only / no-reset guarantee to
    the run.py call-site (mirroring T3's T-NORESET scan technique,
    extended to the run.py module):

    1. ``ops/lab/run.py`` contains NO DELETE/TRUNCATE of, and NO UPDATE
       against, ``data_quality_log`` and NO ``lab_trial_ledger`` raw
       SQL — run.py reaches the ledger ONLY via the two append-only
       helpers.
    2. The ONLY ledger names run.py references are the two T1 helpers
       (``record_trial_spend`` for the append, ``cumulative_n_trials``
       for the SUM read) — never any reset/delete/clear entrypoint
       (there is none on the ledger surface anyway — T3 (1) — but this
       pins run.py introduces no out-of-band one either).
    """
    import ops.lab.run as lab_run

    src = inspect.getsource(lab_run)
    up = src.upper()

    # (1) no mutable/reset SQL against the ledger substrate in run.py.
    assert "DELETE FROM" not in up
    assert "TRUNCATE" not in up
    assert "UPDATE PLATFORM.DATA_QUALITY_LOG" not in up
    # run.py never names the ledger source / table in raw SQL — it goes
    # only through the helpers (which own the disjoint namespace).
    assert "LAB_TRIAL_LEDGER" not in up

    # (2) the ledger is reached ONLY through the two append-only helpers.
    assert "record_trial_spend" in src
    assert "cumulative_n_trials" in src
    for banned in ("reset_trial", "delete_trial", "clear_trial",
                   "zero_trial", "purge_trial", "rollback_trial",
                   "decrement_trial", "_reset_ledger", "_clear_ledger"):
        assert banned not in src, banned


# ── SP-A T5 (T-CUM) — cumulative fails where per-run survived. The proof
#    that the anti-laundering mechanism actually BITES: a candidate that
#    clears DSR>=0.95 at the per-run trial count is correctly FAILED once
#    the cumulative prior-fishing penalty is applied. The FAILED verdict
#    is the honest outcome (spec §5); reverting it to per-run is the bug
#    H-LL-7 forbids. ──────────────────────────────────────────────────


async def test_cumulative_fails_where_per_run_would_have_survived(
        monkeypatch, tmp_path):
    """MAKE-OR-BREAK · T-CUM. A candidate that SURVIVES under the
    per-run trial count but FAILS once the cumulative penalty is
    applied. The FAILED verdict is the CORRECT, HONEST outcome — the
    edge only "passed" because the multiple-testing penalty was being
    laundered across small runs.

    DO NOT "fix" this back to per-run n_trials. SP-A makes the gate
    *correctly harder*; it does NOT weaken DSR>=0.95 / cred>=60 /
    n_trades>=3 (those thresholds are byte-identical — see T6). If this
    test "fails" because someone reverted _run_lab_core to
    n_trials=args.trials, the bug is the revert, not this test (spec
    §5 / H-LL-7)."""
    import numpy as np

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    # Choose a returns slice + (per_run, prior_cumulative) such that the
    # DSR crosses 0.95 between the per-run penalty and the cumulative
    # one. Verified directly against the real compute_dsr_for_verdict
    # (this exact slice: d_per_run(25)=1.0000 SURVIVES; d_cumulative
    # (5025)=0.8962 FAILS) so the pin is not hand-waved. A high-Sharpe,
    # low-noise slice is required — ordinary noisy normal slices never
    # clear 0.95 under any n_trials, so the contrast would be vacuous.
    rng = np.random.default_rng(11)
    returns = [float(x) for x in rng.normal(0.015, 0.004, 60)]
    per_run = 25
    prior_cumulative = 5000  # a lot of prior fishing against the target
    d_per_run = lab_run.compute_dsr_for_verdict(returns, n_trials=per_run)
    d_cumulative = lab_run.compute_dsr_for_verdict(
        returns, n_trials=prior_cumulative + per_run)
    # The pin (asserts the constructed scenario is the right shape; if a
    # future numpy/formula change moves these, retune returns/counts in
    # THIS test only — never the gate threshold).
    assert d_per_run >= 0.95, (
        f"scenario invalid: per-run DSR {d_per_run} must SURVIVE 0.95")
    assert d_cumulative < 0.95, (
        f"scenario invalid: cumulative DSR {d_cumulative} must FAIL 0.95")

    # Seed the shared ledger with prior_cumulative trials of prior
    # fishing against reversion, then run THIS candidate.
    shared = _SharedLedgerPool()
    from tpcore.lab.ledger import record_trial_spend
    seeded_ts = await record_trial_spend(
        shared, target="reversion", candidate="prior_fishing",
        trials=prior_cumulative, seed=99)
    # Force the seeded row strictly BEFORE this run's spend.
    shared.rows[-1]["timestamp"] = seeded_ts

    _install_offline_harness(monkeypatch, lab_run, returns=returns,
                             cred_score=80)

    async def _fake_build(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fake_build,
                        raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / "cum.csv", trials=per_run, seed=3),
            candidate="rev_cand")

    assert not isinstance(core, int)
    assert core.effective_n_trials == prior_cumulative + per_run
    # cred=80 >= 60, n_trades=48 >= 3 — so survival hinges PURELY on DSR.
    # Per-run DSR would have SURVIVED; cumulative DSR FAILS → correct.
    assert core.survived is False, (
        "cumulative penalty must make this FAIL — the honest behaviour "
        "(spec §5). If this asserts True, _run_lab_core regressed to "
        "per-run n_trials.")
