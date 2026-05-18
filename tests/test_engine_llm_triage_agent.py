"""Epic E / Phase 2: the engine-lane agent calls the official SDK
(mocked), emits a non-authoritative ENGINE_LLM_TRIAGE_PROPOSAL via an
engine-lane `_INSERT_SQL` byte-mirroring `ops.engine_ladder._INSERT_SQL`,
never passes `tools`, no-ops without a key, AuthenticationError is safe +
zero retries, RuntimeError is crash-isolated, malformed responses are
per-escalation-isolated, and the agent's import closure pulls NO
actor/mutation path. No live API calls.

This MIRRORS the shipped #187 agent/PR test technique
(`tests/test_llm_data_triage_agent.py` + `tpcore/tests/
test_llm_data_triage_pr.py`) engine-flavoured: importlib-load
`ops/engine_llm_triage.py` (dodges the `scripts/ops.py`↔`ops/` shadow —
the data-lane precedent), a LOUD host-repo guard that NEVER returns []
silently, and an injected fake `pr_runner` so NO real git/gh ever
touches the live host repo (advisory-only, live-money platform).
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
    "eng_lt_agent",
    pathlib.Path(__file__).resolve().parents[1]
    / "ops" / "engine_llm_triage.py")
elt = importlib.util.module_from_spec(_spec)
sys.modules["eng_lt_agent"] = elt
_spec.loader.exec_module(elt)

_HOST_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _host_llm_triage_branches() -> list[str]:
    """Every `llm-triage/*` local branch in the LIVE host repo (empty on
    a clean repo). The regression bite: a test that leaks a real
    `git worktree add -b llm-triage/<ref>` shows up here.

    Fails LOUD on its own failure (git absent / non-zero exit) — a guard
    that returned [] when it could not run git would let the pre/post
    leak asserts vacuously pass (a silent false-negative). The only
    paths out are a positively-confirmed branch list or a raised error
    that ERRORs the test."""
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
    ``pytest``/``gh pr create`` against the live host repo. ``pr_runner``
    is a keyword-only arg whose default is bound at def-time and lives in
    ``run_triage.__kwdefaults__`` — patching ``elt._default_pr_runner``
    alone would NOT take effect, so the bound default is what we swap
    (and ``elt._default_pr_runner`` too, for any direct reference).
    (2) Assert the host repo carries no ``llm-triage/*`` branch before
    AND after the test — the structural regression bite if a real-repo
    git call is ever reintroduced."""
    pre = _host_llm_triage_branches()
    assert pre == [], (
        f"host repo dirty BEFORE test (pre-existing leak): {pre}")

    def _fake_pr_runner(argv, *, env=None, cwd=None):  # noqa: ANN001
        if argv and argv[0] == "gh":
            return 0, "https://github.com/x/y/pull/1", ""
        return 0, "", ""

    orig_attr = elt._default_pr_runner
    orig_kwd = dict(elt.run_triage.__kwdefaults__)
    elt._default_pr_runner = _fake_pr_runner
    elt.run_triage.__kwdefaults__["pr_runner"] = _fake_pr_runner
    try:
        yield
    finally:
        elt._default_pr_runner = orig_attr
        elt.run_triage.__kwdefaults__["pr_runner"] = orig_kwd["pr_runner"]
        post = _host_llm_triage_branches()
        assert post == [], (
            "host repo MUTATED by this test — a real-repo `git worktree "
            f"add -b llm-triage/<ref>` leaked branch(es): {post}. The "
            "Phase-2 agent test must NOT exercise a real git path; the "
            "sandbox/PR path is exercised via an injected fake runner.")


# ── SDK fakes (mirror tests/test_llm_data_triage_agent.py) ───────────────


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


class _EmptyContentMsg:
    def __init__(self):
        self.content = []
        self.stop_reason = "end_turn"
        self.usage = _Usage()


class _Messages:
    def __init__(self, rec): self._rec = rec
    def create(self, **kw):
        self._rec.append(kw)
        return _Msg(json.dumps({
            "proposed_disposition": "structural", "confidence": 0.7,
            "rationale": "r", "could_not_determine": "n"}))


class _Client:
    def __init__(self, rec): self.messages = _Messages(rec)


class _MultiMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
    def create(self, **kw):
        r = self._responses[self._idx]
        self._idx += 1
        return r


class _MultiClient:
    def __init__(self, responses):
        self.messages = _MultiMessages(responses)


# ── Fake pool. select_novel_escalations calls
# engine_ladder.list_undispositioned(pool) then fetches prior proposals;
# build_packet calls supervisor_state.current_hold + a forensics fetch.
# We monkeypatch the select/packet seams so the agent test stays focused
# on the SDK/emit/PR/fail-safe contract (Phase-1 select/packet have
# their own unit tests). ────────────────────────────────────────────────


class _Conn:
    def __init__(self, p): self._p = p; self.emitted = []
    async def fetch(self, sql, *a):
        return []
    async def execute(self, sql, *a):
        self._p.exec_sql.append(sql)
        self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self):
        self.conn = _Conn(self)
        self.exec_sql: list[str] = []
    def acquire(self): return _CM(self.conn)


def _esc(hold_id="h1"):
    from tpcore.engine_llm_triage.select import EngineNovelEscalation
    return EngineNovelEscalation(
        hold_id=hold_id, engine="momentum",
        failure_class="scheduler_crash", reason="boom",
        recorded_at=datetime(2026, 5, 1, tzinfo=UTC), shape="held",
        policy_default="structural", policy_rationale="why")


def _pkt():
    from tpcore.engine_llm_triage.packet import EngineTriagePacket
    return EngineTriagePacket(text='{"k":"v"}', packet_hash="deadbeef")


@pytest.fixture()
def _seam(monkeypatch):
    """Inject the Phase-1 select/packet seams so the agent test isolates
    the Phase-2 SDK/emit/PR/fail-safe contract."""
    def _make(escs):
        async def _fake_select(pool):
            return list(escs)

        async def _fake_packet(pool, esc):
            return _pkt()

        monkeypatch.setattr(elt, "select_novel_escalations", _fake_select)
        monkeypatch.setattr(elt, "build_packet", _fake_packet)
    return _make


# ── (1) SDK call shape + ENGINE_LLM_TRIAGE_PROPOSAL emit ────────────────


async def test_calls_sdk_no_tools_and_emits_engine_proposal(
    monkeypatch, _seam,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _seam([_esc("h1")])
    rec: list = []
    pool = _Pool()
    out = await elt.run_triage(pool, client_factory=lambda: _Client(rec))

    assert len(rec) == 1
    kw = rec[0]
    assert kw["temperature"] == 0.0
    assert "tools" not in kw                       # structural: never acts
    assert kw["messages"][0]["role"] == "user"
    assert isinstance(kw["system"], str) and kw["system"]
    # persona system prompt is the engine persona (Role line present).
    assert "engine-lane triage" in kw["system"]

    ev = [json.loads(a[5]) for a in pool.conn.emitted]
    assert len(ev) == 1
    prop = ev[0]
    assert prop["schema"] == 1
    assert prop["hold_id"] == "h1"
    assert prop["failure_class"] == "scheduler_crash"
    assert prop["engine"] == "momentum"
    assert prop["proposed_disposition"] == "structural"
    assert prop["confidence"] == 0.7
    assert prop["could_not_determine"] == "n"
    assert prop["packet_hash"] == "deadbeef"
    assert prop["persona_version"] == elt.PERSONA_VERSION
    assert prop["usage"] == {"in": 11, "out": 22}
    assert "model" in prop
    # event_type recorded.
    ev_args = pool.conn.emitted[0]
    assert ev_args[2] == "ENGINE_LLM_TRIAGE_PROPOSAL"
    assert out.proposed == ["h1"]
    assert out.error is None


async def test_proposed_disposition_is_existing_engine_verb(
    monkeypatch, _seam,
) -> None:
    """The persona constrains the LLM to an EXISTING
    EngineEscalationDisposition value; the agent passes it through
    verbatim (the deterministic fence — not this module — enforces it).
    The fake returns 'structural' which IS a valid verb."""
    from ops.engine_ladder import EngineEscalationDisposition
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _seam([_esc("h1")])
    pool = _Pool()
    await elt.run_triage(pool, client_factory=lambda: _Client([]))
    prop = json.loads(pool.conn.emitted[0][5])
    EngineEscalationDisposition(prop["proposed_disposition"])  # no raise


# ── (2) engine _INSERT_SQL byte-parity with ops.engine_ladder ───────────


def test_insert_sql_byte_mirrors_engine_ladder() -> None:
    from ops import engine_ladder
    assert elt._INSERT_SQL == engine_ladder._INSERT_SQL, (
        "engine agent _INSERT_SQL must byte-mirror "
        "ops.engine_ladder._INSERT_SQL (engine-lane insert convention)")


# ── (3) no-key safe no-op (zero SDK calls, zero emits) ──────────────────


async def test_no_api_key_is_safe_noop(monkeypatch, _seam) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _seam([_esc("h1")])
    rec: list = []
    pool = _Pool()
    out = await elt.run_triage(pool, client_factory=lambda: _Client(rec))
    assert rec == []
    assert pool.conn.emitted == []
    assert out.proposed == []
    assert out.skipped_no_key is True
    assert out.error is None


# ── (4) AuthenticationError → safe + ZERO retries ───────────────────────


async def test_auth_error_is_safe_like_no_key_zero_retries(
    monkeypatch, _seam,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-key")
    _seam([_esc("h1")])

    import anthropic

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

    pool = _Pool()
    out = await elt.run_triage(pool, client_factory=lambda: _AuthClient())

    assert call_count == 1, f"expected 1 call, got {call_count} (retry bug)"
    assert pool.conn.emitted == []
    assert out.error is None
    assert out.skipped_no_key is True
    assert out.proposed == []


# ── (5) RuntimeError → crash-isolated (out.error, never raises) ─────────


async def test_runtime_error_is_crash_isolated(monkeypatch, _seam) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _seam([_esc("h1")])

    class _Boom:
        @property
        def messages(self):
            class M:
                def create(self, **kw): raise RuntimeError("api down")
            return M()

    pool = _Pool()
    out = await elt.run_triage(pool, client_factory=lambda: _Boom())
    assert pool.conn.emitted == []          # no proposal on failure
    assert out.error is not None            # never raises
    assert out.proposed == []


# ── (6) malformed response → per-escalation isolated, batch continues ──


async def test_empty_content_skips_one_not_batch(monkeypatch, _seam) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _seam([_esc("bad"), _esc("good")])
    valid = _Msg(json.dumps({
        "proposed_disposition": "structural", "confidence": 0.9,
        "rationale": "ok", "could_not_determine": "n"}))
    pool = _Pool()
    out = await elt.run_triage(
        pool, client_factory=lambda: _MultiClient([_EmptyContentMsg(), valid]))

    assert out.error is None, f"batch aborted: out.error={out.error!r}"
    refs = [json.loads(a[5])["hold_id"] for a in pool.conn.emitted]
    assert refs == ["good"], f"expected only good, got {refs}"
    assert out.proposed == ["good"]


async def test_non_dict_json_skips_one_not_batch(monkeypatch, _seam) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _seam([_esc("nulled"), _esc("good")])
    valid = _Msg(json.dumps({
        "proposed_disposition": "structural", "confidence": 0.9,
        "rationale": "ok", "could_not_determine": "n"}))
    pool = _Pool()
    out = await elt.run_triage(
        pool, client_factory=lambda: _MultiClient([_Msg("null"), valid]))

    assert out.error is None, f"batch aborted: out.error={out.error!r}"
    refs = [json.loads(a[5])["hold_id"] for a in pool.conn.emitted]
    assert refs == ["good"], f"expected only good, got {refs}"
    assert out.proposed == ["good"]


async def test_unparseable_text_skips_one_not_batch(
    monkeypatch, _seam,
) -> None:
    """A non-JSON text body (json.JSONDecodeError) is per-escalation
    isolated, not a batch abort."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _seam([_esc("garbage"), _esc("good")])
    valid = _Msg(json.dumps({
        "proposed_disposition": "structural", "confidence": 0.9,
        "rationale": "ok", "could_not_determine": "n"}))
    pool = _Pool()
    out = await elt.run_triage(
        pool,
        client_factory=lambda: _MultiClient([_Msg("not json at all"), valid]))

    assert out.error is None
    refs = [json.loads(a[5])["hold_id"] for a in pool.conn.emitted]
    assert refs == ["good"]
    assert out.proposed == ["good"]


# ── (7) import-isolation AST guard — must BITE ──────────────────────────


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


# The agent must NOT import any actor/mutation path. It MAY import
# `ops.llm_data_triage` (the shipped SDK/PR wrapper it reuses verbatim)
# and the Phase-1 `tpcore.engine_llm_triage` package (whose select/packet
# lazy-import `ops.engine_ladder` read predicates at CALL time — never at
# module load). It must NOT statically import `ops.engine_ladder`
# mechanism, `ops.engine_supervisor`, `ops.aar_autotune`, `tpcore.risk`,
# or `tpcore.order_management`.
_FORBIDDEN_ACTOR_PATHS = (
    "tpcore.risk", "tpcore.order_management",
    "ops.engine_supervisor", "ops.aar_autotune", "ops.engine_ladder",
    "tpcore.supervisor_state", "scripts.ops",
)


def test_import_isolation_no_actor_paths() -> None:
    imported = _imported_modules("ops/engine_llm_triage.py")
    bad = [m for m in imported for f in _FORBIDDEN_ACTOR_PATHS
           if m == f or m.startswith(f + ".")]
    assert bad == [], f"engine agent imports fenced actor path(s): {bad}"


def _module_level_imports(path: str) -> set[str]:
    """ONLY the imports at module body level (not nested in a
    def/class) — a function-body `from ops import X` (the lazy
    `_shipped()` / Phase-1 `_engine_ladder()` precedent) is correct and
    must NOT be flagged; only a TOP-LEVEL one breaks the shadow."""
    import ast
    tree = ast.parse(pathlib.Path(path).read_text())
    out: set[str] = set()
    for node in tree.body:  # module body only — not ast.walk
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_no_top_level_ops_import_under_shadow() -> None:
    """The agent must NOT import ANY `ops.*` submodule at MODULE LOAD —
    not even the shipped advisory `ops.llm_data_triage` it reuses. It
    reaches that wrapper via the lazy `_shipped()` accessor (the
    Phase-1 `_engine_ladder()` precedent) so module-load never binds
    `sys.modules['ops']` under the documented `scripts/ops.py`↔`ops/`
    test shadow. A regression to a top-level `from ops.… import` fails
    full-suite collection — this guard catches it FIRST. (A
    function-body lazy `from ops import …` is correct and is NOT
    flagged.)"""
    top = _module_level_imports("ops/engine_llm_triage.py")
    static_ops = [m for m in top if m == "ops" or m.startswith("ops.")]
    assert static_ops == [], (
        "engine agent TOP-LEVEL imports ops.* (breaks the "
        f"scripts/ops.py↔ops/ shadow): {static_ops}. Use _shipped().")


def test_top_level_import_guard_actually_bites(tmp_path) -> None:
    """Prove the module-level guard is not vacuous: a top-level
    `from ops.x import y` IS flagged, while the same import nested in a
    function body is NOT (the legitimate lazy pattern)."""
    bad = tmp_path / "bad.py"
    bad.write_text("from ops.llm_data_triage import run_triage\n")
    assert "ops.llm_data_triage" in _module_level_imports(str(bad))

    good = tmp_path / "good.py"
    good.write_text(
        "def _shipped():\n"
        "    from ops import llm_data_triage\n"
        "    return llm_data_triage\n")
    top = _module_level_imports(str(good))
    assert not any(m == "ops" or m.startswith("ops.") for m in top)


def test_import_isolation_guard_actually_bites(tmp_path) -> None:
    """Prove the AST guard is not vacuous: a synthetic module that
    imports a forbidden actor path is detected."""
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "import os\n"
        "from ops.engine_ladder import disposition\n"
        "from tpcore.risk import RiskGovernor\n")
    imported = _imported_modules(str(rogue))
    bad = [m for m in imported for f in _FORBIDDEN_ACTOR_PATHS
           if m == f or m.startswith(f + ".")]
    assert "ops.engine_ladder" in bad
    assert "tpcore.risk" in bad


# ── (8) sandbox/PR path via injected fake runner ────────────────────────


_FORBIDDEN_ENV_SUBSTRINGS = (
    "DATABASE_URL", "ANTHROPIC", "ALPACA", "SUPABASE", "TOKEN", "KEY",
)


def _assert_env_scrubbed(env: dict) -> None:
    assert env is not None, "gate ran with env=None (inherits os.environ!)"
    for k in env:
        up = k.upper()
        assert not any(s in up for s in _FORBIDDEN_ENV_SUBSTRINGS), (
            f"forbidden var {k!r} leaked into the sandbox child-env")
    for k in env:
        assert (k in ("PATH", "HOME", "LANG")
                or k.upper().startswith("PYTHON")), (
            f"non-allowlisted var {k!r} in sandbox child-env")


class _FakeRunner:
    def __init__(self, *, gate_rc: int = 0, pr_raises: bool = False) -> None:
        self.gate_rc = gate_rc
        self.pr_raises = pr_raises
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, argv, *, env=None, cwd=None):  # noqa: ANN001
        self.calls.append((list(argv), dict(env) if env is not None else None))
        joined = " ".join(argv)
        if "worktree" in argv and "remove" in argv:
            return 0, "", ""
        if "worktree" in argv and "add" in argv:
            return 0, "", ""
        if argv[0] == "git":
            return 0, "", ""
        if "pytest" in joined or argv[0] == "ruff" or "ruff" in argv:
            return self.gate_rc, "", ("gate red" if self.gate_rc else "")
        if argv[0] == "gh":
            if self.pr_raises:
                raise RuntimeError("gh pr create exploded")
            return 0, "https://github.com/x/y/pull/1", ""
        return 0, "", ""

    def argvs(self) -> list[list[str]]:
        return [c[0] for c in self.calls]

    def gate_envs(self) -> list[dict]:
        return [c[1] for c in self.calls
                if c[1] is not None
                and ("pytest" in " ".join(c[0]) or "ruff" in c[0])]

    def pr_created(self) -> bool:
        return any(c[0] and c[0][0] == "gh" and "create" in c[0]
                   for c in self.calls)


def _seed_secrets(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("ALPACA_API_KEY", "alp-secret")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sup-secret")
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp/home")


async def test_green_gate_opens_draft_pr_scrubbed_env(
    monkeypatch, _seam,
) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool()
    out = await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)

    assert out.proposed == ["h1"]            # advisory preserved
    assert runner.pr_created()
    gh = next(c for c in runner.argvs() if c and c[0] == "gh")
    assert "--draft" in gh
    assert "engine-llm-triage" in gh
    assert "merge" not in gh                 # NEVER gh pr merge
    envs = runner.gate_envs()
    assert envs, "gate never ran with an explicit env"
    for e in envs:
        _assert_env_scrubbed(e)
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())


async def test_red_gate_no_pr_but_proposal_kept_and_wt_removed(
    monkeypatch, _seam,
) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=1)
    pool = _Pool()
    out = await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)

    assert out.proposed == ["h1"]            # advisory preserved
    assert not runner.pr_created()           # NO PR on red gate
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())      # worktree STILL removed
    # proposal STILL emitted.
    assert any(json.loads(a[5]).get("hold_id") == "h1"
               for a in pool.conn.emitted)


async def test_pr_failure_still_emits_proposal_and_cleans_up(
    monkeypatch, _seam,
) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=0, pr_raises=True)
    pool = _Pool()
    out = await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)

    assert out.proposed == ["h1"]
    assert out.error is None
    assert any(json.loads(a[5]).get("hold_id") == "h1"
               for a in pool.conn.emitted)
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())


async def test_branch_deleted_so_retry_not_wedged(monkeypatch, _seam) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool()
    await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)
    deletes = [c for c in runner.argvs() if c[:3] == ["git", "branch", "-D"]]
    assert deletes, "branch was NOT deleted — a same-ref retry is wedged"
    assert deletes[0][3] == "llm-triage/h1"


async def test_never_calls_gh_pr_merge(monkeypatch, _seam) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool()
    await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)
    for argv in runner.argvs():
        assert not (argv and argv[0] == "gh" and "merge" in argv), (
            f"FORBIDDEN: agent invoked `gh pr merge`: {argv}")


async def test_env_scrub_excludes_every_forbidden_var(
    monkeypatch, _seam,
) -> None:
    _seed_secrets(monkeypatch)
    _seam([_esc("h1")])
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool()
    await elt.run_triage(
        pool, client_factory=lambda: _Client([]), pr_runner=runner)
    for e in runner.gate_envs():
        assert "ANTHROPIC_API_KEY" not in e
        assert "DATABASE_URL" not in e
        assert "ALPACA_API_KEY" not in e
        assert "SUPABASE_SERVICE_KEY" not in e
        assert "GH_TOKEN" not in e
        _assert_env_scrubbed(e)


# ── host-repo leak guard fails LOUD (no silent false-negative) ──────────


def test_leak_guard_fails_loud_when_git_absent(monkeypatch) -> None:
    def _git_absent(*_a, **_k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", _git_absent)
    with pytest.raises(RuntimeError,
                       match="host-repo leak guard could not run git"):
        _host_llm_triage_branches()


def test_leak_guard_fails_loud_on_nonzero_git(monkeypatch) -> None:
    def _git_fails(*_a, **_k):
        raise subprocess.CalledProcessError(
            128, ["git"], output="", stderr="fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", _git_fails)
    with pytest.raises(RuntimeError,
                       match="host-repo leak guard could not run git"):
        _host_llm_triage_branches()


# ── (#244) published shared-SDK surface — clockwork contract guard ──────
#
# The engine lane reuses the SHIPPED #187 `ops.llm_data_triage` SDK/PR
# wrapper. Spec decision (Epic E §3 FORK-A pt 4 / follow-up #244): it
# consumes ONLY a PUBLIC re-export surface, never the underscore privates
# (the original `_AuthSkip`/`_MODEL`/`_MAX_TOKENS`/`_scrubbed_env`/
# `_default_pr_runner` spelunking was a rename foot-gun). These guards
# convert that into an ENFORCED contract: a regression to private
# spelunking, or a broken (non-identity) alias, fails the build LOUD.


_PUBLIC_SURFACE = (
    # public name -> the shipped private it must be an identity alias of
    ("AuthSkip", "_AuthSkip"),
    ("ANTHROPIC_MODEL", "_MODEL"),
    ("ANTHROPIC_MAX_TOKENS", "_MAX_TOKENS"),
    ("scrubbed_env", "_scrubbed_env"),
    ("default_pr_runner", "_default_pr_runner"),
)


def test_public_surface_is_identity_preserving_alias() -> None:
    """Each published public name is bound to the EXACT SAME object as
    the shipped private (object identity — `is`), so the engine lane's
    `except _AuthSkip` / `_MODEL` reuse keeps matching the shipped
    objects byte-for-byte. A duplicated/re-authored value (alias drift)
    fails here."""
    from ops import llm_data_triage as ldt

    for pub, priv in _PUBLIC_SURFACE:
        assert hasattr(ldt, pub), (
            f"published #244 public name {pub!r} missing from "
            "ops.llm_data_triage — the shared-SDK contract is broken")
        assert getattr(ldt, pub) is getattr(ldt, priv), (
            f"public {pub!r} is NOT the same object as private {priv!r} "
            "— alias drift (value duplicated instead of aliased); the "
            "engine lane's object-identity reuse breaks silently")
        assert pub in ldt.__all__, (
            f"{pub!r} missing from ops.llm_data_triage.__all__ — the "
            "published shared-SDK surface must be in __all__")


def _data_lane_private_attr_accesses(path: str) -> list[str]:
    """Every ``<name>.<_underscore_attr>`` access in `path` where
    ``<name>`` resolves to the shipped data-lane wrapper — i.e. the
    `_shipped()` accessor result (or a var assigned from it). Returns the
    list of private attribute names spelunked (empty == compliant)."""
    import ast

    tree = ast.parse(pathlib.Path(path).read_text())

    # Names that hold the data-lane module: the `_shipped()` call result
    # and any local bound directly to it (e.g. `m = _shipped()`).
    shipped_bound: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_shipped"):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    shipped_bound.add(tgt.id)

    leaks: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not node.attr.startswith("_"):
            continue
        val = node.value
        # `_shipped()._private` (Attribute on a direct Call to _shipped)
        is_direct_call = (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Name)
            and val.func.id == "_shipped")
        # `m._private` where `m = _shipped()`
        is_bound_var = (
            isinstance(val, ast.Name) and val.id in shipped_bound)
        if is_direct_call or is_bound_var:
            leaks.append(node.attr)
    return leaks


def test_engine_lane_consumes_only_public_surface() -> None:
    """CLOCKWORK GUARD (#244): `ops/engine_llm_triage.py` accesses NO
    underscore-prefixed attribute on the shipped `ops.llm_data_triage`
    wrapper (reached via `_shipped()`). The engine lane must consume the
    PUBLIC re-export surface ONLY — private spelunking is a rename
    foot-gun (a future `ops.llm_data_triage` private rename would
    silently require an `ops/engine_llm_triage` change). If this bites,
    re-point the engine reference at the published public alias."""
    engine_triage_src = (_HOST_REPO_ROOT / "ops" / "engine_llm_triage.py").read_text()
    assert "_shipped" in engine_triage_src, (
        "guard assumption broken: the _shipped accessor was renamed — "
        "update _data_lane_private_attr_accesses + this guard together")
    leaks = _data_lane_private_attr_accesses(
        str(_HOST_REPO_ROOT / "ops" / "engine_llm_triage.py"))
    assert leaks == [], (
        "engine lane spelunks ops.llm_data_triage PRIVATE symbol(s) "
        f"{sorted(set(leaks))} — use the published public shared-SDK "
        "surface (#244): "
        + ", ".join(f"{priv}->{pub}" for pub, priv in _PUBLIC_SURFACE))


def test_private_spelunk_guard_actually_bites(tmp_path) -> None:
    """Prove the clockwork guard is NOT vacuous: a synthetic module that
    spelunks a data-lane private (both the direct-call and the
    bound-var form) IS flagged, while consuming only the public surface
    is NOT."""
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "def _shipped():\n"
        "    from ops import llm_data_triage\n"
        "    return llm_data_triage\n"
        "def f():\n"
        "    a = _shipped()._AuthSkip\n"
        "    m = _shipped()\n"
        "    return a, m._MODEL\n")
    leaks = _data_lane_private_attr_accesses(str(rogue))
    assert "_AuthSkip" in leaks and "_MODEL" in leaks

    clean = tmp_path / "clean.py"
    clean.write_text(
        "def _shipped():\n"
        "    from ops import llm_data_triage\n"
        "    return llm_data_triage\n"
        "def f():\n"
        "    a = _shipped().AuthSkip\n"
        "    m = _shipped()\n"
        "    return a, m.ANTHROPIC_MODEL\n")
    assert _data_lane_private_attr_accesses(str(clean)) == []


def test_clockwork_guard_fails_loud_on_accessor_rename(tmp_path) -> None:
    """Prove the hardened guard raises LOUD (not vacuous-pass) when the
    ``_shipped`` accessor is benignly renamed.

    Pre-fix behaviour: ``_data_lane_private_attr_accesses`` returned [] on a
    renamed accessor because no ``_shipped`` calls existed to track —
    ``test_engine_lane_consumes_only_public_surface`` would have passed
    vacuously even if the module still spelunked a private via the new name.

    Post-fix behaviour: the explicit ``assert "_shipped" in src`` guard fires
    with an ``AssertionError`` BEFORE the scanner runs, so the rename is
    caught loud instead of silently ignored.
    """
    # Synthetic engine module where the accessor is renamed to `_data_lane`
    # but it still spelunks a private attribute — this is the benign-rename
    # + live-spelunk scenario the guard must catch.
    renamed = tmp_path / "engine_renamed.py"
    renamed.write_text(
        "def _data_lane():\n"
        "    from ops import llm_data_triage\n"
        "    return llm_data_triage\n"
        "def f():\n"
        "    m = _data_lane()\n"
        "    return m._MODEL\n")

    renamed_src = renamed.read_text()

    # Pre-fix behaviour: scanner returns [] (no _shipped → nothing tracked)
    # which means the old guard would have passed vacuously.
    assert "_shipped" not in renamed_src, (
        "test setup broken: renamed module must NOT contain '_shipped'")
    pre_fix_leaks = _data_lane_private_attr_accesses(str(renamed))
    assert pre_fix_leaks == [], (
        "pre-fix scanner correctly returns empty on a renamed accessor "
        "(confirming the vacuous-pass risk)")

    # Post-fix behaviour: the hardened assertion fires as an AssertionError.
    import pytest
    with pytest.raises(AssertionError, match="guard assumption broken"):
        assert "_shipped" in renamed_src, (
            "guard assumption broken: the _shipped accessor was renamed — "
            "update _data_lane_private_attr_accesses + this guard together")
