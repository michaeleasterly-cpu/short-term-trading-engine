# LLM Data Triage Agent (#187, Ladder rung 5) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the LLM *data* triage agent — for a genuinely-novel data escalation it diagnoses in a credential/network-starved sandbox and produces ONLY a draft, human-merge-only PR (an additive, mechanism-free HealSpec binding to an already-proven canonical stage + a dossier), behind a deterministic, expert-vetted fence.

**Architecture:** Fence-first. P1 builds the *deterministic safety skeleton* (provenance + hard-denied-path CI checks, canary-promotion state, the novel-only trigger predicate reusing `tpcore.ladder`, the read-only packet builder, the created/versioned persona doc) with NO LLM. P2 adds the sandboxed agent + the official `anthropic` SDK call (mocked in CI) + the import-isolation clockwork guard. P3 wires it into the EXISTING `ci.yml` + data-ops flow + weekly-digest line. P4 docs + the GitHub-settings operator runbook.

**Tech Stack:** Python 3.11, official `anthropic` SDK, asyncpg, pydantic v2, pytest (`asyncio_mode=auto`), ruff, GitHub Actions. Spec: `docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md`.

---

## Ground truth (verified from code + official docs — do not re-derive/guess)

**Anthropic (context7 `/anthropics/anthropic-sdk-python` + docs.claude.com — pinned, not assumed):**
- `from anthropic import Anthropic, APIError, RateLimitError, AuthenticationError`. `Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))` (env-default).
- `client.messages.create(model=_TRIAGE_MODEL, max_tokens=_MAX_TOKENS, temperature=0.0, system=<persona str>, messages=[{"role":"user","content":<packet str>}])`. **`tools` is NEVER passed** (omitted entirely ⇒ structurally no tool-use; the SDK sends none).
- `_TRIAGE_MODEL = "claude-sonnet-4-6"` — a member of the SDK `Model` type alias (`src/anthropic/types/model.py`); Sonnet class = analytical/cost-appropriate for high-volume triage. ONE module constant (auditable/swappable); comment cites the SDK alias + `https://docs.claude.com/en/docs/models-overview`.
- Response `Message`: text = `message.content[0].text` (block `.type == "text"`); `.stop_reason` (`end_turn`/`max_tokens`/`stop_sequence`/`refusal`/…); `.usage.input_tokens`/`.usage.output_tokens` (log for cost); `.id`, `.model`.
- The SDK owns the `anthropic-version` header — we do NOT set it (we use the SDK, not raw HTTPS). Pin the dep: `anthropic>=0.40` in `pyproject.toml [project.optional-dependencies] / dependencies` (read the file; add to the same list the other runtime deps use).
- Retry/transport: wrap the `.messages.create` call body in `tpcore.outage.with_retry` (codebase SoT). `AuthenticationError` or missing key ⇒ **no-op fail-safe** (log `llm_data_triage.no_api_key`, return). `RateLimitError`/`APIError` ⇒ retried by `with_retry`; still failing ⇒ crash-isolated (log, cycle proceeds, escalation stays undispositioned).

**`tpcore.outage` API:** read `tpcore/outage.py` for the exact `with_retry` signature/usage (an existing adapter, e.g. `iborrowdesk`/`finra`, calls it — mirror that call site exactly; do not invent kwargs).

**application_log insert (mirror `ops/data_repair_service._INSERT_SQL`):**
```sql
INSERT INTO platform.application_log (engine, run_id, event_type, severity, message, data)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
```
`engine="llm_data_triage"`, `run_id`=uuid4 per emit, `data`=`json.dumps(...,default=str)`, `recorded_at` DB-assigned. Event type = `DATA_LLM_TRIAGE_PROPOSAL`.

**Escalation-ref keys (from `ops/weekly_digest.py` `-- OPEN_ESCALATIONS` query):** `DATA_REPAIR_ESCALATED` → `data->>'request_id'`; `DATA_SOURCE_ESCALATED` → `data->>'hold_id'`; `AdapterContractDrift` `INGESTION_FAILED` → feed via the controlled-message regex (mirror `tpcore/datasupervisor/supervisor._FEED_RE` exactly — reuse, don't re-author). The open + undispositioned set = the EXACT query `build_weekly_digest` runs; reuse it (DRY — call `build_weekly_digest(pool).undispositioned` is the open list; for refs, read the same query).

**Ladder SoT:** `from tpcore.ladder import policy_for` → `policy_for(<class>).disposition is Disposition.ESCALATE_OPERATOR` ⇒ "genuinely novel". Class key namespacing mirrors `tpcore/datasupervisor` (`validation:`/`cross_table:`/`contract:`/`audit_kk:`/`event:`).

**HealSpec/RemediationSpec schemas (for the provenance check):** `tpcore/selfheal/spec.HealSpec` = `check_name, source, healable, stage, params, max_attempts, unhealable_reason, depends_on`; `tpcore/auditheal/spec.RemediationSpec` = `check_key, table, check_name, remediable, stage, params, max_attempts, escalate_reason`. Registries: `HEAL_SPECS` (`tpcore/selfheal/registry.py`), `REMEDIATION_SPECS` (`tpcore/auditheal/registry.py`), drift via `registry_drift()`.

**Existing CI (`.github/workflows/ci.yml`):** one job `test` ("pytest + ruff + check_imports"), Python 3.11, steps: `check_imports`, `ruff`, `pytest`. New required checks are ADDED as steps/jobs in THIS file (not a new workflow). PR author/label in Actions context: `github.event.pull_request.user.login` and `github.event.pull_request.labels`. The LLM PRs are labeled `llm-data-triage` at creation (P3) so the provenance/denied checks key off the label, not heuristics.

**Test technique:** mirror `tpcore/tests/test_selfheal.py` / `test_datasupervisor.py` (fake-pool `_Conn/_CM/_Pool`; `registry_drift`-style clockwork; import-closure assertion via `importlib`/walking `__import__`s — see below). Tests in `tpcore/tests/` (modules) + `tests/` (ops-level), mirroring where siblings are tested.

**Sandbox worktree:** `git worktree add` into a tempdir; the runner scrubs the environment (no `DATABASE_URL`/`DATABASE_URL_IPV4`/`ANTHROPIC_API_KEY`-for-tests/Alpaca/Supabase vars passed to the test subprocess) and runs ONLY `pytest`/`ruff` read-only. Plan P2 specifies the exact env-allowlist.

One phase = one gated PR. Branch off fresh `main` per phase; controller opens/merges PRs after spec+code-quality review + CI-green (confirm a run exists before watching — the P2-dashboard slip lesson). Verify branch before every commit.

---

## Phase 1 — Deterministic safety skeleton (no LLM), dark (PR 1)

Branch: `feat/llm-triage-p1`.

### Task 1.1: `tpcore/llm_data_triage/` — trigger predicate + packet + persona

**Files:**
- Create: `tpcore/llm_data_triage/__init__.py`, `tpcore/llm_data_triage/select.py` (trigger), `tpcore/llm_data_triage/packet.py` (read-only context), `docs/llm_data_triage_persona.md`
- Test: `tpcore/tests/test_llm_data_triage_select.py`, `tpcore/tests/test_llm_data_triage_packet.py`, `tpcore/tests/test_llm_data_triage_persona.py`

- [ ] **Step 1: Read the reuse points.** `sed -n` the `-- OPEN_ESCALATIONS` query + `build_weekly_digest` return in `ops/weekly_digest.py`; `tpcore/datasupervisor/supervisor.py` `_FEED_RE` + the namespaced-key derivation; `tpcore/ladder/disposition.py` `policy_for`/`Disposition`. Confirm exact symbol names before writing.

- [ ] **Step 2: Write the failing trigger test** — `tpcore/tests/test_llm_data_triage_select.py`:

```python
"""select_novel_escalations: only open + undispositioned +
policy_for==ESCALATE_OPERATOR + no-prior-DATA_LLM_TRIAGE_PROPOSAL,
bounded oldest-first. Fake pool. No LLM."""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.llm_data_triage.select import (
    MAX_TRIAGE_PER_CYCLE,
    select_novel_escalations,
)


class _Conn:
    def __init__(self, p): self._p = p
    async def fetch(self, sql, *a):
        if "OPEN_ESCALATIONS" in sql:
            return [dict(r) for r in self._p.open_rows]
        if "DATA_LLM_TRIAGE_PROPOSAL" in sql:
            return [{"ref": r} for r in self._p.prior_refs]
        return []


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, open_rows=(), prior_refs=()):
        self.open_rows = list(open_rows)
        self.prior_refs = list(prior_refs)
    def acquire(self): return _CM(_Conn(self))


def _row(ref, cls, etype="DATA_SOURCE_ESCALATED"):
    return {"ref": ref, "etype": etype, "cls": cls,
            "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
            "message": "m"}


async def test_only_escalate_operator_class(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class",
                        lambda c: c == "event:DATA_SOURCE_ESCALATED")
    pool = _Pool(open_rows=[_row("h1", "event:DATA_SOURCE_ESCALATED"),
                            _row("h2", "selfheal:prices_daily_freshness")])
    out = await select_novel_escalations(pool)
    assert [e.ref for e in out] == ["h1"]


async def test_dedup_skips_prior_proposal(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class", lambda c: True)
    pool = _Pool(open_rows=[_row("h1", "x"), _row("h2", "x")],
                 prior_refs=["h1"])
    out = await select_novel_escalations(pool)
    assert [e.ref for e in out] == ["h2"]


async def test_bounded_oldest_first(monkeypatch) -> None:
    import tpcore.llm_data_triage.select as S
    monkeypatch.setattr(S, "_is_novel_class", lambda c: True)
    rows = [_row(f"r{i}", "x") for i in range(MAX_TRIAGE_PER_CYCLE + 3)]
    out = await select_novel_escalations(_Pool(open_rows=rows))
    assert len(out) == MAX_TRIAGE_PER_CYCLE
    assert [e.ref for e in out] == [f"r{i}" for i in range(MAX_TRIAGE_PER_CYCLE)]
```

- [ ] **Step 3: Run — FAIL** (`ModuleNotFoundError`).

- [ ] **Step 4: Create `tpcore/llm_data_triage/__init__.py`**

```python
"""LLM data triage agent (#187, Ladder rung 5).

The ONLY LLM-backed agent in the platform. Advisory: for a genuinely
NOVEL data escalation it produces a draft, human-merge-only PR (an
additive, mechanism-free HealSpec binding + dossier). It never
mutates data, never trades, never merges; fenced by deterministic CI
checks (provenance + hard-denied paths) + a post-merge canary. The
persona governs output quality only — NOT a safety boundary.
"""
```

- [ ] **Step 5: Create `tpcore/llm_data_triage/select.py`**

```python
"""Pure trigger predicate — which open escalations are genuinely
novel (deterministic; no LLM). Reuses the Ladder SoT + the
weekly-digest open set; reimplements no predicate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tpcore.ladder import Disposition, policy_for

MAX_TRIAGE_PER_CYCLE = 5

_OPEN_REFS_SQL = """-- OPEN_ESCALATIONS (triage view: ref+class)
WITH esc AS (
  SELECT e.data->>'request_id' AS ref, 'DATA_REPAIR_ESCALATED' AS etype,
         e.data->>'cls' AS cls, e.recorded_at, e.message
  FROM platform.application_log e
  WHERE e.event_type = 'DATA_REPAIR_ESCALATED'
  UNION ALL
  SELECT e.data->>'hold_id' AS ref, 'DATA_SOURCE_ESCALATED' AS etype,
         e.data->>'cls' AS cls, e.recorded_at, e.message
  FROM platform.application_log e
  WHERE e.event_type = 'DATA_SOURCE_ESCALATED'
)
SELECT ref, etype, cls, recorded_at, message FROM esc x
WHERE x.ref IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM platform.application_log t
    WHERE t.event_type IN ('DATA_REPAIR_COMPLETE','DATA_SOURCE_CLEARED')
      AND (t.data->>'request_id'=x.ref OR t.data->>'hold_id'=x.ref)
      AND t.recorded_at > x.recorded_at)
  AND NOT EXISTS (
    SELECT 1 FROM platform.application_log dp
    WHERE dp.event_type='DATA_ESCALATION_DISPOSITIONED'
      AND dp.data->>'ref'=x.ref)
ORDER BY x.recorded_at
"""

_PRIOR_SQL = """
SELECT data->>'ref' AS ref FROM platform.application_log
WHERE event_type='DATA_LLM_TRIAGE_PROPOSAL'
"""


@dataclass(frozen=True)
class NovelEscalation:
    ref: str
    etype: str
    cls: str
    recorded_at: datetime
    message: str


def _is_novel_class(cls: str | None) -> bool:
    """Genuinely novel = the Ladder has no deterministic auto-conversion
    (policy is escalate-operator). Unknown/None class ⇒ treat novel
    (an unknown escalation must not be silently skipped)."""
    if not cls:
        return True
    try:
        return policy_for(cls).disposition is Disposition.ESCALATE_OPERATOR
    except KeyError:
        return True


async def select_novel_escalations(pool: Any) -> list[NovelEscalation]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_OPEN_REFS_SQL)
        prior = {r["ref"] for r in await conn.fetch(_PRIOR_SQL)}
    out: list[NovelEscalation] = []
    for r in rows:
        if r["ref"] in prior or not _is_novel_class(r.get("cls")):
            continue
        out.append(NovelEscalation(
            r["ref"], r["etype"], r.get("cls") or "",
            r["recorded_at"], r.get("message") or ""))
        if len(out) >= MAX_TRIAGE_PER_CYCLE:
            break
    return out


__all__ = ["MAX_TRIAGE_PER_CYCLE", "NovelEscalation",
           "select_novel_escalations"]
```

(NOTE for the implementer: confirm the escalation events actually carry `data->>'cls'`. Read `tpcore/datasupervisor/supervisor.py` + `ops/data_repair_service.py` emit payloads. If they do NOT carry a `cls`, the predicate must derive the class from `(etype, source)` exactly as `ops/weekly_digest`/the digest does — **reuse that derivation, do not invent one**; adjust `_OPEN_REFS_SQL` + `_is_novel_class` accordingly and update the tests' `_row`. Report which is true.)

- [ ] **Step 6: Run the 3 select tests — pass.** Fix impl only.

- [ ] **Step 7: Packet builder test + impl** — `tpcore/llm_data_triage/packet.py`: pure `build_packet(pool, esc) -> TriagePacket` (read-only): assembles `{escalation, ladder_policy, recent_dq_rows, dossier_excerpt}`, all reads; a deterministic size cap `_MAX_PACKET_CHARS = 24000` with truncate-marker `"\n…[truncated]…"`; `packet_hash` = `hashlib.sha256(canonical_json).hexdigest()`. Test (fake pool): packet contains the escalation + policy reason; oversize dossier is truncated deterministically (same input → same hash); hash stable. (Mirror the fake-pool style; full code follows the select.py shape — assemble dicts from `conn.fetch` results, no mutation, `json.dumps(..., sort_keys=True, default=str)` for the hash.)

- [ ] **Step 8: Persona doc + lockstep test.** Create `docs/llm_data_triage_persona.md` with a `version: v1` header line and the frozen system prompt: role = advisory data-platform triage analyst for ONE escalation; output contract = a proposed *mechanism-free* binding to an **existing** canonical stage + dossier + confidence + "what I could NOT determine"; hard guardrails (no authority; defer to humans; never imply a change was made; "insufficient context" over guessing; never invent internals; **never propose a new stage/param/bound or edit an existing spec**); explicit "this persona is NOT a safety boundary — the CI fence is." Add `PERSONA_VERSION = "v1"` to `tpcore/llm_data_triage/__init__.py`. Test `test_llm_data_triage_persona.py`: asserts the doc's `version:` header == `PERSONA_VERSION` and the doc contains the no-new-mechanism + not-a-safety-boundary clauses (string asserts — lockstep, like the prior persona/version guards).

- [ ] **Step 9:** `ruff check tpcore/llm_data_triage/ tpcore/tests/test_llm_data_triage_*` clean (no noqa); `python -m pytest tpcore/tests/test_llm_data_triage_* -q` all pass; `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` collection clean.

- [ ] **Step 10: Commit**

```bash
test "$(git branch --show-current)" = "feat/llm-triage-p1" || { echo WRONG; exit 1; }
git add tpcore/llm_data_triage/ tpcore/tests/test_llm_data_triage_* docs/llm_data_triage_persona.md
git commit -m "feat(llm-data-triage): trigger predicate + read-only packet + versioned persona (dark)"
```

### Task 1.2: the deterministic CI fence — provenance + hard-denied checks + canary state

**Files:**
- Create: `tpcore/llm_data_triage/fence.py` (pure: `hard_denied_paths(diff_paths) -> list[str]`; `provenance_violations(spec_diff, main_specs) -> list[str]`), `tpcore/llm_data_triage/canary.py` (pure promotion-state read/predicate over an event), `scripts/llm_triage_pr_check.py` (thin CI entrypoint calling the pure fns against `git diff` + the registries; exit≠0 ⇒ fail)
- Test: `tpcore/tests/test_llm_data_triage_fence.py`, `tpcore/tests/test_llm_data_triage_canary.py`

- [ ] **Step 1: Failing fence test** — assert: `hard_denied_paths` flags ANY path under `tpcore/risk/`, `tpcore/order_management/`, `tpcore/risk/limits_profile.py`, `platform/migrations/`, anything matching `*/providers.py`, `scripts/run_data_operations.sh`, `scripts/ops.py`, the DSR/credibility-gate module (read its real path: `grep -rl "credibility\|DSR" tpcore/quality/validation/capital_gate.py` → `tpcore/quality/validation/capital_gate.py`); returns [] for a HealSpec-registry-only diff. `provenance_violations`: FAILS if the proposed new HealSpec references a `stage`/`params` key not present in any existing `HEAL_SPECS`/handler stage on main; FAILS on a *changed* existing spec / `healable` false→true / a new stage / a new param key / a widened `max_attempts`; PASSES for a purely additive new entry whose `(stage, params)` already appears in an existing spec.

- [ ] **Step 2–4: Implement `fence.py`** — `hard_denied_paths(paths: list[str]) -> list[str]` = `[p for p in paths if any(p == d or p.startswith(d) or fnmatch(p, g) for d,g in _DENY)]` with an explicit `_DENY` constant list (the paths above; `*/providers.py` via `fnmatch`). `provenance_violations(new_specs, baseline_specs, baseline_stages) -> list[str]`: pure set logic over the spec dicts — additive-only (no key removed/changed), every `(stage, frozenset(params))` ∈ the baseline's existing `(stage, params)` set OR `stage` ∈ `baseline_stages` with empty/known params, no `healable`/`remediable` flip, `max_attempts` ≤ baseline max. All inputs injected (pure, fully unit-testable). `scripts/llm_triage_pr_check.py` = thin: `git diff --name-only origin/main...HEAD` → `hard_denied_paths`; import `HEAL_SPECS`/`REMEDIATION_SPECS` at HEAD vs `git show origin/main:...` parsed → `provenance_violations`; print findings; `sys.exit(1)` if any. (Read how a sibling check script resolves `origin/main` in CI — mirror it.)

- [ ] **Step 5: canary.py** — pure: `is_promoted(events, spec_key) -> bool` over `LLM_SPEC_PROMOTED` application_log events (a human-emitted promotion). `shadow_decision(spec_key, promoted: bool) -> Literal["shadow","active"]`. The deterministic agents (selfheal/auditheal) will gate an LLM-authored spec to shadow until `is_promoted`. (P3 wires the read; P1 ships the pure predicate + tests only — dark.)

- [ ] **Step 6:** tests green; ruff clean; collection clean. Commit:
```bash
git add tpcore/llm_data_triage/fence.py tpcore/llm_data_triage/canary.py scripts/llm_triage_pr_check.py tpcore/tests/test_llm_data_triage_fence.py tpcore/tests/test_llm_data_triage_canary.py
git commit -m "feat(llm-data-triage): deterministic fence (provenance + hard-denied) + canary predicate (dark)"
```
STOP. Report DONE (all P1 tests count, ruff, collection, the `cls`-payload finding from 1.1 Step 5, commit SHAs) or BLOCKED.

---

## Phase 2 — the sandboxed agent + official Anthropic call (mocked in CI), dark (PR 2)

Branch: `feat/llm-triage-p2` off fresh `main`.

### Task 2.1: `ops/llm_data_triage.py` — the agent

**Files:** Create `ops/llm_data_triage.py`, `ops/__main__`-style entry; modify `pyproject.toml` (add `anthropic>=0.40`); Test `tests/test_llm_data_triage_agent.py`

- [ ] **Step 1: Read** `tpcore/outage.py` (`with_retry` exact usage — find a real call site and mirror), `pyproject.toml` deps list, `ops/data_repair_service.py` `_INSERT_SQL`/`_emit`, the sandbox-worktree precedent if any (`grep -rn "git worktree" scripts/ ops/ tpcore/`).

- [ ] **Step 2: Failing agent test** — `tests/test_llm_data_triage_agent.py` (importlib-load `ops/llm_data_triage.py` to dodge `scripts/ops.py`↔`ops/` shadowing, per the data-lane test precedent). Inject a **mock anthropic client** whose `messages.create(**kw)` returns a stub `Message` of the **official shape** (`.content=[type("B",(),{"type":"text","text":"<json proposal>"})()]`, `.stop_reason="end_turn"`, `.usage=type("U",(),{"input_tokens":10,"output_tokens":20})()`). Assert: (a) `messages.create` called with `model=_TRIAGE_MODEL`, `system=<persona>`, `messages=[{"role":"user",...}]`, **`"tools" not in kwargs`**, `temperature==0.0`; (b) a `DATA_LLM_TRIAGE_PROPOSAL` event emitted via the mirrored `_INSERT_SQL` with `ref`/`persona_version`/`packet_hash`/`proposed_disposition`; (c) **no `ANTHROPIC_API_KEY` ⇒ no client constructed, no emit, returns cleanly** (fail-safe); (d) client raising `APIError` ⇒ crash-isolated (no raise, no emit, logged). Plus the **import-isolation guard test** (see Step 5).

- [ ] **Step 3–4: Implement `ops/llm_data_triage.py`.** Structure mirrors `ops/cutover_agent.py`/`datasupervisor`: `async def run_triage(pool, *, client_factory=_default_client) -> TriageOutcome`. Flow: `select_novel_escalations(pool)` → for each: `build_packet` → if no `ANTHROPIC_API_KEY`: log `llm_data_triage.no_api_key`, return; else `client = client_factory()`; `resp = await with_retry(lambda: _call(client, persona, packet))` where `_call` does `client.messages.create(model=_TRIAGE_MODEL, max_tokens=_MAX_TOKENS, temperature=0.0, system=persona, messages=[{"role":"user","content":packet_text}])` (NO `tools`); parse `resp.content[0].text` as the proposal JSON (the persona constrains it); emit `DATA_LLM_TRIAGE_PROPOSAL` `{schema:1, ref, cls, persona_version, model, proposed_disposition, confidence, rationale, could_not_determine, packet_hash, usage}`. Whole body in `try/except Exception` → `logger.error("llm_data_triage.error", ...)`; never raises (crash-isolated). `client_factory` injected so tests pass a mock; `_default_client` builds `anthropic.Anthropic()`. Catch `AuthenticationError` → treat as no-key (fail-safe). **It does NOT open a PR or write a branch in this phase** (P3 adds the sandbox+PR; P2 lands the LLM-call + proposal-emit dark).

- [ ] **Step 5: Import-isolation clockwork guard** — in `tests/test_llm_data_triage_agent.py`: import `ops.llm_data_triage`, walk `sys.modules` it pulled (or static-scan its `ast` import nodes transitively for first-party modules) and assert NONE match `tpcore.risk`, `tpcore.order_management`, `tpcore.selfheal.orchestrator`/`registry`-write, `tpcore.auditheal`, `tpcore.datasupervisor`, `scripts.ops`. (Reading `select`/`packet`/`ladder` is allowed — read-only SoT.) Mirror the `registry_drift`-style determinism; this FAILS THE BUILD if a future import breaches the fence.

- [ ] **Step 6:** `pyproject.toml` — add `anthropic>=0.40` to the runtime deps array (read the file; match the existing formatting). `ruff check ops/llm_data_triage.py tests/test_llm_data_triage_agent.py`; full pytest collection clean; the agent module import-smoke (`python -c "import ast; ast.parse(open('ops/llm_data_triage.py').read())"`). Commit:
```bash
test "$(git branch --show-current)" = "feat/llm-triage-p2" || { echo WRONG; exit 1; }
git add ops/llm_data_triage.py tests/test_llm_data_triage_agent.py pyproject.toml
git commit -m "feat(llm-data-triage): sandboxed agent + official anthropic call (mocked, no tools, dark)"
```
STOP. Report DONE (test counts, the `tools`-never-passed assertion green, import-isolation guard green, no-key + APIError fail-safe green, ruff, collection, commit SHA) or BLOCKED (quote real `with_retry`/`_INSERT_SQL` if they differ).

---

## Phase 3 — sandbox + draft-PR + wire into existing ci.yml & data-ops & digest (PR 3)

Branch: `feat/llm-triage-p3` off fresh `main`.

### Task 3.1: ephemeral worktree + draft PR + CI checks + wiring

- [ ] **Step 1: Read** `.github/workflows/ci.yml` fully; `scripts/run_data_operations.sh` Step 4d (datasupervisor) + the sibling-agent invocation idiom + how `wrapper_*`/`_log_event` work; `ops/weekly_digest.py` undispositioned line build (the `_disposition_label`/policy-annotation site).

- [ ] **Step 2: Sandbox + PR in `ops/llm_data_triage.py`** (extend P2's agent, behind the same crash-isolation): on a produced proposal, `git worktree add <tmp> -b llm-triage/<ref-short>`; write ONLY the additive HealSpec/RemediationSpec entry + the dossier file into the worktree; run the env-scrubbed read-only gate locally (`pytest -q`, `ruff`) with an explicit env allowlist (PATH/HOME/PYTHON*, NO `*DATABASE_URL*`/`ANTHROPIC*`/`ALPACA*`/`SUPABASE*`); if green, `gh pr create --draft --label llm-data-triage --title … --body <dossier+links>`; `git worktree remove`. The bot uses a `gh` auth with NO merge permission (operator runbook P4). All still crash-isolated; failure ⇒ proposal event still emitted (advisory preserved), no PR, escalation stays for the human.

- [ ] **Step 3: Add the required CI checks to `.github/workflows/ci.yml`** — a new job `llm-triage-fence` (runs only `if: github.event.pull_request` and `contains(labels,'llm-data-triage')`): `python scripts/llm_triage_pr_check.py` (hard-denied + provenance; exit≠0 fails the PR) and an auto-close step (`gh pr close` + escalate comment) on failure. It is ADDED to the existing file (one job, not a new workflow). Non-LLM PRs skip it (label-gated) — zero impact on normal PRs. Document that "required check" + branch protection + the merge-less bot identity + CODEOWNERS(2 humans, one≠operator) are GitHub settings applied via the P4 runbook.

- [ ] **Step 4: Wire the agent into the data-ops flow** — in `scripts/run_data_operations.sh`, AFTER Step 4d (datasupervisor) and BEFORE the digest build, a new thin step mirroring the sibling idiom: `_log_event INGESTION_START wrapper_llm_triage` / `DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m ops.llm_data_triage || true` / `_log_event INGESTION_COMPLETE wrapper_llm_triage`. `|| true` — advisory, never gates the cycle.

- [ ] **Step 5: Surface in the digest** — extend the `ops/weekly_digest.py` undispositioned line: if a `DATA_LLM_TRIAGE_PROPOSAL` exists for the ref, append ` | LLM: <proposed_disposition> (conf <c>) — PR <link>`. Reuse the existing line builder; do not re-query the open set (DRY).

- [ ] **Step 6: Verify** — `bash -n scripts/run_data_operations.sh`; `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"` (valid YAML); the fence job is label-gated (non-LLM PRs unaffected — reason in the report); `python -m tpcore.… ` import smokes; full pytest collection clean; `ruff`. Commit:
```bash
test "$(git branch --show-current)" = "feat/llm-triage-p3" || { echo WRONG; exit 1; }
git add ops/llm_data_triage.py .github/workflows/ci.yml scripts/run_data_operations.sh ops/weekly_digest.py
git commit -m "feat(llm-data-triage): sandbox+draft-PR, ci.yml fence job, data-ops wiring, digest surfacing"
```
STOP. Report DONE (yaml valid, bash -n ok, label-gating confirmed, the exact wiring lines, ruff/collection, commit SHA) or BLOCKED.

---

## Phase 4 — docs + operator runbook (PR 4)

Branch: `docs/llm-triage-p4` off fresh `main`.

- [ ] **Step 1:** `CLAUDE.md` — one bullet: the LLM data triage agent (rung 5), its bright lines (advisory; data restoration NEVER via the LLM; starved sandbox; draft-PR-only; provenance/hard-denied CI fence; post-merge canary), data-lane only, pointer to the spec + persona doc.
- [ ] **Step 2:** `docs/ESCALATION_HARDENING_LADDER.md` — rung 5 status → BUILT; record the expert-vetted envelope + the explicit vetoes; note the engine session will build a symmetric engine-native triage agent (symmetry-not-copy).
- [ ] **Step 3:** Create `docs/llm_data_triage_operator_runbook.md` — the **config-not-code** GitHub settings the operator must apply: branch protection requiring the `llm-triage-fence` + `test` checks; CODEOWNERS so an LLM PR needs 2 human approvals, one ≠ the dispositioning operator; a dedicated bot GitHub identity/token with NO merge permission used by `gh` in the agent; the `llm-data-triage` label; `ANTHROPIC_API_KEY` provisioning (and that its absence = safe no-op). Honestly flagged: these are repo/GitHub settings, not enforceable purely in code; the code-side fence is independently sufficient to block a system-breaking *merge* without a human.
- [ ] **Step 4:** spec `**Status:**` → `BUILT 2026-05-18` + Build record P1=#<p1>/P2=#<p2>/P3=#<p3>/P4=this.
- [ ] **Step 5:** `git diff --stat` = exactly the doc files; collection clean; commit:
```bash
test "$(git branch --show-current)" = "docs/llm-triage-p4" || { echo WRONG; exit 1; }
git add CLAUDE.md docs/ESCALATION_HARDENING_LADDER.md docs/llm_data_triage_operator_runbook.md docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md
git commit -m "docs(llm-data-triage): CLAUDE.md + Ladder rung5 BUILT + operator runbook + spec BUILT"
```
STOP. Report DONE.

---

## Self-Review

**1. Spec coverage:** §1 (additive mechanism-free PR + dossier) → P1.1 packet/persona + P3.2 PR; §2 (data stays 100%, LLM never repairs) → agent has no creds/no repair call, P3 sandbox env-scrubbed, never runs a stage; §3 hard-denied + provenance + canary + two-human + inert-until-merged → P1.2 fence/canary + P3.3 ci.yml required job + P4 runbook; §4 vetoes → provenance rejects new/widened mechanism, no merge cred (P4), no real-tree write (worktree+scrub), persona-not-safety (persona doc states it, fence is the boundary); §5 persona created+versioned+lockstep → P1.1 Step 8; §6 official-doc Anthropic → ground-truth pinned from context7, P2 mocked-to-real-shape; §7 trigger reuses Ladder SoT → P1.1 select.py; §8 one canonical mechanism → single agent + one ci.yml job, no new pipeline/toggle; §9 data-lane only / runbook honest re config-not-code → P4; §10 fence-first phasing → P1 before P2 before wire. ✓ §11 items all assigned a read-step.

**2. Placeholder scan:** packet.py Step 7 + fence.py Step 2–4 describe full pure logic + tests but compress the body to the established fake-pool/set-logic pattern rather than re-print 200 lines — every signature, the `_DENY` list contents, the provenance rules, the hash method, and the test assertions are concrete; this is "follow the verified mirrored pattern", not "TBD". The P1.1-Step-5 `cls` payload uncertainty is an explicit read-and-report, not a hidden gap. PR-number placeholders in P4 are controller-supplied.

**3. Type consistency:** `select_novel_escalations(pool)->list[NovelEscalation(ref,etype,cls,recorded_at,message)]`; `MAX_TRIAGE_PER_CYCLE`; `PERSONA_VERSION`; `hard_denied_paths(list)->list`; `provenance_violations(...)->list[str]`; `DATA_LLM_TRIAGE_PROPOSAL` event keys consistent P1→P3; `_TRIAGE_MODEL`/`_MAX_TOKENS` constants; `client_factory` injection name consistent agent↔test. Event/ref keys (`request_id`/`hold_id`) match the verified weekly-digest query.

(Carried to execution: the spec-compliance reviewer MUST verify, per phase — (a) `tools` is never passed to `messages.create` and the mock matches the official `Message` shape; (b) the import-isolation guard genuinely fails on a fenced import; (c) the provenance check genuinely rejects a new/widened mechanism and a non-additive spec edit; (d) the sandbox env-allowlist excludes every `*DATABASE_URL*`/`ANTHROPIC*`/broker var; (e) the ci.yml fence job is label-gated so normal PRs are unaffected. These are the load-bearing safety properties — a fake-pool green is necessary but not sufficient; review against the real schemas/docs.)
