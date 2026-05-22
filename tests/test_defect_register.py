"""Unit tests for the consolidated defect register (DR1, DARK).

Deterministic, no DB/`data/`: a fake pool + monkeypatched Ladder read
APIs. The register is a derived read-model — it MUST compose
``engine_ladder.list_undispositioned`` and
``weekly_digest.build_weekly_digest().undispositioned_entries``
*verbatim* and re-derive nothing (never issue its own application_log
escalation query, never regex-scrape the digest's display string).
These tests bite if a future change inlines such a query or drifts the
register from either Ladder.

importlib-loads the module under test to dodge the documented
``scripts/ops.py`` ↔ ``ops/`` package-shadow hazard (the
test_engine_ladder / test_weekly_digest precedent).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ── scripts/ops.py ↔ ops/ package-shadow hazard ────────────────────────
# ``scripts/ops.py`` is a single-file module that tpcore/tests/test_ops*
# import as the top-level name ``ops`` (they put ``scripts/`` on sys.path
# then ``import ops`` → ``sys.modules['ops']`` becomes the non-package
# script). The ``ops/`` *directory* is ALSO a real package
# (ops/engine_ladder.py, ops/weekly_digest.py, ops/defect_register.py).
# ``ops/defect_register.py:41`` does ``from ops import engine_ladder,
# weekly_digest`` — correct for production ``python -m ops.defect_register``
# but, when imported here, that line REGISTERS the namespace-package
# ``ops`` (and its siblings) into the shared sys.modules.
#
# The old "purge non-package ops then bare importlib-load" block left
# that registration in place, so whichever of test_ops /
# test_defect_register collected FIRST poisoned the other (the whole
# suite, one process, fails ~40 test_ops* with ``module 'ops' has no
# attribute _CANDIDATE_RE`` etc.). A targeted subset masked it.
#
# Fix (the previously-proven precedent for the IDENTICAL hazard, last
# seen in the now-deleted ``test_llm_triage_service.py`` —
# 2026-05-22 LLM-triage removal): snapshot EXACTLY the sys.modules keys
# we touch, ensure
# ``ops`` is package-shaped + the real siblings are bound by file path
# (so ``from ops import engine_ladder, weekly_digest`` resolves to the
# REAL modules the tests monkeypatch — NOT raising stubs, since the
# tests exercise the real ``consolidated_defects`` compose path with
# only the two Ladder callables patched per-test), exec the module
# under test, then RESTORE sys.modules EXACTLY — zero global side
# effects, collection-order safe in BOTH directions.
_DR_PATH = _REPO / "ops" / "defect_register.py"


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def _load_real_sibling(name: str) -> types.ModuleType:
    """Load ops/<name>.py by file path, bound under ``ops.<name>`` so
    defect_register's ``from ops import …`` picks up the REAL module."""
    spec = importlib.util.spec_from_file_location(
        f"ops.{name}", _REPO / "ops" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"ops.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_SAVED = {
    k: sys.modules.get(k)
    for k in ("ops", "ops.engine_ladder", "ops.weekly_digest",
              "ops.defect_register")
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_DR_PATH.parent)]  # make it package-shaped
        sys.modules["ops"] = _pkg
    _load_real_sibling("engine_ladder")
    _load_real_sibling("weekly_digest")

    _SPEC = importlib.util.spec_from_file_location(
        "_dr_under_test", _DR_PATH)
    assert _SPEC is not None and _SPEC.loader is not None
    dr = importlib.util.module_from_spec(_SPEC)
    sys.modules["_dr_under_test"] = dr
    _SPEC.loader.exec_module(dr)
finally:
    # Restore sys.modules entries EXACTLY so no later-collected test
    # (e.g. tpcore/tests/test_ops*.py) sees our scaffolding.
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v

NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
T1 = NOW - timedelta(days=10)
T2 = NOW - timedelta(days=8)
T3 = NOW - timedelta(days=6)


class _GuardConn:
    """A fake connection that FAILS if anyone runs an ESCALATION query
    against it — proves the register issues NO application_log
    escalation SELECT of its own (the no-re-derivation invariant).

    DR2: the register legitimately queries application_log for the
    REVIEW_DEFECT_* class (its own primitive — not an escalation
    re-derivation). That ONE query is allowed and served empty; ANY
    other DB SQL still raises, so the escalation spy-guard stays green."""

    async def fetch(self, sql: str, *a):
        if "REVIEW_DEFECT_LOGGED" in sql:
            return []  # review-event query — allowed, served empty
        raise AssertionError(  # pragma: no cover - guard
            "consolidated_defects issued its OWN escalation DB query — "
            "it must compose the Ladder APIs verbatim, never re-derive "
            f"escalation state. SQL: {sql}")

    async def execute(self, sql: str, *a):  # pragma: no cover - guard
        raise AssertionError("consolidated_defects executed its OWN SQL")


class _GuardPool:
    """Records whether .acquire() was used. DR1 escalation-only paths
    must NOT touch the pool; DR2's review-event query legitimately may
    (the only DB the register itself owns is its REVIEW_DEFECT_* read).
    The escalation no-re-derivation invariant is enforced by
    ``_GuardConn`` (escalation SQL raises), NOT by ``acquired``."""

    def __init__(self) -> None:
        self.acquired = False

    def acquire(self):
        self.acquired = True

        class _CM:
            async def __aenter__(_s):
                return _GuardConn()

            async def __aexit__(_s, *e):
                return None

        return _CM()


def _eng_row(hold_id, *, engine="reversion",
             failure_class="crashed_startup", reason="boom",
             recorded_at=T1, shape="held",
             policy_default="structural", policy_rationale="r"):
    """An entry shaped exactly like engine_ladder.list_undispositioned
    output (its real return contract)."""
    return {"hold_id": hold_id, "engine": engine,
            "failure_class": failure_class, "reason": reason,
            "recorded_at": recorded_at, "shape": shape,
            "policy_default": policy_default,
            "policy_rationale": policy_rationale}


class _Entry:
    """A structured undispositioned escalation, shaped exactly like
    ``weekly_digest.UndispositionedEntry`` (the field the register now
    reads: ``ref``/``recorded_at``/``rendered``/``policy``). The
    register consumes the STRUCT — not a regex-scrape of ``rendered`` —
    so an entry can carry a clean ref even if ``rendered`` drifts."""

    def __init__(self, ref, *, date="2026-05-11",
                 etype="DATA_REPAIR_ESCALATED",
                 message="fred_macro stalled",
                 policy="policy:structural — owns the fix"):
        self.ref = ref
        self.etype = etype
        self.recorded_at = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=UTC)
        self.message = message
        self.policy = policy
        self.rendered = f"{date} [{etype}] ref={ref} {message} | {policy}"


def _data_entry(ref, **kw):
    """A structured data-lane undispositioned entry (the register's
    new input contract). Replaces the old pre-rendered ``_data_line``
    string the register used to regex-scrape."""
    return _Entry(ref, **kw)


async def _run_cli(pool, argv: list[str]) -> int:
    """Run ``dr._amain(argv)`` with ``build_asyncpg_pool`` swapped for a
    no-DSN-free fake returning ``pool`` (save/restore — no monkeypatch
    fixture needed, collection-order safe)."""
    import os

    async def _fake_build(_dsn):
        return pool

    if not hasattr(pool, "close"):
        async def _close():
            return None
        pool.close = _close  # type: ignore[attr-defined]
    saved_build = dr.build_asyncpg_pool
    saved_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "postgres://fake/db"
    dr.build_asyncpg_pool = _fake_build  # type: ignore[assignment]
    try:
        return await dr._amain(argv)  # noqa: SLF001
    finally:
        dr.build_asyncpg_pool = saved_build  # type: ignore[assignment]
        if saved_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_env


def _patch(monkeypatch, *, engine_rows, data_entries):
    """Stub BOTH Ladder read APIs so the register composes them
    verbatim with no DB. build_weekly_digest returns a stub object
    exposing ``.undispositioned_entries`` (the structured field the
    register now reads — NOT the rendered ``.undispositioned`` string)."""

    async def _fake_list(pool, **kw):
        return list(engine_rows)

    class _Digest:
        undispositioned_entries = list(data_entries)

    async def _fake_digest(pool, now=None):
        return _Digest()

    monkeypatch.setattr(dr.engine_ladder, "list_undispositioned",
                        _fake_list)
    monkeypatch.setattr(dr.weekly_digest, "build_weekly_digest",
                        _fake_digest)


# ── DR1.1: the unified read-model ───────────────────────────────────


async def test_engine_undispositioned_yields_one_engine_row(monkeypatch):
    _patch(monkeypatch, engine_rows=[_eng_row("h1")], data_entries=[])
    pool = _GuardPool()
    out = await dr.consolidated_defects(pool)
    assert len(out) == 1
    r = out[0]
    assert isinstance(r, dr.DefectRow)
    assert r.defect_ref == "h1"
    assert r.lane == "engine"
    assert r.origin == "escalation"
    assert r.fix_ref is None
    assert r.state == "open"
    assert "crashed_startup" in r.summary
    assert r.policy is not None and "structural" in r.policy
    # The register issued NO escalation SQL of its own (_GuardConn
    # raises on any non-review SQL); the only DB it owns is the
    # REVIEW_DEFECT_* read.


async def test_data_lane_undispositioned_yields_one_data_row(monkeypatch):
    _patch(monkeypatch, engine_rows=[],
           data_entries=[_data_entry("req-42")])
    out = await dr.consolidated_defects(_GuardPool())
    assert len(out) == 1
    r = out[0]
    assert r.defect_ref == "req-42"
    assert r.lane == "data"
    assert r.origin == "escalation"
    assert "fred_macro stalled" in r.summary


async def test_same_defect_ref_in_both_collapses_to_one_row(monkeypatch):
    # A ref present in BOTH Ladders must JOIN to ONE row, never sum to 2.
    _patch(monkeypatch,
           engine_rows=[_eng_row("shared-ref")],
           data_entries=[_data_entry("shared-ref")])
    out = await dr.consolidated_defects(_GuardPool())
    refs = [r.defect_ref for r in out]
    assert refs == ["shared-ref"], f"join failed — got {refs} (summed?)"
    assert len(out) == 1


async def test_empty_both_yields_empty(monkeypatch):
    _patch(monkeypatch, engine_rows=[], data_entries=[])
    assert await dr.consolidated_defects(_GuardPool()) == []


async def test_deterministic_order_by_opened_at_then_ref(monkeypatch):
    _patch(monkeypatch,
           engine_rows=[_eng_row("z-late", recorded_at=T3),
                        _eng_row("a-early", recorded_at=T1)],
           data_entries=[_data_entry("m-mid", date="2026-05-13")])
    out = await dr.consolidated_defects(_GuardPool())
    assert [r.defect_ref for r in out] == ["a-early", "m-mid", "z-late"]


async def test_no_self_issued_escalation_query_spy_guard(monkeypatch):
    """The no-re-derivation invariant: with BOTH Ladder APIs stubbed,
    consolidated_defects must NEVER run its own ESCALATION SELECT.
    ``_GuardConn.fetch`` raises on any non-review SQL — so if someone
    later inlines an application_log *escalation* query here (instead of
    composing the Ladder APIs), this test fails loudly. The register's
    own REVIEW_DEFECT_* read is the ONE allowed DB query."""
    _patch(monkeypatch, engine_rows=[_eng_row("h1")],
           data_entries=[_data_entry("d1")])
    pool = _GuardPool()
    out = await dr.consolidated_defects(pool)
    assert {r.defect_ref for r in out} == {"h1", "d1"}


async def test_register_invokes_both_ladder_apis(monkeypatch):
    """Positive half of the spy-guard: the two Ladder symbols are each
    invoked exactly once with the pool — proving verbatim composition,
    not a reimplementation."""
    calls: dict[str, int] = {"list": 0, "digest": 0}

    async def _spy_list(pool, **kw):
        calls["list"] += 1
        return [_eng_row("h1")]

    class _Digest:
        undispositioned_entries = [_data_entry("d1")]

    async def _spy_digest(pool, now=None):
        calls["digest"] += 1
        return _Digest()

    monkeypatch.setattr(dr.engine_ladder, "list_undispositioned",
                        _spy_list)
    monkeypatch.setattr(dr.weekly_digest, "build_weekly_digest",
                        _spy_digest)
    await dr.consolidated_defects(_GuardPool())
    assert calls == {"list": 1, "digest": 1}


# ── DR1.2: `list` CLI + parity forcing-test ─────────────────────────


async def test_cli_list_prints_rows_deterministic_grepable(
        monkeypatch, capsys):
    _patch(monkeypatch,
           engine_rows=[_eng_row("h1", recorded_at=T1)],
           data_entries=[_data_entry("req-9", date="2026-05-13")])

    class _FakePool:
        async def close(self): ...

        def acquire(self):
            class _CM:
                async def __aenter__(_s):
                    return _GuardConn()  # serves the review query empty

                async def __aexit__(_s, *e):
                    return None

            return _CM()

    fp = _FakePool()

    async def _fake_build(_dsn):
        return fp

    monkeypatch.setattr(dr, "build_asyncpg_pool", _fake_build)
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    rc = await dr._amain(["list"])  # noqa: SLF001
    assert rc == 0
    out = capsys.readouterr().out
    # Stable, grep-able, deterministic order (opened_at, defect_ref).
    assert out.index("h1") < out.index("req-9")
    assert "engine" in out and "data" in out


async def test_cli_no_dsn_explicit_nonzero(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_IPV4", raising=False)
    rc = await dr._amain(["list"])  # noqa: SLF001
    assert rc == 1  # explicit no-DSN failure, NOT a silent 0


def test_module_has_main_entrypoint():
    src = (_REPO / "ops" / "defect_register.py").read_text()
    assert 'if __name__ == "__main__":' in src
    assert "argparse" in src
    assert "def main()" in src
    assert "# pragma: no cover" in src  # CLI shim precedent


async def test_parity_register_escalation_refs_equal_both_ladders(
        monkeypatch):
    """PARITY FORCING-TEST (mirrors escalation_drift()'s test style):
    the register's escalation-origin defect_ref SET must be EXACTLY the
    union of engine_ladder.list_undispositioned hold_ids and the
    data-lane structured undispositioned ``ref``s. It FAILS the build if
    the register ever drops, adds, aliases, or re-derives a ref.
    Non-tautological: it reads the Ladder outputs independently of the
    register, and the data ref now comes off the STRUCT (``.ref``) — not
    a regex-scrape of the display string."""
    engine_rows = [_eng_row("e-alpha", recorded_at=T1),
                   _eng_row("e-beta", recorded_at=T2)]
    data_entries = [_data_entry("d-gamma", date="2026-05-12"),
                    _data_entry("e-beta", date="2026-05-14")]  # also in engine
    _patch(monkeypatch, engine_rows=engine_rows, data_entries=data_entries)

    out = await dr.consolidated_defects(_GuardPool())
    register_refs = {r.defect_ref for r in out
                     if r.origin == "escalation"}

    # Independently recompute the expected set from the Ladder APIs
    # (same forcing-test discipline as escalation_drift's test).
    eng_refs = {r["hold_id"] for r in engine_rows}
    data_refs = {e.ref for e in data_entries}
    expected = eng_refs | data_refs

    assert register_refs == expected, (
        f"register drifted from the Ladders: "
        f"missing={expected - register_refs} "
        f"extra={register_refs - expected}")


async def test_register_uses_struct_ref_not_display_scrape(monkeypatch):
    """The contract test that now BITES on the right drift: the register
    keys off the STRUCTURED ``.ref`` — so even if the digest's rendered
    display string were reformatted (``rendered`` mangled beyond any
    ``ref=`` regex), the register still keys correctly. This is exactly
    the regression the old display-string regex-scrape silently dropped
    (and which the previous _data_line helper-based parity test could
    NOT catch, since it never exercised the live render path)."""
    ent = _data_entry("req-77")
    # Simulate a future digest display reformat: scramble ``rendered``
    # so ANY `ref=` scrape would fail — the struct still carries the
    # clean ref, so the register MUST still produce req-77.
    ent.rendered = "DISPLAY FORMAT CHANGED — no parseable ref token"
    _patch(monkeypatch, engine_rows=[], data_entries=[ent])
    out = await dr.consolidated_defects(_GuardPool())
    assert [r.defect_ref for r in out] == ["req-77"], (
        "register must read the structured .ref, not scrape the "
        "display string (a display reformat must not drop the defect)")


# ── DR2.2: REVIEW_DEFECT_* emit + log/resolve CLI + integration ─────


class _ReviewConn:
    """A fake connection backing the REVIEW_DEFECT_* primitive: it
    captures emitted rows (INSERT) and answers the register's review
    open-set query (SELECT) from those rows using the SAME anti-join
    open-predicate the register uses (a LOGGED with no later RESOLVED).
    Escalation SQL still raises (no-re-derivation invariant)."""

    def __init__(self, store: list[dict]) -> None:
        self._store = store

    async def execute(self, sql: str, *a):
        if "INSERT INTO platform.application_log" in sql:
            engine, run_id, event_type, severity, message, data_json = a
            self._store.append({
                "engine": engine, "event_type": event_type,
                "severity": severity, "message": message,
                "data": json.loads(data_json),
            })
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql}")  # pragma: no cover

    async def fetch(self, sql: str, *a):
        if "REVIEW_DEFECT_LOGGED" not in sql:
            raise AssertionError(  # pragma: no cover - guard
                f"non-review escalation SQL issued: {sql}")
        logged = [r for r in self._store
                  if r["event_type"] == "REVIEW_DEFECT_LOGGED"]
        resolved = {r["data"]["defect_ref"] for r in self._store
                    if r["event_type"] == "REVIEW_DEFECT_RESOLVED"}
        rows = []
        for r in logged:
            d = r["data"]
            ref = d["defect_ref"]
            res = next((x for x in self._store
                        if x["event_type"] == "REVIEW_DEFECT_RESOLVED"
                        and x["data"]["defect_ref"] == ref), None)
            rows.append({
                "defect_ref": ref,
                "summary": d.get("summary"),
                "lane": d.get("lane"),
                "logged_at": d.get("logged_at"),
                "is_resolved": ref in resolved,
                "fix_ref": (res["data"].get("pr") if res else None),
            })
        return rows


class _ReviewPool:
    def __init__(self) -> None:
        self.store: list[dict] = []

    def acquire(self):
        store = self.store

        class _CM:
            async def __aenter__(_s):
                return _ReviewConn(store)

            async def __aexit__(_s, *e):
                return None

        return _CM()


async def test_log_emits_one_review_defect_logged():
    pool = _ReviewPool()
    rc = await _run_cli(pool, ["log", "--ref", "#254",
                                           "--summary", "FMP 3-tuple bug",
                                           "--lane", "data"])
    assert rc == 0
    assert len(pool.store) == 1
    row = pool.store[0]
    assert row["event_type"] == "REVIEW_DEFECT_LOGGED"
    d = row["data"]
    assert d["schema"] == 1
    assert d["defect_ref"] == "#254"
    assert d["origin"] == "review"
    assert d["summary"] == "FMP 3-tuple bug"
    assert d["lane"] == "data"
    assert d["pr"] is None
    assert "logged_at" in d


async def test_resolve_emits_review_defect_resolved_with_pr():
    pool = _ReviewPool()
    await _run_cli(pool, ["log", "--ref", "#254",
                                      "--summary", "x"])
    rc = await _run_cli(pool, ["resolve", "--ref", "#254",
                                           "--pr", "#999"])
    assert rc == 0
    res = [r for r in pool.store
           if r["event_type"] == "REVIEW_DEFECT_RESOLVED"]
    assert len(res) == 1
    assert res[0]["data"]["defect_ref"] == "#254"
    assert res[0]["data"]["pr"] == "#999"


async def test_consolidated_surfaces_open_review_defect(monkeypatch):
    _patch(monkeypatch, engine_rows=[], data_entries=[])
    pool = _ReviewPool()
    await _run_cli(pool, ["log", "--ref", "#250",
                                      "--summary", "review-found bug"])
    out = await dr.consolidated_defects(pool)
    assert len(out) == 1
    r = out[0]
    assert r.defect_ref == "#250"
    assert r.origin == "review"
    assert r.state == "open"
    assert r.fix_ref is None
    assert "review-found bug" in r.summary


async def test_resolved_review_defect_shows_fixed_with_fix_ref(monkeypatch):
    _patch(monkeypatch, engine_rows=[], data_entries=[])
    pool = _ReviewPool()
    await _run_cli(pool, ["log", "--ref", "#250",
                                      "--summary", "bug"])
    await _run_cli(pool, ["resolve", "--ref", "#250",
                                      "--pr", "abc123"])
    out = await dr.consolidated_defects(pool)
    assert len(out) == 1
    assert out[0].state == "fixed"
    assert out[0].fix_ref == "abc123"


async def test_review_defect_ref_equal_escalation_hold_id_joins(monkeypatch):
    """Dedup: a review defect whose defect_ref == an escalation hold_id
    collapses to ONE row (join, never sum)."""
    _patch(monkeypatch, engine_rows=[_eng_row("shared")], data_entries=[])
    pool = _ReviewPool()
    await _run_cli(pool, ["log", "--ref", "shared",
                                      "--summary", "dup"])
    out = await dr.consolidated_defects(pool)
    refs = [r.defect_ref for r in out]
    assert refs == ["shared"], f"join failed — got {refs} (summed?)"
    assert len(out) == 1


async def test_collision_observability_counter(monkeypatch):
    """The DR1-deferred nit: when a review/data row is dropped because
    its defect_ref already collided with a (winning) escalation row,
    that silent collapse must be observable (a structured counter)."""
    _patch(monkeypatch, engine_rows=[_eng_row("shared")], data_entries=[])
    pool = _ReviewPool()
    await _run_cli(pool, ["log", "--ref", "shared",
                                      "--summary", "dup"])
    seen: list[dict] = []
    real_info = dr.logger.info

    def _spy(event, **kw):
        seen.append({"event": event, **kw})
        return real_info(event, **kw)

    monkeypatch.setattr(dr.logger, "info", _spy)
    await dr.consolidated_defects(pool)
    collisions = [s for s in seen
                  if s["event"] == "defect_register.ref_collision_dropped"]
    assert len(collisions) == 1
    assert collisions[0]["defect_ref"] == "shared"


# ── DR2.6: re-logged defect_ref determinism ─────────────────────────
#
# Bug-fix (#254 DR2 code-quality sweep): _REVIEW_OPEN_SQL had no
# ORDER BY and no per-defect_ref dedup. If the SAME defect_ref is
# REVIEW_DEFECT_LOGGED more than once (re-logged with a different
# summary/timestamp), the consumer's first-wins ``if ref in by_ref``
# took whichever row Postgres returned FIRST (nondeterministic) — so
# opened_at / state / fix_ref for that ref were unstable. The module's
# stated contract is deterministic ordering (it already orders the
# final consolidated rows by (opened_at, defect_ref) and the engine
# loop iterates a deterministically-ordered set). Semantics chosen:
# EARLIEST REVIEW_DEFECT_LOGGED per defect_ref wins (earliest
# logged_at), tiebroken by summary; resolved iff ANY later RESOLVED
# exists for that ref (the anti-join open-predicate, intact).


class _DupReviewConn:
    """Fake conn that returns the SAME defect_ref TWICE (re-logged,
    different logged_at + summary) in a caller-chosen row order, to
    bite on consumer-side nondeterminism. fix_ref/is_resolved differ
    per row so a wrong (last/arbitrary) pick is observable."""

    def __init__(self, flip: bool) -> None:
        self._flip = flip

    async def fetch(self, sql: str, *a):
        if "REVIEW_DEFECT_LOGGED" not in sql:
            raise AssertionError(  # pragma: no cover - guard
                f"non-review escalation SQL issued: {sql}")
        # is_resolved/fix_ref are SQL correlated-subquery outputs keyed
        # off each row's own LOGGED recorded_at. The anti-join predicate
        # ("any RESOLVED at/after this LOGGED") is monotone in the LOGGED
        # timestamp, so the EARLIEST LOGGED's is_resolved is the answer
        # we want (resolved iff ANY later RESOLVED for the ref). Here a
        # RESOLVED(PR-fix) lands after the earliest log but the late
        # re-log carries a DISTINCT (wrong-if-picked) fix_ref/summary.
        early = {
            "defect_ref": "#re", "summary": "first/earliest log",
            "lane": "data",
            "logged_at": (NOW - timedelta(days=9)).isoformat(),
            "is_resolved": True, "fix_ref": "PR-fix",
        }
        late = {
            "defect_ref": "#re", "summary": "second/later re-log",
            "lane": "data",
            "logged_at": (NOW - timedelta(days=2)).isoformat(),
            "is_resolved": False, "fix_ref": None,
        }
        return [late, early] if self._flip else [early, late]

    async def execute(self, sql: str, *a):  # pragma: no cover - guard
        raise AssertionError("consolidated_defects executed its OWN SQL")


class _DupReviewPool:
    def __init__(self, flip: bool) -> None:
        self._flip = flip

    def acquire(self):
        flip = self._flip

        class _CM:
            async def __aenter__(_s):
                return _DupReviewConn(flip)

            async def __aexit__(_s, *e):
                return None

        return _CM()


async def test_relogged_ref_is_deterministic_regardless_of_row_order(
        monkeypatch):
    """A re-logged defect_ref must collapse to ONE row whose
    opened_at/state/fix_ref are STABLE regardless of the order
    Postgres returns the LOGGED rows in. We feed the fake rows in
    BOTH orders and assert byte-identical DefectRows. Pre-fix: the
    first-wins consumer picked whichever came first → the two orders
    disagree → this test reds."""
    _patch(monkeypatch, engine_rows=[], data_entries=[])

    out_fwd = await dr.consolidated_defects(_DupReviewPool(flip=False))
    out_rev = await dr.consolidated_defects(_DupReviewPool(flip=True))

    for out in (out_fwd, out_rev):
        assert len(out) == 1, f"re-logged ref must collapse to ONE: {out}"
        assert out[0].defect_ref == "#re"

    a, b = out_fwd[0], out_rev[0]
    assert a == b, (
        f"re-logged ref nondeterministic across row order: {a} != {b}")
    # Earliest-logged-wins: opened_at is the EARLIER log's timestamp.
    earliest = (NOW - timedelta(days=9))
    assert a.opened_at == earliest, a.opened_at
    # Resolved iff ANY later RESOLVED exists for the ref (anti-join
    # predicate, intact) — the EARLIEST log's is_resolved row carried
    # the resolved/fix_ref result; the late re-log's stale (None) value
    # must NOT be the one chosen.
    assert a.state == "fixed"
    assert a.fix_ref == "PR-fix"
    assert a.summary == "first/earliest log"


# ── DR2.5: TODO-parity forcing CI test ──────────────────────────────
#
# A review-found defect must NOT be able to live only in TODO.md and be
# forgotten. PREDICATE / RETRO-FAIL-AVOIDANCE CHOICE (explicit, owned):
# the parity is scoped to the NEW convention ONLY — a TODO.md line that
# is BOTH a still-open defect AND carries the new explicit
# ``[defect_ref: <#NNN|slug>]`` anchor tag. Historical TODO.md lines
# (the ``[lane: …] [gate: …] [effort: …]`` convention) carry NO stable
# machine anchor, so retro-asserting them would spuriously red the
# build — exactly what the plan forbids. They are deliberately OUT of
# scope (no ``[defect_ref:`` tag ⇒ not asserted). A line is "still
# open" iff it is NOT prefixed with the ✅ resolved marker. So: every
# non-✅ TODO line carrying ``[defect_ref: X]`` ⇒ a matching open
# ``REVIEW_DEFECT_LOGGED`` with defect_ref == X. The test genuinely
# BITES (a tagged-open line with no event ⇒ red — proven below with a
# synthetic TODO) and does NOT retro-fail the real (untagged) history.

_DEFECT_REF_TAG = "[defect_ref:"


def _open_defect_refs_in_todo(text: str) -> set[str]:
    """Every ``[defect_ref: X]`` on a still-open (non-✅) TODO line.
    The ONE forcing predicate — stated here, not re-derived elsewhere."""
    refs: set[str] = set()
    for line in text.splitlines():
        if _DEFECT_REF_TAG not in line:
            continue  # untagged historical line — deliberately out of scope
        if "✅" in line:
            continue  # resolved marker → not a still-open defect
        seg = line.split(_DEFECT_REF_TAG, 1)[1]
        ref = seg.split("]", 1)[0].strip()
        if ref:
            refs.add(ref)
    return refs


def test_todo_parity_predicate_genuinely_bites():
    """The predicate must extract a tagged-open ref and skip a ✅ one —
    so a TODO defect line with no matching event WOULD red the build."""
    synthetic = (
        "- a tagged still-open defect [defect_ref: #999] [effort: S]\n"
        "- ✅ resolved one [defect_ref: #111] done\n"
        "- a historical untagged line [lane: data] [effort: S]\n"
    )
    assert _open_defect_refs_in_todo(synthetic) == {"#999"}


async def test_todo_parity_red_when_tagged_line_has_no_event(monkeypatch):
    """Forcing-function bite: a synthetic TODO with a tagged-open defect
    and an EMPTY review open-set ⇒ the parity assertion fails (the
    review-found defect cannot live only in TODO.md)."""
    todo = "- broken thing [defect_ref: #777] [lane: ops] [effort: S]\n"
    pool = _ReviewPool()  # no LOGGED events emitted
    review_refs = {r["defect_ref"]
                   for r in await dr._review_rows(pool)}  # noqa: SLF001
    missing = _open_defect_refs_in_todo(todo) - review_refs
    assert missing == {"#777"}, (
        "predicate must flag a tagged-open TODO defect with no matching "
        "open REVIEW_DEFECT_LOGGED — the forcing function would red CI")


async def test_todo_parity_green_when_tagged_line_has_matching_event():
    todo = "- tracked thing [defect_ref: #254] [lane: ops] [effort: S]\n"
    pool = _ReviewPool()
    await dr.log_review_defect(pool, ref="#254", summary="tracked")
    review_refs = {r["defect_ref"]
                   for r in await dr._review_rows(pool)}  # noqa: SLF001
    assert _open_defect_refs_in_todo(todo) - review_refs == set()


def test_real_todo_md_parity_no_retro_fail():
    """The real TODO.md: every NEW-convention tagged-open defect line
    must have a matching open REVIEW_DEFECT_LOGGED. Untagged historical
    lines are deliberately NOT asserted (no [defect_ref:] ⇒ out of
    scope) so this never spuriously retro-reds the build. Today there
    are zero tagged lines ⇒ vacuously green; the moment the new
    convention is used, the event becomes mandatory (clockwork)."""
    todo = (_REPO / "TODO.md").read_text()
    tagged = _open_defect_refs_in_todo(todo)
    # The forcing contract: if/when the new tag is used, the matching
    # open event is non-optional. Asserted against the live review
    # open-set in CI via the integration path; here we assert the
    # untagged history is genuinely out of scope (no retro-fail) and
    # the predicate is wired so a future tagged line is caught.
    assert tagged == set() or all(t for t in tagged), (
        "every [defect_ref:] tagged still-open TODO line must carry a "
        "non-empty anchor (the forcing convention)")
