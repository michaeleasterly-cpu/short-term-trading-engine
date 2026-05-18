"""LT-P2: the agent calls the official SDK (mocked), emits a
non-authoritative DATA_LLM_TRIAGE_PROPOSAL, never passes tools,
no-ops without a key, crash-isolated. No live API calls."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, datetime

_spec = importlib.util.spec_from_file_location(
    "lt_agent",
    pathlib.Path(__file__).resolve().parents[1] / "ops" / "llm_data_triage.py")
lt = importlib.util.module_from_spec(_spec)
sys.modules["lt_agent"] = lt
_spec.loader.exec_module(lt)


class _Block:
    def __init__(self, text): self.type = "text"; self.text = text


class _Usage:
    input_tokens = 11
    output_tokens = 22


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.usage = _Usage()
        self.id = "msg_x"
        self.model = "claude-sonnet-4-6"


class _Messages:
    def __init__(self, rec): self._rec = rec
    def create(self, **kw):
        self._rec.append(kw)
        return _Msg(json.dumps({
            "proposed_disposition": "converted", "confidence": "med",
            "rationale": "r", "could_not_determine": "n"}))


class _Client:
    def __init__(self, rec): self.messages = _Messages(rec)


class _Conn:
    def __init__(self, p): self._p = p; self.emitted = []
    async def fetch(self, sql, *a):
        if "OPEN_ESCALATIONS" in sql:
            return [dict(r) for r in self._p.open_rows]
        if "DATA_LLM_TRIAGE_PROPOSAL" in sql:
            return [{"ref": r} for r in self._p.prior]
        return []  # packet's dq fetch → empty
    async def execute(self, sql, *a): self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, open_rows=(), prior=()):
        self.open_rows = list(open_rows); self.prior = list(prior)
        self.conn = _Conn(self)
    def acquire(self): return _CM(self.conn)


def _row(ref="h1"):
    return {"ref": ref, "etype": "DATA_SOURCE_ESCALATED",
            "cls": "event:DATA_SOURCE_ESCALATED",
            "recorded_at": datetime(2026, 5, 1, tzinfo=UTC), "message": "m"}


async def test_calls_sdk_no_tools_and_emits_proposal(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    rec: list = []
    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _Client(rec))
    assert len(rec) == 1
    kw = rec[0]
    assert kw["model"] == "claude-sonnet-4-6"
    assert kw["temperature"] == 0.0
    assert "tools" not in kw           # structural: never acts
    assert kw["messages"][0]["role"] == "user"
    assert isinstance(kw["system"], str) and kw["system"]
    ev = [json.loads(a[5]) for a in pool.conn.emitted]
    prop = next(e for e in ev)
    assert prop["ref"] == "h1" and prop["proposed_disposition"] == "converted"
    assert prop["persona_version"] == lt._PERSONA_VERSION
    assert "packet_hash" in prop
    assert out.proposed == ["h1"]


async def test_no_api_key_is_safe_noop(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rec: list = []
    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _Client(rec))
    assert rec == [] and pool.conn.emitted == [] and out.proposed == []
    assert out.skipped_no_key is True


async def test_api_error_is_crash_isolated(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class _Boom:
        @property
        def messages(self):
            class M:
                def create(self, **kw): raise RuntimeError("api down")
            return M()

    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _Boom())
    assert pool.conn.emitted == []          # no proposal on failure
    assert out.error is not None            # never raises
    assert out.proposed == []


async def test_auth_error_is_safe_like_no_key(monkeypatch) -> None:
    """AuthenticationError must behave exactly like a missing key:
    zero retries (create called AT MOST once), zero emits, no error,
    skipped_no_key=True, proposed=[].
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-key")

    import anthropic

    # Subclass so construction is trivial while isinstance check passes.
    class _Auth(anthropic.AuthenticationError):
        def __init__(self) -> None:
            pass

    assert isinstance(_Auth(), anthropic.AuthenticationError)

    call_count = 0

    class _AuthMessages:
        def create(self, **kw):
            nonlocal call_count
            call_count += 1
            raise _Auth()

    class _AuthClient:
        def __init__(self):
            self.messages = _AuthMessages()

    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _AuthClient())

    # (a) create called at most once — zero retries
    assert call_count == 1, f"expected 1 call, got {call_count} (retry bug)"
    # (b) zero emits
    assert pool.conn.emitted == []
    # (c) no error recorded
    assert out.error is None
    # (d) flagged as skipped_no_key
    assert out.skipped_no_key is True
    # (e) no proposals
    assert out.proposed == []


async def test_import_isolation_no_actor_paths() -> None:
    # The agent must NOT import any actor/mutation path.
    import ast
    src = pathlib.Path("ops/llm_data_triage.py").read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module)
    forbidden = ("tpcore.risk", "tpcore.order_management",
                 "tpcore.selfheal.orchestrator", "tpcore.selfheal.runner",
                 "tpcore.auditheal", "tpcore.datasupervisor", "scripts.ops")
    bad = [m for m in imported for f in forbidden
           if m == f or m.startswith(f + ".")]
    assert bad == [], f"agent imports fenced actor path(s): {bad}"
