"""P1b — FMP ``/stable/profile`` adapter for CIK long-tail fallback.

Single-purpose adapter: when the SEC ticker map's
``SECTickerCIKMap.resolve_missing_ciks`` returns ``unresolved`` for a
ticker, this adapter asks FMP whether it carries a profile for the
symbol. Lower-authority than SEC; never used as primary; never
authoritative for country / sector / legal-name (those are owned by
``scripts/ops.py::_stage_fmp_profile_backfill``).

The adapter is **pure HTTP + parse**: no DB writes, no state, no
imports of asyncpg. Per-ticker errors are encoded in
``FMPProfileResult.state`` (a Literal). The adapter NEVER raises a
per-ticker exception — that's the stage's resilience contract so a
mid-batch outage doesn't strand the whole long-tail.

Spec: ``docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md``
Plan: ``docs/superpowers/plans/2026-06-01-p1b-cik-long-tail-backfill-plan.md``

Authoritative external:
  * FMP API docs: <https://site.financialmodelingprep.com/developer/docs/stable>
  * SEC EDGAR ticker→CIK map: ``tpcore/sec/ticker_cik_map.py``
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Literal

import httpx
import structlog

logger = structlog.get_logger(__name__)


FMP_BASE_URL: Final[str] = "https://financialmodelingprep.com/stable"
_PROFILE_PATH: Final[str] = "/profile"
_DEFAULT_TIMEOUT_S: Final[float] = 20.0


ResolutionState = Literal[
    "resolved",
    "no_match",
    "symbol_mismatch",
    "no_cik_in_profile",
    "ambiguous_response",
    "fmp_error",
]


@dataclass(frozen=True, slots=True)
class FMPProfileResult:
    """Outcome of one ``/stable/profile`` call for one ticker.

    Per-ticker errors are encoded in ``state`` — the adapter never
    raises a per-ticker exception. The stage maps the state to its
    operator-facing terminal-state counter and decides whether to
    persist + emit a divergence event.
    """

    requested_ticker: str
    """The ticker the caller asked us to resolve (input echo)."""

    state: ResolutionState
    """One of: resolved | no_match | symbol_mismatch |
    no_cik_in_profile | ambiguous_response | fmp_error."""

    cik: str | None = None
    """Zero-padded 10-digit CIK string (matches the SEC ticker-map
    convention at ``tpcore/sec/ticker_cik_map.py``). Set only when
    ``state == 'resolved'``."""

    returned_symbol: str | None = None
    """The ``symbol`` field FMP echoed in the first profile row, if
    any. Used by the stage when emitting an
    ``IDENTITY_DIVERGENCE_INVESTIGATE`` event so operator review has
    a concrete mismatch to compare."""

    country: str | None = None
    """FMP's ``country`` field (2-letter ISO if FMP normalized;
    otherwise the raw value). Diagnostic only — the P1b sub-leg DOES
    NOT persist this. Country writeback is the responsibility of the
    existing ``_stage_fmp_profile_backfill`` stage. Recorded here so
    the stage can include it in divergence-event payloads if useful."""

    profiles_count: int = 0
    """Number of profile dicts FMP returned. ``0`` for ``no_match``;
    ``1`` for the happy path; ``>= 2`` for ``ambiguous_response``."""

    http_status: int | None = None
    """HTTP status code of the final attempt (after retries). Set on
    ``fmp_error`` for diagnosis."""

    error_summary: str | None = None
    """Truncated 120-char summary of the underlying error for
    structured-log telemetry. Never includes API key or request body."""


def _zero_pad_cik(raw: str | int | None) -> str | None:
    """Coerce an FMP-returned ``cik`` value to the canonical 10-digit
    zero-padded string used by ``tpcore/sec/ticker_cik_map.py``.

    Returns ``None`` if ``raw`` is empty, non-numeric, or longer than
    10 digits (the SEC CIK space is 10 digits — anything longer is a
    parse error, not a real CIK).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if not s.isdigit():
        return None
    if len(s) > 10:
        return None
    return s.zfill(10)


def _normalize_symbol(value: str | None) -> str:
    """Case-insensitive trim. Empty for None."""
    if value is None:
        return ""
    return str(value).strip().upper()


async def fetch_profile(
    client: httpx.AsyncClient,
    ticker: str,
    *,
    api_key: str,
    retry_429_max: int = 3,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> FMPProfileResult:
    """Fetch one FMP ``/stable/profile`` for ``ticker`` and return a
    normalized result.

    Retry-on-429 with exponential sleep (2 s, 4 s, 8 s) capped at
    ``retry_429_max`` attempts. All other HTTP errors map to
    ``state='fmp_error'``. JSON-decode failures map to ``fmp_error``.

    The caller owns the ``httpx.AsyncClient`` so:
      * Tests can pass an ``httpx.MockTransport``-backed client and
        keep the suite hermetic.
      * Production callers can share a single client across many
        ticker resolutions in one batch (connection reuse).
    """
    requested = _normalize_symbol(ticker)
    if not requested:
        return FMPProfileResult(
            requested_ticker=ticker or "",
            state="fmp_error",
            error_summary="empty ticker passed to fetch_profile",
        )

    last_status: int | None = None
    last_error: str | None = None

    for attempt in range(retry_429_max):
        try:
            resp = await client.get(
                f"{FMP_BASE_URL}{_PROFILE_PATH}",
                params={"symbol": requested, "apikey": api_key},
                timeout=timeout_s,
            )
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            # Network errors → retry one more cycle with the same
            # backoff so a transient blip resolves; after the cap,
            # fall through to fmp_error.
            if attempt < retry_429_max - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            return FMPProfileResult(
                requested_ticker=ticker,
                state="fmp_error",
                error_summary=last_error,
            )

        last_status = resp.status_code
        if resp.status_code == 429:
            if attempt < retry_429_max - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            return FMPProfileResult(
                requested_ticker=ticker,
                state="fmp_error",
                http_status=resp.status_code,
                error_summary="rate-limited after retries",
            )

        if resp.status_code != 200:
            return FMPProfileResult(
                requested_ticker=ticker,
                state="fmp_error",
                http_status=resp.status_code,
                error_summary=f"HTTP {resp.status_code}",
            )

        # Happy path — parse JSON.
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — narrow per-ticker recovery
            return FMPProfileResult(
                requested_ticker=ticker,
                state="fmp_error",
                http_status=resp.status_code,
                error_summary=f"json_decode: {str(exc)[:80]}",
            )

        if not isinstance(payload, list):
            return FMPProfileResult(
                requested_ticker=ticker,
                state="fmp_error",
                http_status=resp.status_code,
                error_summary="payload is not a list",
            )

        return _classify_profile_payload(ticker, requested, payload)

    # Defensive — loop exited without returning. Shouldn't reach.
    return FMPProfileResult(
        requested_ticker=ticker,
        state="fmp_error",
        http_status=last_status,
        error_summary=last_error or "unknown error",
    )


def _classify_profile_payload(
    requested_ticker: str,
    requested_normalized: str,
    payload: list,
) -> FMPProfileResult:
    """Classify a parsed FMP /stable/profile JSON list into a terminal
    ``FMPProfileResult``. Pure function — no I/O, no retries.

    Decision tree (kept narrow so the test matrix is small):

      * empty list                                  → no_match
      * len >= 2                                    → ambiguous_response
      * len == 1 + symbol mismatch                  → symbol_mismatch
      * len == 1 + symbol match + cik missing/empty → no_cik_in_profile
      * len == 1 + symbol match + cik present       → resolved
    """
    count = len(payload)
    if count == 0:
        return FMPProfileResult(
            requested_ticker=requested_ticker,
            state="no_match",
            profiles_count=0,
        )

    if count >= 2:
        first = payload[0] if isinstance(payload[0], dict) else {}
        return FMPProfileResult(
            requested_ticker=requested_ticker,
            state="ambiguous_response",
            returned_symbol=str(first.get("symbol") or "") or None,
            profiles_count=count,
        )

    # len == 1.
    profile = payload[0]
    if not isinstance(profile, dict):
        return FMPProfileResult(
            requested_ticker=requested_ticker,
            state="fmp_error",
            profiles_count=1,
            error_summary="profile entry is not a dict",
        )

    returned = _normalize_symbol(profile.get("symbol"))
    country = profile.get("country") or None
    if returned != requested_normalized:
        return FMPProfileResult(
            requested_ticker=requested_ticker,
            state="symbol_mismatch",
            returned_symbol=returned or None,
            country=country,
            profiles_count=1,
        )

    cik_padded = _zero_pad_cik(profile.get("cik"))
    if cik_padded is None:
        return FMPProfileResult(
            requested_ticker=requested_ticker,
            state="no_cik_in_profile",
            returned_symbol=returned,
            country=country,
            profiles_count=1,
        )

    return FMPProfileResult(
        requested_ticker=requested_ticker,
        state="resolved",
        cik=cik_padded,
        returned_symbol=returned,
        country=country,
        profiles_count=1,
    )


__all__ = [
    "FMPProfileResult",
    "ResolutionState",
    "FMP_BASE_URL",
    "fetch_profile",
]
