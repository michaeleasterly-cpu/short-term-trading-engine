"""LT-P3 §2: the agent, on a produced proposal, builds an env-SCRUBBED
sandbox worktree with ONLY additive content (HealSpec binding + dossier),
runs a read-only local gate, and opens a DRAFT human-merge-only PR.

Sacred invariants asserted here:
  * the constructed child-env has NONE of the forbidden secret vars
    (built as a fresh allowlisted dict — never os.environ.copy())
  * the worktree is ALWAYS removed (even on gate failure)
  * a PR-creation failure STILL emits the advisory proposal (no raise)
  * NO PR is opened when the local gate is red

Fake git/gh via an injected runner; no real subprocess, no LLM, no DB.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, datetime

import pytest

# Multi-line spec_from_file_location of an ops/ module — grouped by the
# over-inclusion rule (verified non-poisoning, but safe to co-locate).
pytestmark = pytest.mark.xdist_group("ops_shadow")

_spec = importlib.util.spec_from_file_location(
    "lt_pr_agent",
    pathlib.Path(__file__).resolve().parents[2] / "ops" / "llm_data_triage.py")
lt = importlib.util.module_from_spec(_spec)
sys.modules["lt_pr_agent"] = lt
_spec.loader.exec_module(lt)


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


class _Messages:
    # async — mirrors the real anthropic.AsyncAnthropic.messages.create
    # coroutine fn the agent now awaits inside the daemon event loop.
    async def create(self, **kw):
        return _Msg(json.dumps({
            "proposed_disposition": "converted", "confidence": "high",
            "rationale": "r", "could_not_determine": "n"}))


class _Client:
    def __init__(self): self.messages = _Messages()


class _Conn:
    def __init__(self, p): self._p = p; self.emitted = []
    async def fetch(self, sql, *a):
        if "OPEN_ESCALATIONS" in sql:
            return [dict(r) for r in self._p.open_rows]
        if "DATA_LLM_TRIAGE_PROPOSAL" in sql:
            return []
        return []
    async def execute(self, sql, *a): self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, open_rows=()):
        self.open_rows = list(open_rows)
        self.conn = _Conn(self)
    def acquire(self): return _CM(self.conn)


def _row(ref="h1"):
    return {"ref": ref, "etype": "DATA_SOURCE_ESCALATED",
            "cls": "event:DATA_SOURCE_ESCALATED",
            "recorded_at": datetime(2026, 5, 1, tzinfo=UTC), "message": "m"}


# ── Fake command runner: records every (argv, env) invocation ────────────


class _FakeRunner:
    """Injected in place of the real subprocess runner. ``gate_rc`` is
    the simulated `pytest -q` / `ruff` exit code; ``pr_raises`` makes
    `gh pr create` raise (PR-creation failure path)."""

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

    # convenience accessors
    def argvs(self) -> list[list[str]]:
        return [c[0] for c in self.calls]

    def gate_envs(self) -> list[dict]:
        return [c[1] for c in self.calls
                if c[1] is not None
                and ("pytest" in " ".join(c[0]) or "ruff" in c[0])]

    def pr_created(self) -> bool:
        return any(c[0] and c[0][0] == "gh" and "create" in c[0]
                   for c in self.calls)


_FORBIDDEN_ENV_SUBSTRINGS = (
    "DATABASE_URL", "ANTHROPIC", "ALPACA", "SUPABASE", "TOKEN", "KEY",
)


def _assert_env_scrubbed(env: dict) -> None:
    assert env is not None, "gate ran with env=None (inherits os.environ!)"
    for k in env:
        up = k.upper()
        assert not any(s in up for s in _FORBIDDEN_ENV_SUBSTRINGS), (
            f"forbidden var {k!r} leaked into the sandbox child-env")
    # Only the documented allowlist may be present.
    for k in env:
        assert (k in ("PATH", "HOME", "LANG")
                or k.upper().startswith("PYTHON")), (
            f"non-allowlisted var {k!r} in sandbox child-env")


def _seed_secrets(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("ALPACA_API_KEY", "alp-secret")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sup-secret")
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp/home")


# ── (a) Happy path: gate green → DRAFT PR, env scrubbed, wt removed ──────


async def test_green_gate_opens_draft_pr_with_scrubbed_env(monkeypatch) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool(open_rows=[_row("h1")])

    out = await lt.run_triage(
        pool, client_factory=lambda: _Client(),
        pr_runner=runner)

    assert out.proposed == ["h1"]            # advisory preserved
    assert runner.pr_created()               # a PR was opened
    # It must be a DRAFT PR with the llm-data-triage label.
    gh = next(c for c in runner.argvs() if c and c[0] == "gh")
    assert "--draft" in gh
    assert "llm-data-triage" in gh
    # Env-scrub: the gate child-env has NO forbidden var and is the
    # constructed allowlist dict (proven by inspecting every captured env).
    envs = runner.gate_envs()
    assert envs, "gate never ran with an explicit env"
    for e in envs:
        _assert_env_scrubbed(e)
    # worktree always removed.
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())


# ── (b) Gate RED → NO PR, worktree still removed, proposal still emitted ─


async def test_red_gate_no_pr_but_worktree_removed_and_proposal_kept(
    monkeypatch,
) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=1)  # pytest/ruff fail in the sandbox
    pool = _Pool(open_rows=[_row("h1")])

    out = await lt.run_triage(
        pool, client_factory=lambda: _Client(),
        pr_runner=runner)

    assert out.proposed == ["h1"]            # advisory preserved
    assert not runner.pr_created()           # NO PR on a red gate
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())      # worktree STILL removed


# ── (c) PR-creation failure → still emits proposal, no leaked worktree ───


async def test_pr_failure_still_emits_proposal_and_cleans_up(
    monkeypatch,
) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=0, pr_raises=True)
    pool = _Pool(open_rows=[_row("h1")])

    out = await lt.run_triage(
        pool, client_factory=lambda: _Client(),
        pr_runner=runner)

    # The proposal event is STILL emitted (advisory preserved); no raise.
    assert out.proposed == ["h1"]
    assert out.error is None
    emitted = [json.loads(a[5]) for a in pool.conn.emitted]
    assert any(e.get("ref") == "h1" for e in emitted)
    # worktree always removed even though gh raised.
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in runner.argvs())


# ── (d) Env-scrub is a fresh dict, never os.environ.copy() ──────────────


# ── (e) Branch is deleted on the gate-red path (no wedged retry) ────────


def _branch_deletes(runner: _FakeRunner) -> list[list[str]]:
    return [c for c in runner.argvs()
            if c[:3] == ["git", "branch", "-D"]]


async def test_gate_red_deletes_branch_so_retry_is_not_wedged(
    monkeypatch,
) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=1)  # gate red ⇒ no PR
    pool = _Pool(open_rows=[_row("h1")])

    out = await lt.run_triage(
        pool, client_factory=lambda: _Client(),
        pr_runner=runner)

    assert out.proposed == ["h1"]            # advisory preserved
    assert not runner.pr_created()           # NO PR on a red gate
    # `git worktree remove` does NOT delete the branch; without the
    # explicit `git branch -D` a same-ref retry would hit `worktree add
    # -b <same branch>` → rc≠0 → that ref never gets a PR again.
    deletes = _branch_deletes(runner)
    assert deletes, "branch was NOT deleted — a same-ref retry is wedged"
    assert deletes[0][3] == "llm-triage/h1"  # the exact branch removed


async def test_branch_deleted_on_success_path_too(monkeypatch) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=0)  # green ⇒ PR opened
    pool = _Pool(open_rows=[_row("h1")])

    await lt.run_triage(pool, client_factory=lambda: _Client(),
                        pr_runner=runner)

    assert runner.pr_created()
    deletes = _branch_deletes(runner)
    assert deletes and deletes[0][3] == "llm-triage/h1"


async def test_env_scrub_excludes_every_forbidden_var(monkeypatch) -> None:
    _seed_secrets(monkeypatch)
    runner = _FakeRunner(gate_rc=0)
    pool = _Pool(open_rows=[_row("h1")])

    await lt.run_triage(pool, client_factory=lambda: _Client(),
                        pr_runner=runner)

    for e in runner.gate_envs():
        # The constructed dict must not even CONTAIN the forbidden keys.
        assert "ANTHROPIC_API_KEY" not in e
        assert "DATABASE_URL" not in e
        assert "ALPACA_API_KEY" not in e
        assert "SUPABASE_SERVICE_KEY" not in e
        assert "GH_TOKEN" not in e
        _assert_env_scrubbed(e)
