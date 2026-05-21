"""Autonomous data-lane recovery — LLM action selector + bounded runner.

Operator directive (2026-05-21): "automate the god damn triage, no
operator-task bullshit in the self heal". When the in-orchestrator
cascade (scripts/ops.py auto-cascade + the in-flight smart-feed cascade)
exhausts on a data-lane failure and emits
``INGESTION_AUTO_RECOVERY_FAILED`` / ``DATA_REPAIR_ESCALATED`` /
``DATA_SOURCE_ESCALATED``, the triage daemon hands the escalation off
HERE. There is NO draft PR, NO human-merge gate — the LLM picks ONE
existing ``scripts/ops.py`` stage + params from a frozen whitelist, the
deterministic validator gates it, the bounded subprocess runs it.

This is the data-lane half of the autonomous-action split. The
ENGINE-lane and the ROSTER-lane stay PR-gated under the existing
draft-PR + human-merge path (``ops.llm_data_triage`` /
``ops.engine_llm_triage``) — code or roster mutations remain ECR-gated.

Safety boundary — NOT the persona, but the layered deterministic stack:

  1. The frozen Pydantic ``RecoveryAction`` contract  — malformed LLM
     output fails parse → REJECTED, no stage runs.
  2. The ``_AUTONOMOUS_DATA_ACTIONS`` whitelist            — non-
     whitelisted stage / param names → REJECTED, no stage runs.
  3. Per-param value sanity in ``validate_recovery_action`` — out-of-
     range / wrong-type values → REJECTED, no stage runs.
  4. The subprocess runner's per-stage timeout              — runaway
     stages are killed.
  5. The single-shot policy — a FAILED recovery NEVER recurses; the
     next escalation cycle decides whether to try again (re-entered
     through application_log + the triage daemon, not in-process).

Terminal events emitted on platform.application_log:

  * ``DATA_RECOVERY_ACTION_REJECTED`` — LLM picked something the
    validator rejected (whitelist miss, bad params, malformed JSON,
    or the LLM returned the ``noop`` sentinel because the escalation
    is not stage-recoverable).
  * ``DATA_RECOVERY_ACTION_SUCCEEDED`` — stage subprocess exit 0.
  * ``DATA_RECOVERY_ACTION_FAILED``   — stage subprocess exit ≠ 0 or
    timeout / crash.

Reuse posture (matches the shipped ``ops.llm_data_triage`` /
``ops.engine_llm_triage`` reuse rule — symmetry of approach, not clone):
the ``AsyncAnthropic`` client, the credential-starved env, the SDK
exception envelope, and the per-process retry/backoff via
``tpcore.outage.with_retry`` are the SAME idioms; the action-selection
prompt + the JSON action contract + the deterministic subprocess
runner are this module's delta.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from anthropic import (
    APIError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tpcore.outage import with_retry

logger = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Public surface — module pins (frozen at import time).
# ────────────────────────────────────────────────────────────────────────


# Persona version selection — v2 is the default (pattern-matching catalogue
# for the 2026-05-21 incident shapes); v1 stays available for rollback via
# the ``LLM_DATA_RECOVERY_PERSONA_VERSION=v1`` environment override.
_PERSONA_VERSION_ENV = "LLM_DATA_RECOVERY_PERSONA_VERSION"
_PERSONA_DEFAULT_VERSION = "v2"
_PERSONA_VALID_VERSIONS = frozenset({"v1", "v2"})


def _resolve_persona_version() -> str:
    requested = os.environ.get(_PERSONA_VERSION_ENV, _PERSONA_DEFAULT_VERSION)
    if requested not in _PERSONA_VALID_VERSIONS:
        return _PERSONA_DEFAULT_VERSION
    return requested


PERSONA_VERSION = _resolve_persona_version()
_PERSONA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs"
    / "llm_triage_personas"
    / f"data_recovery_{PERSONA_VERSION}.md"
)
_PERSONA_TEXT: str = _PERSONA_PATH.read_text(encoding="utf-8")
_PERSONA_SHA = hashlib.sha256(_PERSONA_TEXT.encode("utf-8")).hexdigest()

# Match ops.llm_data_triage._MODEL / _MAX_TOKENS pins (NEVER re-pinned
# here; the operator's standing rule is one model pin per session).
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

# Bounded turn quota — one LLM call per escalation, no chain, no tools.
_MAX_LLM_TURNS = 1

# Per-cycle cost ledger — accrued in-memory and stamped into the
# terminal event payload. The bus is the audit surface.
_COST_LEDGER: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

# Engine tag emitted to platform.application_log — matches the
# data_repair_service convention (engine = source-identifier string).
_AGENT_ENGINE_TAG = "llm_data_recovery"

# Default per-action subprocess timeout (seconds). The Pydantic
# RecoveryAction caps this further — LLM cannot exceed _MAX_TIMEOUT_SEC.
_DEFAULT_TIMEOUT_SEC = 3600.0
_MAX_TIMEOUT_SEC = 3600.0

# Reused, byte-mirrored from ops/data_repair_service convention:
# (engine, run_id, event_type, severity, message, data::jsonb).
_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""


# ────────────────────────────────────────────────────────────────────────
# Whitelist — the closed set of (stage_name, allowed_params) the LLM is
# allowed to invoke. Operator directive: "operational re-runs of existing
# stages = autonomous; code or roster mutations = still PR-gated".
#
# Frozen frozenset-of-tuples-of-frozensets so the whitelist is hash-stable
# and immutable per-process. Adding a stage here is the ONLY way to grow
# the autonomous-action surface — and itself requires a PR.
# ────────────────────────────────────────────────────────────────────────


_AUTONOMOUS_DATA_ACTIONS: frozenset[tuple[str, frozenset[str]]] = frozenset({
    # daily_bars covers force-refresh, the two bounded self-heal modes
    # (repair_gaps / repair_coverage), feed selection (iex/sip), and
    # window narrowing (lookback_days / end_offset_days).
    (
        "daily_bars",
        frozenset({
            "force_refresh",
            "repair_gaps",
            "repair_coverage",
            "universe",
            "feed",
            "lookback_days",
            "end_offset_days",
        }),
    ),
    # data_validation — no params; pure re-run of the 10-check suite.
    ("data_validation", frozenset()),
    # reconcile — no params; re-runs the Alpaca open-orders reconcile.
    ("reconcile", frozenset()),
    # forensics — no params; re-runs the AAR forensics pass.
    ("forensics", frozenset()),
    # corporate_actions — refresh corporate-actions table; no params.
    ("corporate_actions", frozenset()),
    # coverage_fill — bounded fill stage; no params today.
    ("coverage_fill", frozenset()),
    # Tradier CSV bulk-extract → ingest chain (operator-on-demand;
    # operational re-runs allowed).
    ("extract_tradier_full", frozenset()),
    ("ingest_tradier_csv", frozenset()),
    # fundamentals_refresh (v2 / Pattern 5): the
    # ``fundamentals_quarterly_completeness`` validation check (PR #172)
    # is the gate; when it fails, refreshing the source then re-running
    # validation on the next cycle is the bounded heal. The stage's
    # config keys (min_price/min_volume/lookback_days) are intentionally
    # NOT exposed to the LLM — defaults are operator-tuned, and the LLM
    # has no evidence that would justify overriding them.
    ("fundamentals_refresh", frozenset()),
})


# ────────────────────────────────────────────────────────────────────────
# v2 pattern guards (operator decisions 2026-05-21):
#
#   * ``_SKIP_WITH_WARNING_ACTIONS`` — stages where there IS NO LLM-runnable
#     recovery (Pattern 4: greeks_pro 401 = operator-credential, not LLM-
#     recoverable). If the LLM returns one of these, the dispatcher emits
#     ``DATA_RECOVERY_ACTION_SKIPPED`` instead of invoking the stage. These
#     stages are NOT in the autonomous whitelist either — they are pure
#     skip-only landmines for when the LLM tries general reasoning.
#
#   * ``_NEGATIVE_PATTERNS`` — ``(error_substring, banned_stage_name)``
#     pairs. When the current escalation message contains ``error_substring``
#     AND the LLM returns an action whose ``stage_name`` matches
#     ``banned_stage_name``, the action is REJECTED with
#     ``reason=negative_pattern_match``. Pattern 6: ``repair_gaps`` on
#     ``coverage collapse`` is the documented anti-pattern (completeness
#     check threshold is blind to coverage_collapse).
#
# The dispatcher VALIDATES against these guards; it does NOT pattern-match
# itself. Pattern matching is the LLM's job (the v2 persona's first
# section); the dispatcher is the safety boundary that catches a wrong
# pick the LLM made anyway.
# ────────────────────────────────────────────────────────────────────────


_SKIP_WITH_WARNING_ACTIONS: frozenset[str] = frozenset({
    # Pattern 4: greeks_max_pain 401 — third-party API auth. Operator
    # rotates the credential; the LLM cannot. Stage is also absent from
    # ``_AUTONOMOUS_DATA_ACTIONS`` — double-fence.
    "greeks_max_pain",
})


_NEGATIVE_PATTERNS: frozenset[tuple[str, str]] = frozenset({
    # Pattern 6: "coverage collapse" + repair_gaps is the documented
    # blind-spot. The completeness check threshold (PR #231 cascade)
    # routes around this for the orchestrator; the LLM must NEVER re-pick
    # repair_gaps on a coverage_collapse escalation.
    ("coverage collapse", "repair_gaps"),
})

# O(1) lookup map derived from the frozen whitelist.
_WHITELIST_MAP: dict[str, frozenset[str]] = {
    name: params for name, params in _AUTONOMOUS_DATA_ACTIONS
}


# Per-param sanity gates. Values that pass the whitelist still must clear
# these — a non-bool ``force_refresh`` or a 999-day ``lookback_days`` is
# REJECTED. Anything not listed here is param-name-allowed-but-value-
# constrained-only-by-type (the Pydantic frozen contract).
_FEED_ALLOWED: frozenset[str] = frozenset({"iex", "sip"})
_UNIVERSE_ALLOWED: frozenset[str] = frozenset(
    {"active", "tier_1_2", "all_active", "all_active_with_history_to_2000"}
)
_UNIVERSE_MAX_CSV_LEN = 4096
_LOOKBACK_MIN_DAYS = 1
_LOOKBACK_MAX_DAYS = 30
_END_OFFSET_MIN = 0
_END_OFFSET_MAX = 7


# ────────────────────────────────────────────────────────────────────────
# Frozen Pydantic v2 contracts.
# ────────────────────────────────────────────────────────────────────────


class RecoveryAction(BaseModel):
    """One LLM-selected recovery action. Frozen + extra-forbid: ONLY
    these fields, ONLY this shape. Anything malformed raises
    ValidationError and the recovery lands REJECTED."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.0
    timeout: float = _DEFAULT_TIMEOUT_SEC


class RecoveryResult(BaseModel):
    """Bounded-subprocess outcome. ``ok`` is the gate; ``error`` is the
    audit string; ``returncode`` is the raw subprocess exit code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    returncode: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str = ""
    duration_ms: int = 0


class RecoveryContext(BaseModel):
    """Read-only LLM packet. The text payload is JSON; ``text`` is what
    the LLM actually sees. ``packet_hash`` is the audit fingerprint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    packet_hash: str
    escalation_event_type: str
    escalation_message: str


# ────────────────────────────────────────────────────────────────────────
# Private sentinels (mirror the shipped ops.llm_data_triage convention).
# ────────────────────────────────────────────────────────────────────────


class _AuthSkip(Exception):
    """Signals AuthenticationError. Treated identically to missing key:
    safe no-op, zero retries, zero emits."""


# Public alias for the shared SDK surface (mirrors the data-triage
# AuthSkip alias — same idiom).
AuthSkip = _AuthSkip


# ────────────────────────────────────────────────────────────────────────
# Credential-starved env for the subprocess. Allowlist-built dict — never
# os.environ.copy(). Forbidden vars (ANTHROPIC_*, ALPACA_*, *_KEY,
# *_TOKEN, DATABASE_URL*) are never even READ into the result, so they
# cannot leak. Mirrors ops.llm_data_triage._scrubbed_env's allowlist.
# ────────────────────────────────────────────────────────────────────────


_SUBPROCESS_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TMPDIR")
_SUBPROCESS_ENV_ALLOWLIST_PREFIXES = ("PYTHON",)
# The subprocess (scripts/ops.py) NEEDS a DB DSN to actually do work.
# This is the ONE production-credential exception. ANTHROPIC keys /
# ALPACA keys / SUPABASE secrets are deliberately NOT in this list — the
# stage subprocess never needs them (it talks to Postgres + Tradier/etc
# via its own adapter env, which IS the DB the orchestrator owns).
_SUBPROCESS_ENV_DB_DSN_KEYS = ("DATABASE_URL", "DATABASE_URL_IPV4")


def _subprocess_env() -> dict[str, str]:
    """A fresh allowlist-built env dict for the stage subprocess.

    Includes the DB DSN (the subprocess cannot run without it) but
    EXCLUDES every Anthropic / Alpaca / Supabase / generic *_KEY /
    *_TOKEN variable. The subprocess inherits NOTHING by default —
    every kept var is positively named.
    """
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if (
            k in _SUBPROCESS_ENV_ALLOWLIST
            or k in _SUBPROCESS_ENV_DB_DSN_KEYS
            or any(
                k.upper().startswith(p)
                for p in _SUBPROCESS_ENV_ALLOWLIST_PREFIXES
            )
        ):
            env[k] = v
    return env


# ────────────────────────────────────────────────────────────────────────
# Context builder — the LLM input packet.
# ────────────────────────────────────────────────────────────────────────


_RECENT_LOG_SQL = """
SELECT recorded_at, event_type, severity, message
FROM platform.application_log
WHERE recorded_at > now() - interval '4 hours'
  AND event_type IN (
      'INGESTION_FAILED',
      'INGESTION_AUTO_RECOVERY_START',
      'INGESTION_AUTO_RECOVERY_FAILED',
      'INGESTION_COMPLETE',
      'DATA_REPAIR_ESCALATED',
      'DATA_SOURCE_ESCALATED',
      'DATA_RECOVERY_ACTION_REJECTED',
      'DATA_RECOVERY_ACTION_SUCCEEDED',
      'DATA_RECOVERY_ACTION_FAILED'
  )
ORDER BY recorded_at DESC
LIMIT 25
"""


def _whitelist_for_packet() -> list[dict[str, Any]]:
    """Serialise the whitelist into a JSON-stable list for the LLM."""
    return sorted(
        (
            {
                "stage_name": name,
                "allowed_params": sorted(allowed),
            }
            for name, allowed in _AUTONOMOUS_DATA_ACTIONS
        ),
        key=lambda d: d["stage_name"],
    )


async def build_data_recovery_context(
    event: dict[str, Any],
    pool: Any,
) -> RecoveryContext:
    """Assemble the read-only LLM packet for ONE escalation.

    Pure read: NO writes, NO LLM call, NO mutation. The packet carries
    the escalation event itself, a short tail of recent application_log
    events from the same time window, and the (frozen) whitelist of
    actions the LLM is allowed to pick from.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RECENT_LOG_SQL)

    payload: dict[str, Any] = {
        "escalation": {
            "event_type": event.get("event_type") or "",
            "message": event.get("message") or "",
            "data": event.get("data") or {},
            "recorded_at": str(event.get("recorded_at") or ""),
        },
        "recent_application_log": [dict(r) for r in rows],
        "whitelist": _whitelist_for_packet(),
        "param_sanity": {
            "feed_allowed": sorted(_FEED_ALLOWED),
            "universe_allowed": sorted(_UNIVERSE_ALLOWED),
            "lookback_days_range": [_LOOKBACK_MIN_DAYS, _LOOKBACK_MAX_DAYS],
            "end_offset_days_range": [_END_OFFSET_MIN, _END_OFFSET_MAX],
        },
    }
    text = json.dumps(payload, sort_keys=True, default=str)
    packet_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RecoveryContext(
        text=text,
        packet_hash=packet_hash,
        escalation_event_type=str(event.get("event_type") or ""),
        escalation_message=str(event.get("message") or ""),
    )


# ────────────────────────────────────────────────────────────────────────
# LLM call — bounded turn quota, frozen contract, cost-tracked.
# ────────────────────────────────────────────────────────────────────────


def _default_client() -> AsyncAnthropic:
    """The official AsyncAnthropic client (matches ops.llm_data_triage)."""
    return AsyncAnthropic()


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Best-effort: pull the JSON object out of an LLM response that may
    have ignored the no-fence instruction and wrapped its output."""
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


async def llm_recovery_decision(
    context: RecoveryContext,
    *,
    client_factory: Callable[[], Any] = _default_client,
) -> RecoveryAction | None:
    """Ask the LLM to pick ONE action. Returns ``None`` on:

    * Missing API key (safe no-op).
    * AuthenticationError (key invalid/exhausted — safe no-op).
    * Malformed JSON / ValidationError (the caller emits REJECTED).
    * Any LLM round-trip exception (the caller emits REJECTED).

    Single LLM call. No chains. No tools. No recursion. Bounded by
    ``_MAX_LLM_TURNS=1`` structurally (one ``messages.create`` invocation
    per call to this function).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("llm_data_recovery.no_api_key")
        return None

    client = client_factory()
    try:

        @with_retry(
            max_attempts=3,
            backoff_base_sec=2.0,
            backoff_cap_sec=30.0,
            retry_on=(RateLimitError, APIError),
        )
        async def _call_api() -> Any:
            try:
                return await client.messages.create(
                    model=_MODEL,
                    max_tokens=_MAX_TOKENS,
                    temperature=0.0,
                    system=_PERSONA_TEXT,
                    messages=[{"role": "user", "content": context.text}],
                )
            except AuthenticationError:
                raise _AuthSkip() from None

        try:
            resp = await _call_api()
        except _AuthSkip:
            logger.warning("llm_data_recovery.auth_error_skipped")
            return None

        # Accrue cost — visible on every terminal event payload.
        try:
            _COST_LEDGER["input_tokens"] += int(resp.usage.input_tokens)
            _COST_LEDGER["output_tokens"] += int(resp.usage.output_tokens)
        except (AttributeError, TypeError, ValueError):
            pass

        # Parse → validate → ValidationError ⇒ caller emits REJECTED.
        raw_text = resp.content[0].text
        json_text = _extract_json(raw_text)
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "llm_data_recovery.malformed_json",
                error=str(exc),
                preview=raw_text[:200],
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "llm_data_recovery.non_dict_response", preview=raw_text[:200]
            )
            return None

        try:
            return RecoveryAction(**data)
        except ValidationError as exc:
            logger.warning(
                "llm_data_recovery.contract_violation",
                error=str(exc),
                preview=raw_text[:200],
            )
            return None
    except Exception as exc:  # noqa: BLE001 — never propagate; advisory
        logger.error("llm_data_recovery.call_error", error=str(exc))
        return None
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as exc:  # noqa: BLE001 — best effort
                logger.warning(
                    "llm_data_recovery.aclose_failed", error=str(exc)
                )


# ────────────────────────────────────────────────────────────────────────
# Deterministic validator — the action safety boundary.
# ────────────────────────────────────────────────────────────────────────


def _match_negative_pattern(
    escalation_message: str, action: RecoveryAction
) -> tuple[str, str] | None:
    """Return the matched (error_substring, banned_token) pair from
    ``_NEGATIVE_PATTERNS`` when the current escalation's message contains
    ``error_substring`` AND the action picks the banned token (either as
    the ``stage_name`` itself OR as a truthy param name on the action);
    ``None`` if no negative pattern is hit.

    The dual stage_name / params-key match handles two ways the LLM can
    re-pick a documented anti-pattern:

      1. ``stage_name == banned_token`` — token is itself a stage in the
         whitelist (general case).
      2. ``params.get(banned_token) is True`` — token is a mode/param
         on a parent stage (Pattern 6: ``repair_gaps=true`` on
         ``daily_bars`` for a coverage_collapse escalation; ``repair_gaps``
         is the mode name, not a stage name).

    Case-insensitive substring match on the message; exact match on the
    stage / param name. The check is a constant-size loop over a frozen
    set — structurally bounded.
    """
    if not escalation_message:
        return None
    msg_lc = escalation_message.lower()
    for err_sub, banned_token in _NEGATIVE_PATTERNS:
        if err_sub.lower() not in msg_lc:
            continue
        if action.stage_name == banned_token:
            return err_sub, banned_token
        if action.params.get(banned_token) is True:
            return err_sub, banned_token
    return None


def validate_recovery_action(action: RecoveryAction) -> tuple[bool, str]:
    """Gate: (stage in whitelist) AND (every param in stage's allowed
    set) AND (per-param value passes sanity). Returns ``(ok, reason)``.
    """
    if action.stage_name not in _WHITELIST_MAP:
        return False, f"stage '{action.stage_name}' not in whitelist"
    allowed = _WHITELIST_MAP[action.stage_name]
    extra = set(action.params) - set(allowed)
    if extra:
        return False, f"params {sorted(extra)} not allowed for stage '{action.stage_name}'"

    # Per-param value sanity.
    for k, v in action.params.items():
        if k == "force_refresh" or k == "repair_gaps" or k == "repair_coverage":
            if not isinstance(v, bool):
                return False, f"param '{k}' must be bool, got {type(v).__name__}"
        elif k == "feed":
            if not isinstance(v, str) or v not in _FEED_ALLOWED:
                return False, f"param 'feed' must be one of {sorted(_FEED_ALLOWED)}"
        elif k == "universe":
            if not isinstance(v, str):
                return False, "param 'universe' must be str"
            if v in _UNIVERSE_ALLOWED:
                continue
            # csv string ≤ 4kb
            if len(v) > _UNIVERSE_MAX_CSV_LEN:
                return False, (
                    f"param 'universe' csv length {len(v)} > {_UNIVERSE_MAX_CSV_LEN}"
                )
            # Hard-block the catastrophic combo even if 'all_active' is the
            # listed value (operator directive: force_refresh=true with
            # all_active universe = too big).
            # Check below outside the per-param loop covers the combo.
        elif k == "lookback_days":
            if not isinstance(v, int) or isinstance(v, bool):
                return False, "param 'lookback_days' must be int"
            if not (_LOOKBACK_MIN_DAYS <= v <= _LOOKBACK_MAX_DAYS):
                return False, (
                    f"param 'lookback_days' {v} out of range "
                    f"[{_LOOKBACK_MIN_DAYS}, {_LOOKBACK_MAX_DAYS}]"
                )
        elif k == "end_offset_days":
            if not isinstance(v, int) or isinstance(v, bool):
                return False, "param 'end_offset_days' must be int"
            if not (_END_OFFSET_MIN <= v <= _END_OFFSET_MAX):
                return False, (
                    f"param 'end_offset_days' {v} out of range "
                    f"[{_END_OFFSET_MIN}, {_END_OFFSET_MAX}]"
                )

    # Hard-block: full-universe force_refresh — the catastrophic combo
    # called out in the operator directive.
    if (
        action.stage_name == "daily_bars"
        and bool(action.params.get("force_refresh"))
        and str(action.params.get("universe", "")) in {
            "all_active",
            "all_active_with_history_to_2000",
        }
    ):
        return False, (
            "daily_bars force_refresh=true with universe='all_active' is "
            "blocked (operator-banned catastrophic combo)"
        )

    # Timeout cap.
    if action.timeout <= 0 or action.timeout > _MAX_TIMEOUT_SEC:
        return False, (
            f"timeout {action.timeout}s out of range (0, {_MAX_TIMEOUT_SEC}]"
        )
    return True, ""


# ────────────────────────────────────────────────────────────────────────
# Bounded subprocess runner — the same invocation operator would type,
# minus the credentials we deliberately strip.
# ────────────────────────────────────────────────────────────────────────


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _build_argv(action: RecoveryAction) -> list[str]:
    """Translate a validated action into the canonical CLI invocation."""
    argv: list[str] = [
        sys.executable,
        "scripts/ops.py",
        "--stage",
        action.stage_name,
    ]
    for k, v in action.params.items():
        # bools render as 'true' / 'false' — scripts/ops.py parses them.
        if isinstance(v, bool):
            argv.extend(["--param", f"{k}={'true' if v else 'false'}"])
        else:
            argv.extend(["--param", f"{k}={v}"])
    # daily_bars stage needs --force when run during market hours; the
    # autonomous lane is OK to bypass the market-closed gate (the operator
    # already chose to run an in-orchestrator cascade that hit this path,
    # and the smart-feed cascade itself bypasses the same gate).
    if action.stage_name == "daily_bars":
        argv.append("--force")
    return argv


def _default_subprocess_runner(
    argv: list[str], *, env: dict[str, str], cwd: str, timeout: float
) -> tuple[int, str, str]:
    """Fixed-argv, no-shell, bounded subprocess. Tests inject a fake."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, allowlist env
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


async def run_ops_stage(
    action: RecoveryAction,
    *,
    runner: Callable[..., tuple[int, str, str]] = _default_subprocess_runner,
) -> RecoveryResult:
    """Run the validated action via ``scripts/ops.py --stage`` in a
    fresh, credential-starved subprocess. NEVER raises — every failure
    path returns a RecoveryResult with ok=False + an audit string."""
    started = asyncio.get_running_loop().time()
    argv = _build_argv(action)
    env = _subprocess_env()
    try:
        rc, out, err = await asyncio.to_thread(
            runner, argv, env=env, cwd=str(_REPO_ROOT), timeout=action.timeout
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int(
            (asyncio.get_running_loop().time() - started) * 1000
        )
        return RecoveryResult(
            ok=False,
            returncode=-1,
            stdout_tail="",
            stderr_tail=(str(exc) or "")[-2048:],
            error=f"subprocess timed out after {action.timeout}s",
            duration_ms=elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001 — never raise
        elapsed_ms = int(
            (asyncio.get_running_loop().time() - started) * 1000
        )
        return RecoveryResult(
            ok=False,
            returncode=-1,
            stdout_tail="",
            stderr_tail="",
            error=f"subprocess crashed: {exc}",
            duration_ms=elapsed_ms,
        )
    elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
    return RecoveryResult(
        ok=(rc == 0),
        returncode=rc,
        stdout_tail=(out or "")[-2048:],
        stderr_tail=(err or "")[-2048:],
        error="" if rc == 0 else f"stage exit code {rc}",
        duration_ms=elapsed_ms,
    )


# ────────────────────────────────────────────────────────────────────────
# Event emission (terminal: REJECTED / SUCCEEDED / FAILED).
# ────────────────────────────────────────────────────────────────────────


async def emit_event(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """Insert one terminal recovery event. Matches the data_repair_service
    INSERT shape verbatim (engine, run_id, event_type, severity, message,
    data::jsonb)."""
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


# ────────────────────────────────────────────────────────────────────────
# Public top-level handler — the one entrypoint the triage daemon calls.
# ────────────────────────────────────────────────────────────────────────


async def handle_data_recovery_escalation(
    pool: Any,
    event: dict[str, Any],
    *,
    client_factory: Callable[[], Any] = _default_client,
    runner: Callable[..., tuple[int, str, str]] = _default_subprocess_runner,
) -> str:
    """Drive ONE escalation through the autonomous chain:

      build context → LLM picks action → validate → run → emit terminal.

    Returns the terminal event_type emitted (``DATA_RECOVERY_ACTION_
    REJECTED`` / ``..._SUCCEEDED`` / ``..._FAILED``). Single-shot: a
    FAILED outcome is NOT retried in-process; the next escalation cycle
    re-enters this handler through the daemon poll.
    """
    try:
        context = await build_data_recovery_context(event, pool)
    except Exception as exc:  # noqa: BLE001 — never raise
        logger.error("llm_data_recovery.context_build_failed", error=str(exc))
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_REJECTED",
            f"context build failed: {exc}",
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "reason": "context_build_failed",
                "error": str(exc),
                "trigger_event_type": event.get("event_type") or "",
            },
            severity="ERROR",
        )
        return "DATA_RECOVERY_ACTION_REJECTED"

    action = await llm_recovery_decision(
        context, client_factory=client_factory
    )
    if action is None:
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_REJECTED",
            "LLM returned no action (no key / malformed / contract violation)",
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "reason": "no_action",
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "trigger_message": context.escalation_message,
                "cost": dict(_COST_LEDGER),
            },
            severity="WARNING",
        )
        return "DATA_RECOVERY_ACTION_REJECTED"

    # v2 guard: skip-with-warning. Some stages have no LLM-runnable
    # recovery (Pattern 4: third-party API auth). If the LLM picks one
    # of these anyway, emit SKIPPED and do NOT invoke the subprocess.
    if action.stage_name in _SKIP_WITH_WARNING_ACTIONS:
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_SKIPPED",
            (
                f"recovery skipped: stage={action.stage_name} has no "
                f"LLM-runnable recovery (operator credential / provider auth)"
            ),
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "reason": "provider_auth_failure",
                "action": action.model_dump(),
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "trigger_message": context.escalation_message,
                "cost": dict(_COST_LEDGER),
            },
            severity="WARNING",
        )
        return "DATA_RECOVERY_ACTION_SKIPPED"

    # v2 guard: negative-pattern match. Some (error_substring, stage)
    # pairs are documented blind spots — Pattern 6: repair_gaps is
    # blind to coverage_collapse. If the LLM picks a banned combination,
    # REJECT before invoking.
    negative_match = _match_negative_pattern(
        context.escalation_message, action
    )
    if negative_match is not None:
        err_sub, banned_stage = negative_match
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_REJECTED",
            (
                f"action rejected: negative pattern match "
                f"(error substring={err_sub!r}, banned stage={banned_stage!r})"
            ),
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "reason": "negative_pattern_match",
                "negative_pattern": {
                    "error_substring": err_sub,
                    "banned_stage_name": banned_stage,
                },
                "action": action.model_dump(),
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "trigger_message": context.escalation_message,
                "cost": dict(_COST_LEDGER),
            },
            severity="WARNING",
        )
        return "DATA_RECOVERY_ACTION_REJECTED"

    ok, reason = validate_recovery_action(action)
    if not ok:
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_REJECTED",
            f"action rejected: {reason}",
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "reason": reason,
                "action": action.model_dump(),
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "cost": dict(_COST_LEDGER),
            },
            severity="WARNING",
        )
        return "DATA_RECOVERY_ACTION_REJECTED"

    # v2 Pattern 3: an IEX failover (LLM picked feed=iex on daily_bars
    # to route around a SIP-subscription 403) is a degraded recovery —
    # partial coverage > nothing, but the operator still needs to see
    # the SIP outage. Emit the degraded marker BEFORE running so the
    # bus carries the cause-of-degradation even if the stage itself
    # then succeeds.
    if (
        action.stage_name == "daily_bars"
        and str(action.params.get("feed", "")) == "iex"
    ):
        await emit_event(
            pool,
            "INGESTION_AUTO_RECOVERY_DEGRADED",
            (
                "autonomous recovery picked IEX failover — partial coverage; "
                "SIP-subscription / 403 likely; operator: investigate"
            ),
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "action": action.model_dump(),
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "trigger_message": context.escalation_message,
            },
            severity="WARNING",
        )

    result = await run_ops_stage(action, runner=runner)
    if result.ok:
        await emit_event(
            pool,
            "DATA_RECOVERY_ACTION_SUCCEEDED",
            (
                f"autonomous recovery ok: stage={action.stage_name} "
                f"params={action.params} duration_ms={result.duration_ms}"
            ),
            {
                "schema": 1,
                "persona_version": PERSONA_VERSION,
                "persona_sha": _PERSONA_SHA,
                "action": action.model_dump(),
                "result": result.model_dump(),
                "packet_hash": context.packet_hash,
                "trigger_event_type": context.escalation_event_type,
                "cost": dict(_COST_LEDGER),
            },
            severity="INFO",
        )
        return "DATA_RECOVERY_ACTION_SUCCEEDED"

    await emit_event(
        pool,
        "DATA_RECOVERY_ACTION_FAILED",
        (
            f"autonomous recovery FAILED: stage={action.stage_name} "
            f"rc={result.returncode} error={result.error}"
        ),
        {
            "schema": 1,
            "persona_version": PERSONA_VERSION,
            "persona_sha": _PERSONA_SHA,
            "action": action.model_dump(),
            "result": result.model_dump(),
            "packet_hash": context.packet_hash,
            "trigger_event_type": context.escalation_event_type,
            "cost": dict(_COST_LEDGER),
        },
        severity="ERROR",
    )
    return "DATA_RECOVERY_ACTION_FAILED"


# ────────────────────────────────────────────────────────────────────────
# Daemon-facing wrapper: poll the latest data-lane trigger event the
# triage daemon's cursor advanced over, hand it to the autonomous chain.
# Mirrors ops.llm_data_triage.run_triage's signature (one positional
# ``pool``) so the lane_loop can swap triage_fn without ceremony.
# ────────────────────────────────────────────────────────────────────────


_LATEST_TRIGGER_SQL = """
SELECT recorded_at, event_type, severity, message, data
FROM platform.application_log
WHERE event_type = ANY($1::text[])
ORDER BY recorded_at DESC
LIMIT 1
"""

# Autonomous data-lane triggers. Operator directive (2026-05-21):
# "no operator-task bullshit in the self heal" — all three data-lane
# escalations route through the autonomous chain, not draft-PR.
AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES: tuple[str, ...] = (
    "DATA_REPAIR_ESCALATED",
    "DATA_SOURCE_ESCALATED",
    "INGESTION_AUTO_RECOVERY_FAILED",
)


async def run_autonomous_recovery(
    pool: Any,
    *,
    client_factory: Callable[[], Any] = _default_client,
    runner: Callable[..., tuple[int, str, str]] = _default_subprocess_runner,
) -> str | None:
    """Fetch the most recent autonomous-trigger event and drive it.

    Returns the terminal event_type emitted, or ``None`` if there is no
    open trigger to process (a safe no-op — the lane_loop's cursor
    already advanced; this is the lane_loop firing its triage_fn).
    Never raises (advisory + crash-isolated by the daemon).
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _LATEST_TRIGGER_SQL,
                list(AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES),
            )
    except Exception as exc:  # noqa: BLE001 — advisory
        logger.error(
            "llm_data_recovery.trigger_select_failed", error=str(exc)
        )
        return None
    if row is None:
        return None

    # asyncpg returns ``data`` as a string when stored as jsonb. Normalise
    # to dict for the handler's interface.
    raw_data = row["data"]
    if isinstance(raw_data, (bytes, bytearray)):
        raw_data = raw_data.decode("utf-8", errors="replace")
    if isinstance(raw_data, str):
        try:
            parsed_data: Any = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed_data = {}
    else:
        parsed_data = raw_data or {}

    event: dict[str, Any] = {
        "event_type": row["event_type"],
        "message": row["message"],
        "recorded_at": row["recorded_at"],
        "data": parsed_data,
    }
    return await handle_data_recovery_escalation(
        pool, event, client_factory=client_factory, runner=runner
    )


__all__ = [
    "AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES",
    "AuthSkip",
    "PERSONA_VERSION",
    "RecoveryAction",
    "RecoveryContext",
    "RecoveryResult",
    "build_data_recovery_context",
    "emit_event",
    "handle_data_recovery_escalation",
    "llm_recovery_decision",
    "run_autonomous_recovery",
    "run_ops_stage",
    "validate_recovery_action",
]


# Public, frozen helper re-exports for test/spec inspection (v2). The
# leading-underscore variants stay the canonical names; these aliases let
# tests assert against a stable public surface without an inline
# ``noqa: SLF001`` (per-file SLF ignore is the policy form).
NEGATIVE_PATTERNS: frozenset[tuple[str, str]] = _NEGATIVE_PATTERNS
SKIP_WITH_WARNING_ACTIONS: frozenset[str] = _SKIP_WITH_WARNING_ACTIONS
