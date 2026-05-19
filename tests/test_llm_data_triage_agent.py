"""LT-P2: the agent calls the official SDK (mocked), emits a
non-authoritative DATA_LLM_TRIAGE_PROPOSAL, never passes tools,
no-ops without a key, crash-isolated. No live API calls.

Test-hygiene fence (autouse, this whole module): these P2 tests
exercise the SDK/emit path and DELIBERATELY set ANTHROPIC_API_KEY,
so a produced proposal would reach P3 ``_open_draft_pr`` — which, with
the *real* ``_default_pr_runner``, runs real ``git worktree add -b
llm-triage/<ref>`` / a nested ``pytest`` / ``gh pr create`` against the
LIVE host repo (leaking ``llm-triage/h1`` / ``llm-triage/ref-good``
branches and potentially a real PR). The P3 git path is owned and fully
covered by the isolated ``tpcore/tests/test_llm_data_triage_pr.py``
(injected ``_FakeRunner``); here we replace the module default
``_default_pr_runner`` with a no-op fake so NO real subprocess ever
touches the host repo, and assert the host repo is untouched after every
test (the regression bite — any reintroduced real-repo git call fails).
Production ``ops/llm_data_triage.py`` is byte-identical: only the test
process's view of ``lt._default_pr_runner`` is patched.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime

import pytest

_spec = importlib.util.spec_from_file_location(
    "lt_agent",
    pathlib.Path(__file__).resolve().parents[1] / "ops" / "llm_data_triage.py")
lt = importlib.util.module_from_spec(_spec)
sys.modules["lt_agent"] = lt
_spec.loader.exec_module(lt)

_HOST_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _host_llm_triage_branches() -> list[str]:
    """Every `llm-triage/*` local branch in the LIVE host repo (empty
    on a clean repo). Used as the regression bite: a test that leaks a
    real `git worktree add -b llm-triage/<ref>` shows up here.

    Fails LOUD on its own failure (git absent / non-zero exit): a guard
    that returns ``[]`` when it could not actually run git would let the
    pre/post leak asserts vacuously pass — a silent false-negative. The
    only paths out of here are a *positively confirmed* branch list or a
    raised error that ERRORs the test."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(_HOST_REPO_ROOT),
             "branch", "--list", "llm-triage/*"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError as exc:  # git absent
        raise RuntimeError(
            f"host-repo leak guard could not run git: {exc}") from exc
    except subprocess.CalledProcessError as exc:  # non-zero git exit
        raise RuntimeError(
            "host-repo leak guard could not run git: "
            f"rc={exc.returncode} stderr={exc.stderr!r}") from exc
    return [ln.strip().lstrip("* ").strip()
            for ln in proc.stdout.splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def _no_real_pr_path():
    """Autouse, module-wide. (1) Replace the *bound* default
    ``pr_runner`` of ``run_triage`` with a no-op fake so a produced
    proposal can NEVER spawn a real ``git worktree``/nested
    ``pytest``/``gh pr create`` against the host repo. ``pr_runner`` is
    a keyword-only arg whose default is bound to the real
    ``_default_pr_runner`` at def-time and lives in
    ``run_triage.__kwdefaults__`` — patching ``lt._default_pr_runner``
    alone would NOT take effect, so the bound default is what we swap
    (and ``lt._default_pr_runner`` too, for any direct reference).
    (2) Assert the host repo carries no ``llm-triage/*`` branch before
    AND after the test — the structural regression bite if a real-repo
    git call is ever reintroduced."""
    pre = _host_llm_triage_branches()
    assert pre == [], (
        f"host repo dirty BEFORE test (pre-existing leak): {pre}")

    def _fake_pr_runner(argv, *, env=None, cwd=None):  # noqa: ANN001
        # gh pr create → success URL; everything else (incl. every git
        # worktree/branch op) → benign rc=0. No subprocess is spawned.
        if argv and argv[0] == "gh":
            return 0, "https://github.com/x/y/pull/1", ""
        return 0, "", ""

    orig_attr = lt._default_pr_runner
    orig_kwd = dict(lt.run_triage.__kwdefaults__)
    lt._default_pr_runner = _fake_pr_runner
    lt.run_triage.__kwdefaults__["pr_runner"] = _fake_pr_runner
    try:
        yield
    finally:
        lt._default_pr_runner = orig_attr
        lt.run_triage.__kwdefaults__["pr_runner"] = orig_kwd["pr_runner"]
        post = _host_llm_triage_branches()
        assert post == [], (
            "host repo MUTATED by this test — a real-repo `git worktree "
            f"add -b llm-triage/<ref>` leaked branch(es): {post}. The P2 "
            "agent test must NOT exercise the real P3 git path; that is "
            "owned by tpcore/tests/test_llm_data_triage_pr.py.")


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


# Mocks mirror the REAL anthropic.AsyncAnthropic surface: ``messages``
# is an object whose ``create`` is an ``async def`` coroutine fn
# (verified against the official anthropic-sdk-python docs — the agent
# ``await``s it inside the daemon event loop). A *sync* ``create`` mock
# would let a sync-client regression pass silently, so every mock here
# is async by construction.
class _Messages:
    def __init__(self, rec): self._rec = rec
    async def create(self, **kw):
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
                async def create(self, **kw): raise RuntimeError("api down")
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
        async def create(self, **kw):
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


async def test_with_retry_wraps_async_client_and_retries(monkeypatch) -> None:
    """tpcore.outage.with_retry must still retry correctly around the
    AWAITED async client: a transient retryable APIError on the first
    awaited ``messages.create`` is retried and the second call succeeds
    → exactly one proposal, no error. Bite: a retry layer that no longer
    awaits the async call (or stops retrying) fails this.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    import anthropic

    class _Transient(anthropic.APIError):
        def __init__(self) -> None:
            self.request = None
            self.body = None
            self.message = "transient"

    valid = json.dumps({
        "proposed_disposition": "converted", "confidence": "high",
        "rationale": "ok", "could_not_determine": "n"})
    calls = {"n": 0}

    class _FlakyMessages:
        async def create(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Transient()
            return _Msg(valid)

    class _FlakyClient:
        def __init__(self): self.messages = _FlakyMessages()

    # Collapse backoff so the test is fast (with_retry awaits asyncio.sleep).
    import asyncio as _aio
    real_sleep = _aio.sleep

    async def _no_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr(_aio, "sleep", _no_sleep)

    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _FlakyClient())
    assert calls["n"] == 2, f"expected 1 retry (2 calls), got {calls['n']}"
    assert out.error is None
    assert out.proposed == ["h1"]


class _MultiMessages:
    """Returns responses from a list, one per call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
    async def create(self, **kw):
        r = self._responses[self._idx]
        self._idx += 1
        return r


class _MultiClient:
    def __init__(self, responses):
        self.messages = _MultiMessages(responses)


class _EmptyContentMsg:
    """Simulates SDK returning empty content list."""
    def __init__(self):
        self.content = []
        self.stop_reason = "end_turn"
        self.usage = _Usage()
        self.id = "msg_empty"
        self.model = "claude-sonnet-4-6"


async def test_empty_content_skips_escalation_not_batch(monkeypatch) -> None:
    """Empty content[] on first call → per-escalation skip; second processes fine."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    valid_text = json.dumps({
        "proposed_disposition": "converted", "confidence": "high",
        "rationale": "ok", "could_not_determine": "n"})

    responses = [_EmptyContentMsg(), _Msg(valid_text)]
    pool = _Pool(open_rows=[_row("ref-bad"), _row("ref-good")])
    out = await lt.run_triage(pool, client_factory=lambda: _MultiClient(responses))

    assert out.error is None, f"batch aborted: out.error={out.error!r}"
    emitted = [json.loads(a[5]) for a in pool.conn.emitted
               if json.loads(a[5]).get("ref") is not None]
    proposal_refs = [e["ref"] for e in emitted if "proposed_disposition" in e]
    assert proposal_refs == ["ref-good"], f"expected only ref-good, got {proposal_refs}"
    assert out.proposed == ["ref-good"]


async def test_non_dict_json_skips_escalation_not_batch(monkeypatch) -> None:
    """json.loads returns None (not a dict) on first call → per-escalation skip."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    valid_text = json.dumps({
        "proposed_disposition": "converted", "confidence": "high",
        "rationale": "ok", "could_not_determine": "n"})

    responses = [_Msg("null"), _Msg(valid_text)]
    pool = _Pool(open_rows=[_row("ref-null"), _row("ref-good")])
    out = await lt.run_triage(pool, client_factory=lambda: _MultiClient(responses))

    assert out.error is None, f"batch aborted: out.error={out.error!r}"
    emitted = [json.loads(a[5]) for a in pool.conn.emitted
               if json.loads(a[5]).get("ref") is not None]
    proposal_refs = [e["ref"] for e in emitted if "proposed_disposition" in e]
    assert proposal_refs == ["ref-good"], f"expected only ref-good, got {proposal_refs}"
    assert out.proposed == ["ref-good"]


def _imported_modules(path: str) -> set[str]:
    import ast
    src = pathlib.Path(path).read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module)
    return imported


_FORBIDDEN_ACTOR_PATHS = (
    "tpcore.risk", "tpcore.order_management",
    "tpcore.selfheal.orchestrator", "tpcore.selfheal.runner",
    "tpcore.auditheal", "tpcore.datasupervisor", "scripts.ops",
)


async def test_import_isolation_no_actor_paths() -> None:
    # The agent must NOT import any actor/mutation path.
    imported = _imported_modules("ops/llm_data_triage.py")
    bad = [m for m in imported for f in _FORBIDDEN_ACTOR_PATHS
           if m == f or m.startswith(f + ".")]
    assert bad == [], f"agent imports fenced actor path(s): {bad}"


async def test_import_isolation_daemon_no_actor_paths() -> None:
    # P3 §4c: the event-driven triage daemon must ALSO import no
    # actor/mutation path — same forbidden set as the agent. (It
    # imports only ops.llm_data_triage.run_triage + stdlib/asyncpg/
    # structlog.) This test still BITES: adding any forbidden import
    # to ops/llm_triage_service.py fails it.
    imported = _imported_modules("ops/llm_triage_service.py")
    bad = [m for m in imported for f in _FORBIDDEN_ACTOR_PATHS
           if m == f or m.startswith(f + ".")]
    assert bad == [], f"daemon imports fenced actor path(s): {bad}"


def test_default_client_is_async_anthropic() -> None:
    """The data-lane triage agent runs inside the long-lived
    ``ops/llm_triage_service.py`` async daemon event loop. A *sync*
    ``anthropic.Anthropic`` whose ``messages.create`` is ``await``ed
    blocks the whole event loop for the full LLM round-trip (seconds),
    starving the crash-isolated co-tasks + the poll loop. The default
    client MUST be the official async client so the round-trip yields.
    Bite: revert ``_default_client`` to ``Anthropic()`` → this fails.
    """
    import anthropic

    client = lt._default_client()
    assert isinstance(client, anthropic.AsyncAnthropic), (
        f"_default_client must return AsyncAnthropic (non-blocking in "
        f"the daemon event loop), got {type(client).__name__}"
    )


async def test_awaits_async_client_create_and_emits(monkeypatch) -> None:
    """End-to-end with the async-create client (``_Client.messages.create``
    is an ``async def`` — the real AsyncAnthropic shape): the agent must
    ``await`` ``messages.create`` — a coroutine — and still produce the
    proposal incl. the ``usage`` token counts off the awaited Message. If
    the agent ever stops awaiting (sync-client regression), this breaks.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    rec: list = []
    pool = _Pool(open_rows=[_row("h1")])
    out = await lt.run_triage(pool, client_factory=lambda: _Client(rec))
    assert len(rec) == 1
    kw = rec[0]
    assert kw["model"] == "claude-sonnet-4-6"
    assert "tools" not in kw
    ev = [json.loads(a[5]) for a in pool.conn.emitted]
    prop = next(e for e in ev)
    assert prop["ref"] == "h1" and prop["proposed_disposition"] == "converted"
    assert prop["usage"] == {"in": 11, "out": 22}
    assert out.proposed == ["h1"]


def test_leak_guard_fails_loud_when_git_absent(monkeypatch) -> None:  # noqa: ANN001
    # The host-repo leak guard must be incapable of a silent
    # false-negative: if git can't even run, it must ERROR the test,
    # NEVER return [] (which would make the pre/post leak asserts
    # vacuously pass). Bite: revert _host_llm_triage_branches to
    # check=False / swallow → this test fails.
    def _git_absent(*_a, **_k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", _git_absent)
    with pytest.raises(RuntimeError,
                       match="host-repo leak guard could not run git"):
        _host_llm_triage_branches()


def test_leak_guard_fails_loud_on_nonzero_git(monkeypatch) -> None:  # noqa: ANN001
    # Same incapability-of-false-negative invariant for a non-zero git
    # exit (e.g. not-a-repo): must raise, not return [].
    def _git_fails(*_a, **_k):
        raise subprocess.CalledProcessError(
            128, ["git"], output="", stderr="fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", _git_fails)
    with pytest.raises(RuntimeError,
                       match="host-repo leak guard could not run git"):
        _host_llm_triage_branches()
