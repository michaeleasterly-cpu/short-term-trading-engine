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
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
# Documented scripts/ops.py ↔ ops/ package-shadow hazard: when
# scripts/tests/ is co-collected, scripts/ops.py can register itself as
# module ``ops``, breaking ``from ops import engine_ladder``. Purge any
# shadowed (non-package) ``ops`` entry so the real package wins (the
# scripts/tests/test_engine_ladder.py precedent, lines 8-10).
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

_SPEC = importlib.util.spec_from_file_location(
    "_dr_under_test", _REPO / "ops" / "defect_register.py")
dr = importlib.util.module_from_spec(_SPEC)
sys.modules["_dr_under_test"] = dr
_SPEC.loader.exec_module(dr)

NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
T1 = NOW - timedelta(days=10)
T2 = NOW - timedelta(days=8)
T3 = NOW - timedelta(days=6)


class _GuardConn:
    """A fake connection that FAILS if anyone runs an escalation query
    against it — proves the register issues NO application_log
    escalation SELECT of its own (the no-re-derivation invariant)."""

    async def fetch(self, sql: str, *a):  # pragma: no cover - guard
        raise AssertionError(
            "consolidated_defects issued its OWN DB query — it must "
            f"compose the Ladder APIs verbatim, never re-derive. SQL: {sql}")

    async def execute(self, sql: str, *a):  # pragma: no cover - guard
        raise AssertionError("consolidated_defects executed its OWN SQL")


class _GuardPool:
    """Records that .acquire() was never used (the register must not
    touch the pool itself — only the Ladder APIs do)."""

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
    # The register touched ONLY the Ladder APIs, never the pool.
    assert pool.acquired is False


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
    consolidated_defects must NEVER call pool.acquire()/run its own
    escalation SELECT. _GuardPool.acquire + _GuardConn.fetch raise — so
    if someone later inlines an application_log query here, this test
    fails loudly."""
    _patch(monkeypatch, engine_rows=[_eng_row("h1")],
           data_entries=[_data_entry("d1")])
    pool = _GuardPool()
    out = await dr.consolidated_defects(pool)
    assert {r.defect_ref for r in out} == {"h1", "d1"}
    assert pool.acquired is False, (
        "register acquired a DB connection itself — it must compose "
        "the Ladder APIs verbatim and re-derive nothing")


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

    fp = _FakePool()

    async def _fake_build(_dsn):
        return fp

    monkeypatch.setattr(dr, "build_asyncpg_pool", _fake_build)
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    rc = await dr._amain(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    # Stable, grep-able, deterministic order (opened_at, defect_ref).
    assert out.index("h1") < out.index("req-9")
    assert "engine" in out and "data" in out


async def test_cli_no_dsn_explicit_nonzero(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_IPV4", raising=False)
    rc = await dr._amain(["list"])
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
