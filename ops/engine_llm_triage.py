"""Engine-lane LLM triage agent (Epic E / Engine Ladder R5) — Phase 2.

Symmetric mirror of the shipped data-lane #187 — symmetry-of-approach,
NOT a clone (`feedback_symmetry_not_copy`). Advisory + human-gated ONLY:
for a genuinely NOVEL, undispositioned engine-lane escalation it calls
the official Anthropic SDK (mocked in CI; never live in automated
pipelines) to produce a non-authoritative ENGINE_LLM_TRIAGE_PROPOSAL +
a draft, human-merge-only PR (an additive, mechanism-free
DISPOSITION_POLICIES binding to an EXISTING EngineEscalationDisposition
verb + dossier). It never mutates the engine, never trades, never
disposes, never merges, never live-calls in CI.

Lands DARK in Phase 2: no daemon / CI caller imports this module yet
(wired event-driven in Phase 3).

Reuse posture (spec §3 FORK-A / §6):
  * the official Anthropic-SDK-call / no-key / AuthenticationError /
    malformed-per-escalation envelope is the SHIPPED `ops.llm_data_triage`
    wrapper, reused VERBATIM (resolved via the lazy `_shipped()`
    accessor — NOT re-authored here): the SAME `_AuthSkip` class
    object, the SAME `_MODEL` / `_MAX_TOKENS` pins (Task 2.1: never
    re-pin an engine-local constant).
  * the credential-starved sandbox + draft-PR machinery reuses the
    SHIPPED `_scrubbed_env` / `_default_pr_runner` VERBATIM (same
    objects, lazily resolved). The `_open_draft_pr` STRUCTURE mirrors
    the shipped one; the only engine-flavoured delta is the additive
    binding stub / dossier text + the `engine-llm-triage` label.
  * the application_log INSERT is the ENGINE-lane convention: this
    module's `_INSERT_SQL` byte-mirrors `ops.engine_ladder._INSERT_SQL`
    (not the data-lane one).

Import isolation: this module statically imports ONLY the Phase-1
engine package (`tpcore.engine_llm_triage`) + tpcore/stdlib/anthropic;
it reaches the shipped `ops.llm_data_triage` wrapper through the lazy
`_shipped()` accessor (the Phase-1 `_engine_ladder()` precedent — a
CALL-time import so module-load never binds `sys.modules['ops']` under
the documented `scripts/ops.py`↔`ops/` test shadow). It NEVER imports
an actor/mutation path (`tpcore.risk`, `tpcore.order_management`,
`ops.engine_supervisor`, `ops.aar_autotune`, `ops.engine_ladder`
mechanism, `tpcore.supervisor_state`, `scripts.ops`) — asserted by the
AST clockwork guard in `tests/test_engine_llm_triage_agent.py`. (The
shipped `ops.llm_data_triage` advisory wrapper is NOT in the forbidden
actor set: it is itself a no-mutation advisory module.)

Safety boundary: the deterministic CI fence (provenance + hard-denied
paths + post-merge canary), NOT this module. The persona governs output
quality only.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from anthropic import APIError, AuthenticationError, RateLimitError

from tpcore.engine_llm_triage import PERSONA_VERSION
from tpcore.engine_llm_triage.packet import EngineTriagePacket, build_packet
from tpcore.engine_llm_triage.select import select_novel_escalations
from tpcore.outage import with_retry


def _shipped():
    """Lazy accessor for the SHIPPED #187 `ops.llm_data_triage` wrapper.

    Imported at CALL time (never at module-load / pytest collection
    time) — exactly the Phase-1 `select._engine_ladder()` /
    `packet._engine_ladder()` precedent — so importing THIS module never
    binds `sys.modules['ops']` at load. Without this the documented
    `scripts/ops.py`↔`ops/` shadow (installed by
    `scripts/tests/test_ops.py` putting `scripts/` on `sys.path`) makes
    a top-level `from ops.llm_data_triage import …` raise
    `'ops' is not a package` during full-suite collection.

    This is the documented, intentional FORK-A reuse: ONE
    SDK-call / sandbox object, zero clone, no re-authored envelope.
    `ops.llm_data_triage` is the shipped #187 advisory module — it is
    NOT an actor/mutation path (the AST import-isolation guard's
    forbidden set is the trading/risk/ladder-mechanism actors, which
    this is not).
    """
    from ops import llm_data_triage

    return llm_data_triage


def _default_pr_runner(
    argv: list[str], *, env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """The default `pr_runner` — delegates VERBATIM to the SHIPPED
    `ops.llm_data_triage.default_pr_runner` (the published #244 public
    shared-SDK surface — an identity-preserving alias of the shipped
    private object; resolved lazily so the kwarg default binds at
    def-time without binding `sys.modules['ops']` under the test
    shadow). Tests inject a fake instead; this is never exercised in
    CI."""
    return _shipped().default_pr_runner(argv, env=env, cwd=cwd)

logger = structlog.get_logger(__name__)

# Cached at import time — static for the process lifetime.
_PERSONA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs" / "engine_llm_triage_persona.md"
)
_PERSONA_TEXT: str = _PERSONA_PATH.read_text(encoding="utf-8")

# Engine tag emitted to application_log — matches the engine-lane
# convention (engine_ladder/_emit stamps the human-legible engine name;
# this advisory agent uses a fixed lane tag like data_repair_service).
_AGENT_ENGINE_TAG = "engine_llm_triage"


def _persona() -> str:
    return _PERSONA_TEXT


# Byte-mirror ops.engine_ladder._INSERT_SQL (same table, same column
# order, same ::jsonb cast — the ENGINE-lane insert convention; do NOT
# re-author the SQL, do NOT borrow the data-lane variant). A test
# asserts string equality with engine_ladder._INSERT_SQL so any drift
# fails the build.
_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """One non-authoritative advisory event via the locked engine-lane
    INSERT (column-order parity with engine_ladder._emit). ``data`` is
    json.dumps'd and cast to jsonb DB-side."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            _AGENT_ENGINE_TAG,
            uuid.uuid4(),
            event_type,
            severity,
            message,
            json.dumps(data, default=str),
        )


@dataclass
class EngineTriageOutcome:
    proposed: list[str] = field(default_factory=list)
    skipped_no_key: bool = False
    error: str | None = None


def _default_client() -> Any:
    """The official ASYNC SDK client.

    This module's ``run_triage`` is invoked inside the long-lived
    ``ops/llm_triage_service.py`` asyncio daemon as a second
    crash-isolated co-task. A *sync* ``anthropic.Anthropic`` whose
    ``messages.create`` is ``await``ed blocks the entire event loop for
    the full LLM round-trip (seconds), starving the crash-isolated
    co-tasks + the poll loop in the same process.
    ``AsyncAnthropic`` makes the round-trip a true awaitable that yields
    to the loop. ``tpcore.outage.with_retry`` already ``await``s the
    wrapped fn, so retry/backoff semantics are unchanged. Symmetric
    mirror of the data-lane twin fix (PR #97, #244 shared-SDK-surface
    contract)."""
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _engine_binding_stub(hold_id: str, failure_class: str) -> str:
    """The ADDITIVE, mechanism-free content the agent may produce — a
    commented DISPOSITION_POLICIES binding proposal, NOT an edit to any
    existing policy/mechanism (the deterministic CI fence enforces that).
    Inert text in the draft PR until a human reviews + merges it."""
    return (
        "# engine-llm-triage proposal — ADDITIVE DISPOSITION_POLICIES\n"
        "# binding ONLY (inert until a human reviews + merges this draft\n"
        "# PR). The deterministic CI fence rejects any non-additive /\n"
        "# new-mechanism / new-disposition-member change.\n"
        f"# hold_id: {hold_id}\n"
        f"# failure_class: {failure_class}\n"
    )


async def _open_draft_pr(
    pool: Any,
    esc: Any,
    pkt: Any,
    prop: dict[str, Any],
    runner: Callable[..., tuple[int, str, str]],
) -> str | None:
    """Sandbox + draft-PR for ONE produced engine proposal.

    This MIRRORS the shipped `ops.llm_data_triage._open_draft_pr`
    structure verbatim (credential-starved fresh-dict env via the
    SHIPPED `_scrubbed_env`, read-only local gate, draft + human-merge-
    only PR, worktree/branch/tmpdir always cleaned, #63-hardened
    behaviour) with the only deltas being engine-flavoured additive
    content + the `engine-llm-triage` label.

    Crash-isolated: ANY failure ⇒ returns None (the advisory proposal
    was already / will still be emitted; no PR, no leaked worktree, the
    escalation stays for the R3 human), NEVER raises. NEVER `gh pr
    merge` — draft + human-merge-only by construction.
    """
    ref_short = re.sub(r"[^A-Za-z0-9._-]", "-", str(esc.hold_id))[:24]
    branch = f"llm-triage/{ref_short}"
    tmpdir = tempfile.mkdtemp(prefix="engine_llm_triage_wt_")
    worktree_added = False
    try:
        rc, _o, err = runner(
            ["git", "worktree", "add", tmpdir, "-b", branch],
            cwd=str(_REPO_ROOT),
        )
        if rc != 0:
            logger.error("engine_llm_triage.worktree_add_failed",
                         hold_id=esc.hold_id, error=err)
            return None
        worktree_added = True

        proposals_dir = (
            pathlib.Path(tmpdir) / "docs" / "engine_llm_triage_proposals"
        )
        proposals_dir.mkdir(parents=True, exist_ok=True)
        binding_path = proposals_dir / f"{ref_short}.binding.txt"
        dossier_path = proposals_dir / f"{ref_short}.dossier.md"
        binding_path.write_text(
            _engine_binding_stub(esc.hold_id, esc.failure_class),
            encoding="utf-8",
        )
        dossier = (
            f"# Engine LLM-Triage Dossier — {esc.hold_id}\n\n"
            f"- engine: `{esc.engine}`\n"
            f"- failure_class: `{esc.failure_class}`\n"
            f"- proposed_disposition: "
            f"`{prop.get('proposed_disposition')}`\n"
            f"- confidence: `{prop.get('confidence')}`\n"
            f"- packet_hash: `{pkt.packet_hash}`\n"
            f"- persona_version: `{PERSONA_VERSION}`\n\n"
            f"## Rationale\n\n{prop.get('rationale')}\n\n"
            f"## Could not determine\n\n"
            f"{prop.get('could_not_determine')}\n\n"
            f"> ADVISORY ONLY. Draft + human-merge-only. The LLM never "
            f"mutates the engine, trades, disposes, holds, runs a "
            f"self-heal, or merges. Resolution only ever happens via "
            f"the existing deterministic Ladder path (R3 human / "
            f"R1–R4). Inert until a human merges this PR.\n"
        )
        dossier_path.write_text(dossier, encoding="utf-8")

        # Read-only local gate INSIDE the worktree with a fresh,
        # credential-STARVED allowlist env (the SHIPPED scrubbed_env —
        # the published #244 public shared-SDK surface, an identity-
        # preserving alias of the shipped private; never
        # os.environ.copy()).
        gate_env = _shipped().scrubbed_env()
        for gate_cmd in (
            [sys.executable, "-m", "pytest", "-q"],
            ["ruff", "check", "."],
        ):
            grc, _go, _ge = runner(gate_cmd, env=gate_env, cwd=tmpdir)
            if grc != 0:
                logger.warning(
                    "engine_llm_triage.local_gate_red",
                    hold_id=esc.hold_id, cmd=gate_cmd[0],
                )
                return None  # gate red ⇒ NO PR (worktree still removed)

        title = f"[engine-llm-triage] proposal for {esc.hold_id}"
        body = (
            f"{dossier}\n\n"
            f"packet_hash: `{pkt.packet_hash}`\n"
            f"hold_id: `{esc.hold_id}`\n"
        )
        prc, pout, perr = runner(
            ["gh", "pr", "create", "--draft",
             "--label", "engine-llm-triage",
             "--title", title, "--body", body],
            cwd=tmpdir,
        )
        if prc != 0:
            logger.error("engine_llm_triage.pr_create_failed",
                         hold_id=esc.hold_id, error=perr)
            return None
        logger.info("engine_llm_triage.draft_pr_opened",
                    hold_id=esc.hold_id, pr=pout.strip())
        return pout.strip() or branch
    except Exception as exc:  # noqa: BLE001 — never raise; advisory preserved
        logger.error("engine_llm_triage.pr_isolated_failure",
                     hold_id=esc.hold_id, error=str(exc))
        return None
    finally:
        # ALWAYS remove the worktree — even on gate failure / exception.
        if worktree_added:
            try:
                runner(["git", "worktree", "remove", "--force", tmpdir],
                        cwd=str(_REPO_ROOT))
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error("engine_llm_triage.worktree_remove_failed",
                             hold_id=esc.hold_id, error=str(exc))
            # `git worktree remove` does NOT delete the branch. Without
            # this, the local `llm-triage/<ref>` branch persists after
            # ANY outcome and a later run for the SAME hold_id hits
            # `git worktree add -b <same branch>` → rc≠0 → that
            # escalation never gets a PR again (a wedged retry).
            try:
                runner(["git", "branch", "-D", branch],
                        cwd=str(_REPO_ROOT))
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error("engine_llm_triage.branch_delete_failed",
                             hold_id=esc.hold_id, error=str(exc))
        # mkdtemp() created tmpdir BEFORE `git worktree add`; if the add
        # failed (worktree_added=False) the conditional remove above is
        # skipped and the dir would leak. ignore_errors → never raises.
        shutil.rmtree(tmpdir, ignore_errors=True)


async def run_triage(
    pool: Any,
    *,
    client_factory: Callable[[], Any] = _default_client,
    pr_runner: Callable[..., tuple[int, str, str]] = _default_pr_runner,
) -> EngineTriageOutcome:
    """Run one advisory engine-lane triage pass. Never raises — all
    failures are captured in ``EngineTriageOutcome.error``. Never emits
    on a fatal failure; a malformed single response is per-escalation
    isolated (skip that one, batch continues, no ``error``).

    The Anthropic-SDK-call / no-key / AuthenticationError-bypasses-retry
    / @with_retry envelope below is the SHIPPED `ops.llm_data_triage`
    pattern reused verbatim (same `_AuthSkip` sentinel object, same
    `_MODEL`/`_MAX_TOKENS`, same retry_on tuple, same temperature 0.0 /
    no `tools` / persona-as-system call) — only the persona text and
    the emit/payload/PR-flavour are engine-native.
    """
    out = EngineTriageOutcome()
    try:  # noqa: BLE001 — never abort the sweep
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("engine_llm_triage.no_api_key")
            out.skipped_no_key = True
            return out

        escs = await select_novel_escalations(pool)
        if not escs:
            logger.info("engine_llm_triage.no_novel_escalations")
            return out

        # Resolve the SHIPPED #187 wrapper symbols VERBATIM via the
        # published #244 public shared-SDK surface (lazy — only once we
        # are actually triaging, so module-load never binds
        # sys.modules['ops']). `AuthSkip` is an identity-preserving alias
        # bound to the SAME class object as `ops.llm_data_triage._AuthSkip`
        # (the shipped sentinel — NOT a redefinition), so the
        # `except _AuthSkip` below matches the exact shipped class;
        # `ANTHROPIC_MODEL`/`ANTHROPIC_MAX_TOKENS` are the shipped pins
        # (Task 2.1: never re-pin an engine-local constant). The engine
        # lane consumes ONLY this public surface — never the underscore
        # privates (enforced by a clockwork guard test).
        _shipped_mod = _shipped()
        _AuthSkip = _shipped_mod.AuthSkip
        _model = _shipped_mod.ANTHROPIC_MODEL
        _max_tokens = _shipped_mod.ANTHROPIC_MAX_TOKENS

        client = client_factory()

        @with_retry(
            max_attempts=3,
            backoff_base_sec=2.0,
            backoff_cap_sec=30.0,
            retry_on=(RateLimitError, APIError),
        )
        async def _call_api(pkt_arg: EngineTriagePacket) -> Any:
            """Thin async wrapper so @with_retry (async decorator)
            applies. AuthenticationError is intercepted and re-raised as
            the SHIPPED `_AuthSkip` so it escapes the retry_on tuple
            entirely (zero retries, zero backoff) — identical semantics
            to a missing key. `_AuthSkip` is the SAME class object as
            `ops.llm_data_triage._AuthSkip` (resolved lazily above, NOT
            redefined)."""
            try:
                return await client.messages.create(
                    model=_model,
                    max_tokens=_max_tokens,
                    temperature=0.0,
                    system=_persona(),
                    messages=[{"role": "user", "content": pkt_arg.text}],
                )
            except AuthenticationError:
                raise _AuthSkip() from None

        try:
            for esc in escs:
                pkt = await build_packet(pool, esc)

                try:
                    resp = await _call_api(pkt)
                except _AuthSkip:
                    logger.warning("engine_llm_triage.auth_error_skipped",
                                   hold_id=esc.hold_id)
                    out.skipped_no_key = True
                    return out
                except Exception as call_exc:  # noqa: BLE001 — isolate
                    logger.error(
                        "engine_llm_triage.call_error",
                        hold_id=esc.hold_id,
                        error=str(call_exc),
                    )
                    raise  # propagate to outer try/except → sets out.error

                try:
                    txt = resp.content[0].text
                    try:
                        prop = json.loads(txt)
                    except json.JSONDecodeError as json_exc:
                        logger.warning(
                            "engine_llm_triage.malformed_response",
                            hold_id=esc.hold_id,
                            error=str(json_exc),
                            response_preview=txt[:200],
                        )
                        continue  # skip this escalation, don't abort the loop

                    if not isinstance(prop, dict):
                        logger.warning(
                            "engine_llm_triage.non_dict_response",
                            hold_id=esc.hold_id,
                            response_preview=txt[:200],
                        )
                        continue  # skip this escalation, don't abort the loop

                    # Sandbox + draft, human-merge-only PR FIRST so the
                    # proposal event can carry pr_link. Fully
                    # crash-isolated: ANY failure ⇒ pr_link is None and
                    # the advisory proposal below is STILL emitted
                    # (invariant: advisory preserved), no leaked
                    # worktree, escalation stays for the R3 human, NO
                    # raise.
                    pr_link = await _open_draft_pr(
                        pool, esc, pkt, prop, pr_runner
                    )

                    await _emit(
                        pool,
                        "ENGINE_LLM_TRIAGE_PROPOSAL",
                        f"engine LLM triage proposal for hold_id={esc.hold_id}",
                        {
                            "schema": 1,
                            "hold_id": esc.hold_id,
                            "failure_class": esc.failure_class,
                            "engine": esc.engine,
                            "persona_version": PERSONA_VERSION,
                            "packet_hash": pkt.packet_hash,
                            "proposed_disposition": prop.get(
                                "proposed_disposition"),
                            "confidence": prop.get("confidence"),
                            "could_not_determine": prop.get(
                                "could_not_determine"),
                            "rationale": prop.get("rationale"),
                            "model": _model,
                            "pr_link": pr_link,
                            "usage": {
                                "in": resp.usage.input_tokens,
                                "out": resp.usage.output_tokens,
                            },
                        },
                    )
                    out.proposed.append(esc.hold_id)
                    logger.info(
                        "engine_llm_triage.proposal_emitted",
                        hold_id=esc.hold_id,
                        model=_model,
                        persona_version=PERSONA_VERSION,
                        pr_link=pr_link,
                    )
                except (IndexError, AttributeError, KeyError,
                        TypeError) as exc:
                    logger.warning(
                        "engine_llm_triage.malformed_response",
                        hold_id=esc.hold_id,
                        error=str(exc),
                    )
                    continue  # skip this escalation, don't abort the loop
        finally:
            # Release the AsyncAnthropic httpx connection pool on EVERY
            # exit path (normal completion, the _AuthSkip early `return
            # out`, and the propagate-to-outer-except `raise`). In the
            # long-lived ops/llm_triage_service.py daemon each event-
            # driven run_triage would otherwise leak a pool until GC.
            # Matches the codebase-wide aclose() discipline + the
            # data-lane twin (#97). Defensive getattr so injected test
            # fakes lacking aclose don't break.
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    except Exception as exc:  # noqa: BLE001 — never raises; crash-isolated
        logger.error("engine_llm_triage.error", error=str(exc))
        out.error = str(exc)

    return out


__all__ = ["EngineTriageOutcome", "run_triage"]


def main() -> None:  # pragma: no cover
    from tpcore.db import build_asyncpg_pool

    async def _amain() -> None:
        dsn = os.environ.get("DATABASE_URL") or os.environ.get(
            "DATABASE_URL_IPV4")
        if not dsn:
            logger.error("engine_llm_triage.no_dsn")
            sys.exit(1)
        pool = await build_asyncpg_pool(dsn)
        try:
            result = await run_triage(pool)
        finally:
            await pool.close()
        print(
            f"engine_llm_triage: proposed={result.proposed} "
            f"skipped_no_key={result.skipped_no_key} "
            f"error={result.error}"
        )

    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()
