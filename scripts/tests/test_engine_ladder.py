import contextlib
import json as _json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402
from ops import engine_ladder as el  # noqa: E402
from ops import engine_supervisor as es  # noqa: E402


def test_disposition_enum_is_converted_structural_removed():
    vals = {d.value for d in el.EngineEscalationDisposition}
    assert vals == {"converted", "structural", "removed"}
    assert "auto_converted" not in vals


def test_known_classes_derived_from_real_constants():
    # Phase-0: KNOWN is the explicit 3-way union — INFRA (DA-1) +
    # PLATFORM_SERVICE (engine-daemon co-hosted) + {behavioral} (DA-2).
    assert el.KNOWN_ESCALATION_CLASSES == (
        es.INFRA_FAILURE_CLASSES | es.PLATFORM_SERVICE_FAILURE_CLASSES
        | {at._BEHAVIORAL})


def test_every_known_class_has_a_policy():
    for cls in el.KNOWN_ESCALATION_CLASSES:
        p = el.policy_for(cls)
        assert p is not None
        assert isinstance(p.default, el.EngineEscalationDisposition)
        assert p.rationale.strip()


def test_data_repair_escalated_default_is_structural_not_removed():
    p = el.policy_for("data_repair_escalated")
    assert p.default is el.EngineEscalationDisposition.STRUCTURAL


def test_escalation_drift_empty_in_lockstep():
    missing, extra = el.escalation_drift()
    assert missing == set(), f"classes with no disposition policy: {missing}"
    assert extra == set(), f"disposition policies for unknown classes: {extra}"


def test_platform_service_classes_in_known_set_and_have_structural_policy():
    """Phase-0: the two engine-daemon co-hosted platform-service failure
    classes must be in KNOWN (via the new frozenset union, NOT folded
    into INFRA_FAILURE_CLASSES) AND each carry a STRUCTURAL policy — the
    R2 clockwork tooth (escalation_drift stays empty)."""
    psf = es.PLATFORM_SERVICE_FAILURE_CLASSES
    assert psf == frozenset(
        {"engine_service_task_crashloop", "engine_service_digest_failed"})
    # not folded into INFRA
    assert psf & es.INFRA_FAILURE_CLASSES == set()
    # derived KNOWN set is the explicit 3-way union
    assert el.KNOWN_ESCALATION_CLASSES == (
        es.INFRA_FAILURE_CLASSES | psf | {at._BEHAVIORAL})
    for cls in psf:
        assert cls in el.KNOWN_ESCALATION_CLASSES
        p = el.policy_for(cls)
        assert p is not None
        assert p.default is el.EngineEscalationDisposition.STRUCTURAL
        assert p.rationale.strip()
    missing, extra = el.escalation_drift()
    assert missing == set() and extra == set()


def test_escalation_drift_reports_missing_for_uncovered_class():
    missing, extra = el._drift_for(
        known=el.KNOWN_ESCALATION_CLASSES | {"_synthetic_probe"},
        policies=el.DISPOSITION_POLICIES)
    assert "_synthetic_probe" in missing
    assert extra == set()


def test_policy_for_unknown_is_none():
    assert el.policy_for("not_a_class") is None


from datetime import UTC, datetime, timedelta  # noqa: E402

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)   # past 7d grace
FRESH = NOW - timedelta(days=1)  # within grace


def _conn(esc_rows, open_fps):
    """esc_rows: rows the candidate-SQL returns; open_fps: fingerprints
    still open in forensics_triggers."""
    class _C:
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return [{"fp": fp} for fp in open_fps]
            cutoff = a[0] if a else None
            if cutoff is not None:
                return [r for r in esc_rows
                        if r["recorded_at"] is None or r["recorded_at"] < cutoff]
            return esc_rows
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _C()
    return _P()


def _row(hold_id, *, engine="reversion", failure_class="crashed_startup",
         reason="x", recorded_at=OLD, has_held=True, triggers=None,
         raw_triggers=False):
    t = triggers or []
    return {"hold_id": hold_id, "engine": engine,
            "failure_class": failure_class, "reason": reason,
            "recorded_at": recorded_at, "has_held": has_held,
            "triggers": (t if raw_triggers else _json.dumps(t))}


async def test_list_includes_past_grace_held_open():
    pool = _conn([_row("h1")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["h1"]
    assert out[0]["shape"] == "held"


async def test_list_excludes_within_grace():
    pool = _conn([_row("h2", recorded_at=FRESH)], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_escalate_only_included_when_fps_still_open():
    pool = _conn([_row("e1", failure_class="behavioral", has_held=False,
                        triggers=["fp-a", "fp-b"])], {"fp-b"})
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["e1"]
    assert out[0]["shape"] == "escalate-only"


async def test_list_escalate_only_excluded_when_all_fps_resolved():
    pool = _conn([_row("e2", failure_class="behavioral", has_held=False,
                        triggers=["fp-x"])], set())
    assert await el.list_undispositioned(pool, now=NOW, grace_days=7) == []


async def test_list_carries_policy_default():
    pool = _conn([_row("h3", failure_class="data_repair_escalated")], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert out[0]["policy_default"] == "structural"


async def test_list_escalate_only_native_list_triggers_branch():
    pool = _conn([_row("e3", failure_class="behavioral", has_held=False,
                        triggers=["fp-n"], raw_triggers=True)], {"fp-n"})
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["e3"]
    assert out[0]["shape"] == "escalate-only"


async def test_list_escalate_only_no_fps_stays_open():
    pool = _conn([_row("e4", failure_class="behavioral", has_held=False,
                        triggers=[])], set())
    out = await el.list_undispositioned(pool, now=NOW, grace_days=7)
    assert [r["hold_id"] for r in out] == ["e4"]
    assert out[0]["shape"] == "escalate-only"


def _rec_pool(open_hold_ids):
    """fetch returns a 1-row marker iff hold_id is an open escalation;
    execute records the disposition INSERT. The ENGINE_ESCALATED row is
    escalate-only (has_held=False) with a still-open fingerprint so the
    shared escalate-only gate keeps it genuinely open."""
    class _C:
        def __init__(self):
            self.inserts = []
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return [{"fp": "fp-open"}]  # still unresolved → open
            if "ENGINE_ESCALATED" in sql:
                hid = a[0]
                return ([{"hold_id": hid, "engine": "reversion",
                          "has_held": False,
                          "triggers": _json.dumps(["fp-open"])}]
                        if hid in open_hold_ids else [])
            return []
        async def execute(self, sql, *a):
            self.inserts.append((sql, a))
    c = _C()
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield c
    p = _P()
    p.conn = c
    return p


async def test_disposition_emits_locked_event_for_valid_verb():
    pool = _rec_pool({"h9"})
    rc = await el.disposition(pool, "h9", "Structural", "the note")
    assert rc == 0
    ins = [a for s, a in pool.conn.inserts
           if "INSERT INTO platform.application_log" in s]
    assert len(ins) == 1
    payload = _json.loads(ins[0][-1])
    assert payload == {"schema": 1, "hold_id": "h9",
                       "disposition": "structural", "note": "the note"}
    assert ins[0][2] == "ENGINE_ESCALATION_DISPOSITIONED"


async def test_disposition_rejects_unknown_verb_no_write():
    pool = _rec_pool({"h9"})
    rc = await el.disposition(pool, "h9", "bogus", "")
    assert rc == 1
    assert not any("INSERT" in s for s, _ in pool.conn.inserts)


async def test_disposition_rejects_unknown_or_not_open_hold_no_write():
    pool = _rec_pool(set())
    rc = await el.disposition(pool, "h9", "structural", "")
    assert rc == 2
    assert not any("INSERT" in s for s, _ in pool.conn.inserts)


async def test_disposition_accepts_escalate_only_hold_id():
    pool = _rec_pool({"e1"})
    rc = await el.disposition(pool, "e1", "converted", "fixed it")
    assert rc == 0


async def test_disposition_missing_engine_surfaces_no_write():
    class _C:
        def __init__(self): self.inserts = []
        async def fetch(self, sql, *a):
            if "ENGINE_ESCALATED" in sql:
                return [{"engine": None}]
            return []
        async def execute(self, sql, *a):
            self.inserts.append((sql, a))
    c = _C()
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield c
    p = _P()
    p.conn = c
    rc = await el.disposition(p, "hx", "structural", "")
    assert rc == 2
    assert p.conn.inserts == []


async def test_disposition_rejects_escalate_only_all_fps_resolved_no_write():
    # escalate-only hold_id whose fingerprints have ALL resolved →
    # list_undispositioned auto-closed it → disposition MUST reject it.
    class _C:
        def __init__(self): self.inserts = []
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return []  # zero still-open fingerprints
            if "ENGINE_ESCALATED" in sql:
                return [{"engine": "reversion", "has_held": False,
                         "triggers": _json.dumps(["fp-gone"])}]
            return []
        async def execute(self, sql, *a):
            self.inserts.append((sql, a))
    c = _C()
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield c
    p = _P()
    p.conn = c
    rc = await el.disposition(p, "eo1", "structural", "")
    assert rc == 2
    assert p.conn.inserts == []


async def test_disposition_accepts_escalate_only_fp_still_open_one_write():
    # complementary positive: an escalate-only hold_id with a still-open
    # fingerprint → disposition rc==0 + exactly one INSERT (proves the
    # new gate does NOT over-reject genuinely-open escalate-only rows).
    class _C:
        def __init__(self): self.inserts = []
        async def fetch(self, sql, *a):
            if "forensics_triggers" in sql:
                return [{"fp": "fp-live"}]  # still unresolved → open
            if "ENGINE_ESCALATED" in sql:
                return [{"engine": "vector", "has_held": False,
                         "triggers": _json.dumps(["fp-live"])}]
            return []
        async def execute(self, sql, *a):
            self.inserts.append((sql, a))
    c = _C()
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield c
    p = _P()
    p.conn = c
    rc = await el.disposition(p, "eo2", "removed", "edge gone")
    assert rc == 0
    ins = [a for s, a in p.conn.inserts
           if "INSERT INTO platform.application_log" in s]
    assert len(ins) == 1
    payload = _json.loads(ins[0][-1])
    assert payload == {"schema": 1, "hold_id": "eo2",
                       "disposition": "removed", "note": "edge gone"}
    assert ins[0][2] == "ENGINE_ESCALATION_DISPOSITIONED"


def test_module_has_main_entrypoint():
    src = (REPO_ROOT / "ops" / "engine_ladder.py").read_text()
    assert 'if __name__ == "__main__":' in src
    assert "argparse" in src
    assert "def main()" in src


async def test_amain_list_runs_dbless_clean(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_IPV4", raising=False)
    rc = await el._amain(["list"])
    assert rc == 1  # explicit no-DSN failure, NOT a silent 0


async def test_amain_dispatches_list_and_disposition(monkeypatch):
    """Wire-up guard: _amain must route list→list_undispositioned and
    disposition→disposition with correctly-named argparse attrs (the
    canary __main__-no-op lesson — a typo'd attr would survive to a
    live run otherwise)."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")

    class _FakePool:
        async def close(self): ...
    fake_pool = _FakePool()

    async def _fake_build(_dsn):
        return fake_pool
    monkeypatch.setattr(el, "build_asyncpg_pool", _fake_build)

    seen = {}

    async def _fake_list(pool, *, grace_days=None):
        seen["list"] = (pool is fake_pool, grace_days)
        return []

    async def _fake_disp(pool, hold_id, verb, note):
        seen["disp"] = (pool is fake_pool, hold_id, verb, note)
        return 0
    monkeypatch.setattr(el, "list_undispositioned", _fake_list)
    monkeypatch.setattr(el, "disposition", _fake_disp)

    rc_list = await el._amain(["list", "--grace-days", "3"])
    rc_disp = await el._amain(
        ["disposition", "h1", "structural", "a", "note"])

    assert rc_list == 0
    assert seen["list"] == (True, 3)
    assert rc_disp == 0
    assert seen["disp"] == (True, "h1", "structural", "a note")


# ════════════════════════════════════════════════════════════════════════
# Epic E Phase 3.2 — surface the engine ENGINE_LLM_TRIAGE_PROPOSAL on the
# engine escalation's EXISTING undispositioned digest line (the engine
# equivalent of weekly_digest._llm_suffix). DRY: reuse the EXISTING line
# builder (_fmt) + the EXISTING open set (list_undispositioned output) —
# do NOT re-derive the engine overdue set. Annotation appears iff a
# proposal exists for that hold_id.
# ════════════════════════════════════════════════════════════════════════


def _row_out(hold_id, **kw):
    base = {
        "hold_id": hold_id, "engine": "reversion",
        "failure_class": "crashed_startup", "reason": "x",
        "recorded_at": OLD, "shape": "held",
        "policy_default": "structural", "policy_rationale": "r",
    }
    base.update(kw)
    return base


def test_fmt_no_llm_proposal_renders_no_suffix():
    # A row WITHOUT an attached LLM proposal renders exactly the legacy
    # line (no annotation) — the data-lane _llm_suffix "" parity.
    rows = [_row_out("h1")]
    out = el._fmt(rows)
    assert "h1" in out
    assert "LLM:" not in out  # no proposal → no suffix


def test_fmt_llm_proposal_appended_when_present():
    rows = [
        _row_out(
            "h2",
            llm_proposal={
                "proposed_disposition": "structural",
                "confidence": "0.81",
                "pr_link": "https://example/pr/9",
            },
        )
    ]
    out = el._fmt(rows)
    assert "LLM: structural" in out
    assert "conf 0.81" in out
    assert "PR https://example/pr/9" in out


def test_fmt_llm_proposal_no_pr_link_degrades_gracefully():
    rows = [
        _row_out(
            "h3",
            llm_proposal={
                "proposed_disposition": "converted",
                "confidence": "0.4",
                "pr_link": None,
            },
        )
    ]
    out = el._fmt(rows)
    assert "LLM: converted" in out
    assert "(no PR)" in out


async def test_attach_llm_proposals_reuses_open_set_no_rederive():
    """_attach_llm_proposals annotates the GIVEN open-set rows in place
    from ENGINE_LLM_TRIAGE_PROPOSAL — it does NOT re-query
    list_undispositioned / re-derive the overdue set. Only rows whose
    hold_id has a proposal get annotated; the rest stay clean."""
    rows = [_row_out("hA"), _row_out("hB")]

    class _C:
        async def fetch(self, sql, *a):
            assert "ENGINE_LLM_TRIAGE_PROPOSAL" in sql
            # Only hA has a proposal.
            return [{
                "hold_id": "hA",
                "proposed_disposition": "structural",
                "confidence": "0.9",
                "pr_link": "https://x/pr/1",
            }]

    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _C()

    await el._attach_llm_proposals(_P(), rows)
    assert rows[0]["llm_proposal"]["proposed_disposition"] == "structural"
    assert "llm_proposal" not in rows[1] or rows[1]["llm_proposal"] is None
    out = el._fmt(rows)
    assert "hA" in out and "LLM: structural" in out
    # hB has NO proposal → its line carries no LLM suffix.
    hb_line = [ln for ln in out.splitlines() if "hold_id=hB" in ln][0]
    assert "LLM:" not in hb_line


async def test_amain_list_attaches_llm_proposals(monkeypatch):
    """The `list` CLI path attaches LLM proposals to the open set
    (reusing list_undispositioned's output) before _fmt renders it."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")

    class _FakePool:
        async def close(self): ...
    fake_pool = _FakePool()

    async def _fake_build(_dsn):
        return fake_pool
    monkeypatch.setattr(el, "build_asyncpg_pool", _fake_build)

    open_rows = [_row_out("hZ")]

    async def _fake_list(pool, *, grace_days=None):
        return open_rows

    seen = {}

    async def _fake_attach(pool, rows):
        seen["attach"] = (pool is fake_pool, rows is open_rows)

    monkeypatch.setattr(el, "list_undispositioned", _fake_list)
    monkeypatch.setattr(el, "_attach_llm_proposals", _fake_attach)

    rc = await el._amain(["list"])
    assert rc == 0
    # Reused the SAME open-set list object — no re-derivation.
    assert seen["attach"] == (True, True)
