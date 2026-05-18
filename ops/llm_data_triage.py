"""LLM data triage agent (LT-P2) — Advisory only, NEVER acts.

Calls the official Anthropic SDK to produce a non-authoritative
DATA_LLM_TRIAGE_PROPOSAL for each genuinely novel data escalation.
Mocked in CI; no live API calls in automated pipelines. Lands dark —
not wired into any cycle until P3 (the human-review / PR step).

Safety boundary: the deterministic CI fence (provenance + hard-denied
paths + post-merge canary), NOT this module. This module only governs
output quality via the advisory persona.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from anthropic import (
    Anthropic,
    APIError,
    AuthenticationError,
    RateLimitError,
)

from tpcore.llm_data_triage import PERSONA_VERSION as _PERSONA_VERSION
from tpcore.llm_data_triage.packet import TriagePacket, build_packet
from tpcore.llm_data_triage.select import select_novel_escalations
from tpcore.outage import with_retry

logger = structlog.get_logger(__name__)

# Private sentinel: raised inside _call_api when the SDK raises
# AuthenticationError so the exception escapes with_retry's retry_on
# tuple WITHOUT triggering any retry loop. with_retry gates retries via
# ``except retry_on as exc`` (isinstance check, retry.py:130); _AuthSkip
# is NOT in retry_on, so it propagates immediately.
class _AuthSkip(Exception):
    """Signals that the Anthropic API key is invalid/exhausted (AuthenticationError).
    Treated identically to a missing key: safe no-op, zero retries, zero emits."""

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048
_PERSONA_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "docs" / "llm_data_triage_persona.md"
)

# Engine tag emitted to application_log — matches data_repair_service convention.
_AGENT_ENGINE_TAG = "llm_data_triage"

# Cached at import time — file is static for the lifetime of the process.
_PERSONA_TEXT: str = _PERSONA_PATH.read_text(encoding="utf-8")


def _persona() -> str:
    return _PERSONA_TEXT


# Mirror data_repair_service._INSERT_SQL exactly (same table, same column order,
# same ::jsonb cast convention from tpcore/logging/db_handler.py).
_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """Insert one advisory/operational event. ``data`` is json.dumps'd
    to a string and cast to jsonb DB-side (db_handler convention)."""
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
class TriageOutcome:
    proposed: list[str] = field(default_factory=list)
    skipped_no_key: bool = False
    error: str | None = None


def _default_client() -> Anthropic:
    return Anthropic()


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The ONLY env vars the sandboxed read-only gate may see. The child env
# is built as a FRESH dict containing solely these (NEVER
# os.environ.copy()) — the sandbox is credential-STARVED by
# construction. Explicitly NOT *DATABASE_URL*, ANTHROPIC*, ALPACA*,
# SUPABASE*, *TOKEN*, *KEY*: those are never copied because the dict is
# allowlist-built, not blocklist-filtered.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG")
_ENV_ALLOWLIST_PREFIXES = ("PYTHON",)


def _scrubbed_env() -> dict[str, str]:
    """A fresh dict of ONLY the allowlisted vars from os.environ.

    Built additively from an allowlist — a forbidden var (a *KEY*, a
    *TOKEN*, any *DATABASE_URL*, ANTHROPIC*/ALPACA*/SUPABASE*) is never
    even read into the result, so it CANNOT leak. This is the
    credential-starve guarantee for the local sandbox gate.
    """
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _ENV_ALLOWLIST or any(
            k.upper().startswith(p) for p in _ENV_ALLOWLIST_PREFIXES
        ):
            env[k] = v
    return env


def _default_pr_runner(
    argv: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None
) -> tuple[int, str, str]:
    """Run one git/gh/gate command. Returns (returncode, stdout, stderr).

    The seam tests inject a fake for — no real subprocess, no real
    network, no real worktree. ``env`` is passed verbatim to the child
    (the scrubbed allowlist dict for the gate; None ⇒ inherit for plain
    git/gh metadata calls that need no secrets)."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _heal_spec_binding_stub(esc_ref: str, cls: str) -> str:
    """The ADDITIVE, mechanism-free content the agent is allowed to
    produce — a commented binding proposal, NOT an edit to any existing
    spec/mechanism (the deterministic CI fence enforces that). It is
    inert text in the draft PR until a human reviews + merges it."""
    return (
        "# LLM-data-triage proposal — ADDITIVE binding ONLY (inert until\n"
        "# a human reviews + merges this draft PR). The deterministic CI\n"
        "# fence (scripts/llm_triage_pr_check.py) rejects any\n"
        "# non-additive / new-mechanism change.\n"
        f"# escalation_ref: {esc_ref}\n"
        f"# ladder_class: {cls}\n"
    )


async def _open_draft_pr(
    pool: Any,
    esc: Any,
    pkt: Any,
    prop: dict[str, Any],
    runner: Callable[..., tuple[int, str, str]],
) -> str | None:
    """Sandbox + draft-PR for ONE produced proposal. Crash-isolated:
    ANY failure ⇒ returns None (the advisory proposal was already
    emitted; no PR, no leaked worktree, escalation stays for the human),
    NEVER raises. The produced PR is draft + human-merge-only + inert.
    """
    # Defense-in-depth: sanitize anything that isn't a safe
    # branch/path char before it ever becomes a branch or filename
    # (we already use list-args / no-shell, so this is belt-and-
    # suspenders, not the primary control).
    ref_short = re.sub(r"[^A-Za-z0-9._-]", "-", str(esc.ref))[:24]
    branch = f"llm-triage/{ref_short}"
    tmpdir = tempfile.mkdtemp(prefix="llm_triage_wt_")
    worktree_added = False
    try:
        rc, _o, err = runner(
            ["git", "worktree", "add", tmpdir, "-b", branch],
            cwd=str(_REPO_ROOT),
        )
        if rc != 0:
            logger.error("llm_data_triage.worktree_add_failed",
                         ref=esc.ref, error=err)
            return None
        worktree_added = True

        # Write ONLY additive content: (a) the binding stub, (b) the
        # dossier. NO edit to any existing spec/mechanism — the fence
        # enforces that; we only ever PRODUCE additive content.
        proposals_dir = pathlib.Path(tmpdir) / "docs" / "llm_triage_proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        binding_path = proposals_dir / f"{ref_short}.binding.txt"
        dossier_path = proposals_dir / f"{ref_short}.dossier.md"
        binding_path.write_text(
            _heal_spec_binding_stub(esc.ref, esc.cls), encoding="utf-8"
        )
        dossier = (
            f"# LLM Data-Triage Dossier — {esc.ref}\n\n"
            f"- ladder_class: `{esc.cls}`\n"
            f"- proposed_disposition: "
            f"`{prop.get('proposed_disposition')}`\n"
            f"- confidence: `{prop.get('confidence')}`\n"
            f"- packet_hash: `{pkt.packet_hash}`\n"
            f"- persona_version: `{_PERSONA_VERSION}`\n\n"
            f"## Rationale\n\n{prop.get('rationale')}\n\n"
            f"## Could not determine\n\n"
            f"{prop.get('could_not_determine')}\n\n"
            f"> ADVISORY ONLY. Draft + human-merge-only. The LLM never "
            f"repairs data, runs a stage, mutates a table, trades, or "
            f"merges. Restoration only ever happens via the existing "
            f"deterministic path. Inert until a human merges this PR.\n"
        )
        dossier_path.write_text(dossier, encoding="utf-8")

        # Read-only local gate INSIDE the worktree with a fresh,
        # credential-STARVED allowlist env (never os.environ.copy()).
        gate_env = _scrubbed_env()
        for gate_cmd in (
            [sys.executable, "-m", "pytest", "-q"],
            ["ruff", "check", "."],
        ):
            grc, _go, _ge = runner(gate_cmd, env=gate_env, cwd=tmpdir)
            if grc != 0:
                logger.warning(
                    "llm_data_triage.local_gate_red",
                    ref=esc.ref, cmd=gate_cmd[0],
                )
                return None  # gate red ⇒ NO PR (worktree still removed)

        title = f"[llm-data-triage] proposal for {esc.ref}"
        body = (
            f"{dossier}\n\n"
            f"packet_hash: `{pkt.packet_hash}`\n"
            f"escalation_ref: `{esc.ref}`\n"
        )
        prc, pout, perr = runner(
            ["gh", "pr", "create", "--draft",
             "--label", "llm-data-triage",
             "--title", title, "--body", body],
            cwd=tmpdir,
        )
        if prc != 0:
            logger.error("llm_data_triage.pr_create_failed",
                         ref=esc.ref, error=perr)
            return None
        logger.info("llm_data_triage.draft_pr_opened",
                    ref=esc.ref, pr=pout.strip())
        return pout.strip() or branch
    except Exception as exc:  # noqa: BLE001 — never raise; advisory preserved
        logger.error("llm_data_triage.pr_isolated_failure",
                     ref=esc.ref, error=str(exc))
        return None
    finally:
        # ALWAYS remove the worktree — even on gate failure / exception.
        if worktree_added:
            try:
                runner(["git", "worktree", "remove", "--force", tmpdir],
                        cwd=str(_REPO_ROOT))
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error("llm_data_triage.worktree_remove_failed",
                             ref=esc.ref, error=str(exc))
            # `git worktree remove` does NOT delete the branch. Without
            # this, the local `llm-triage/<ref_short>` branch persists
            # after ANY outcome (gate-red / gh-fail / success) and a
            # later run for the SAME esc.ref hits `git worktree add -b
            # <same branch>` → rc≠0 → that ref NEVER gets a PR again
            # (a wedged retry). Best-effort delete; never raises out.
            try:
                runner(["git", "branch", "-D", branch],
                        cwd=str(_REPO_ROOT))
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error("llm_data_triage.branch_delete_failed",
                             ref=esc.ref, error=str(exc))
        # Unconditionally drop the temp dir: mkdtemp() created it BEFORE
        # `git worktree add`, so if the add failed (worktree_added=False)
        # the conditional `git worktree remove` above is skipped and the
        # dir would otherwise leak. ignore_errors → never raises out.
        shutil.rmtree(tmpdir, ignore_errors=True)


async def run_triage(
    pool: Any,
    *,
    client_factory: Callable[[], Any] = _default_client,
    pr_runner: Callable[..., tuple[int, str, str]] = _default_pr_runner,
) -> TriageOutcome:
    """Run one advisory triage pass. Never raises — all failures are
    captured in ``TriageOutcome.error``. Never emits on failure."""
    out = TriageOutcome()
    try:  # noqa: BLE001 — never abort the sweep
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("llm_data_triage.no_api_key")
            out.skipped_no_key = True
            return out

        escs = await select_novel_escalations(pool)
        if not escs:
            logger.info("llm_data_triage.no_novel_escalations")
            return out

        client = client_factory()

        @with_retry(
            max_attempts=3,
            backoff_base_sec=2.0,
            backoff_cap_sec=30.0,
            retry_on=(RateLimitError, APIError),
        )
        async def _call_api(pkt_arg: TriagePacket) -> Any:
            """Thin async wrapper so @with_retry (async decorator) applies.

            The synchronous SDK call is wrapped here — mirrors the
            @with_retry pattern on edgar_adapter / fred adapter call sites.
            pkt_arg is passed explicitly to avoid B023 loop-variable capture.

            AuthenticationError is intercepted here and re-raised as
            _AuthSkip so it escapes the retry_on tuple entirely (zero
            retries, zero backoff delays). See _AuthSkip docstring.
            """
            try:
                return client.messages.create(
                    model=_MODEL,
                    max_tokens=_MAX_TOKENS,
                    temperature=0.0,
                    system=_persona(),
                    messages=[{"role": "user", "content": pkt_arg.text}],
                )
            except AuthenticationError:
                raise _AuthSkip() from None

        for esc in escs:
            pkt = await build_packet(pool, esc)

            try:
                resp = await _call_api(pkt)
            except _AuthSkip:
                logger.warning("llm_data_triage.auth_error_skipped", ref=esc.ref)
                out.skipped_no_key = True
                return out
            except Exception as call_exc:  # noqa: BLE001 — isolate per-escalation
                logger.error(
                    "llm_data_triage.call_error",
                    ref=esc.ref,
                    error=str(call_exc),
                )
                raise  # propagate to outer try/except → sets out.error

            try:
                txt = resp.content[0].text
                try:
                    prop = json.loads(txt)
                except json.JSONDecodeError as json_exc:
                    logger.warning(
                        "llm_data_triage.malformed_response",
                        ref=esc.ref,
                        error=str(json_exc),
                        response_preview=txt[:200],
                    )
                    continue  # skip this escalation, don't abort the loop

                if not isinstance(prop, dict):
                    logger.warning(
                        "llm_data_triage.non_dict_response",
                        ref=esc.ref,
                        response_preview=txt[:200],
                    )
                    continue  # skip this escalation, don't abort the loop

                # Sandbox + draft, human-merge-only PR FIRST so the
                # proposal event can carry pr_link (consumed by the
                # weekly digest §5). Fully crash-isolated: ANY failure
                # ⇒ pr_link is None and the advisory proposal below is
                # STILL emitted, no leaked worktree, escalation stays
                # for the human, NO raise. The proposal event is the
                # advisory artifact — it is ALWAYS emitted regardless
                # of PR outcome (invariant: advisory preserved).
                pr_link = await _open_draft_pr(
                    pool, esc, pkt, prop, pr_runner
                )

                await _emit(
                    pool,
                    "DATA_LLM_TRIAGE_PROPOSAL",
                    f"LLM triage proposal for ref={esc.ref}",
                    {
                        "schema": 1,
                        "ref": esc.ref,
                        "cls": esc.cls,
                        "persona_version": _PERSONA_VERSION,
                        "model": _MODEL,
                        "proposed_disposition": prop.get("proposed_disposition"),
                        "confidence": prop.get("confidence"),
                        "rationale": prop.get("rationale"),
                        "could_not_determine": prop.get("could_not_determine"),
                        "packet_hash": pkt.packet_hash,
                        "pr_link": pr_link,
                        "usage": {
                            "in": resp.usage.input_tokens,
                            "out": resp.usage.output_tokens,
                        },
                    },
                )
                out.proposed.append(esc.ref)
                logger.info(
                    "llm_data_triage.proposal_emitted",
                    ref=esc.ref,
                    model=_MODEL,
                    persona_version=_PERSONA_VERSION,
                    pr_link=pr_link,
                )
            except (IndexError, AttributeError, KeyError, TypeError) as parse_exc:
                logger.warning(
                    "llm_data_triage.malformed_response",
                    ref=esc.ref,
                    error=str(parse_exc),
                )
                continue  # skip this escalation, don't abort the loop

    except Exception as exc:  # noqa: BLE001 — never raises; crash-isolated
        logger.error("llm_data_triage.error", error=str(exc))
        out.error = str(exc)

    return out


__all__ = ["TriageOutcome", "run_triage"]


def main() -> None:  # pragma: no cover
    import os as _os

    from tpcore.db import build_asyncpg_pool

    async def _amain() -> None:
        dsn = _os.environ.get("DATABASE_URL") or _os.environ.get("DATABASE_URL_IPV4")
        if not dsn:
            logger.error("llm_data_triage.no_dsn")
            sys.exit(1)
        pool = await build_asyncpg_pool(dsn)
        try:
            result = await run_triage(pool)
        finally:
            await pool.close()
        print(
            f"llm_data_triage: proposed={result.proposed} "
            f"skipped_no_key={result.skipped_no_key} error={result.error}"
        )

    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()
