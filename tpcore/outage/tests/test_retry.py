"""Tests for ``tpcore.outage.retry.with_retry``.

Pins the contract every adapter and handler in the platform depends on:

* Retry on transient errors (429, 5xx, network, timeout).
* No retry on permanent errors (4xx-not-429).
* Honor ``Retry-After`` header when present.
* Re-raise on exhaustion.
* Logging-friendly metadata on each retry.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from tpcore.outage.retry import with_retry


def _http_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    """Build a real httpx.HTTPStatusError with a synthetic response."""
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(
        status_code=status,
        headers=headers,
        request=httpx.Request("GET", "https://example.test/data"),
    )
    return httpx.HTTPStatusError("error", request=response.request, response=response)


# ─── Happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_retry_when_first_attempt_succeeds():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        return "ok"

    assert await fn() == "ok"
    assert calls["n"] == 1


# ─── Retry on transient ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429)
        return "ok"

    with patch("asyncio.sleep"):  # don't actually sleep
        assert await fn() == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return "ok"

    with patch("asyncio.sleep"):
        assert await fn() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retries_on_network_error():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.NetworkError("connection reset")
        return "ok"

    with patch("asyncio.sleep"):
        assert await fn() == "ok"
    assert calls["n"] == 2


# ─── No retry on permanent ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_retry_on_400_bad_request():
    """4xx-not-429 is permanent — don't waste retries."""
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        raise _http_error(400)

    with patch("asyncio.sleep"), pytest.raises(httpx.HTTPStatusError):
        await fn()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_retry_on_404():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        raise _http_error(404)

    with patch("asyncio.sleep"), pytest.raises(httpx.HTTPStatusError):
        await fn()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_retry_on_non_retry_on_exception():
    """ValueError isn't in retry_on — fall through immediately."""
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        raise ValueError("not a transient")

    with patch("asyncio.sleep"), pytest.raises(ValueError, match="not a transient"):
        await fn()
    assert calls["n"] == 1


# ─── Exhaustion ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reraises_after_max_attempts():
    calls = {"n": 0}

    @with_retry(max_attempts=3, backoff_base_sec=0.01)
    async def fn() -> str:
        calls["n"] += 1
        raise _http_error(429)

    with patch("asyncio.sleep"), pytest.raises(httpx.HTTPStatusError):
        await fn()
    assert calls["n"] == 3  # first try + 2 retries


# ─── Retry-After header ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_honors_retry_after_header():
    """When the server sends Retry-After: 7 we sleep ~7s, not the
    exponential schedule."""
    calls = {"n": 0}
    sleeps: list[float] = []

    @with_retry(max_attempts=3, backoff_base_sec=2.0, jitter=False)
    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429, retry_after="7")
        return "ok"

    async def _capture(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("asyncio.sleep", side_effect=_capture):
        assert await fn() == "ok"
    # The single sleep before the retry was the server's 7s, not 2s.
    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_caps_retry_after_at_backoff_cap():
    """A misbehaving server returning Retry-After: 9999 doesn't stall
    the pipeline — capped at backoff_cap_sec."""
    sleeps: list[float] = []

    @with_retry(max_attempts=2, backoff_base_sec=1.0, backoff_cap_sec=30.0)
    async def fn() -> str:
        raise _http_error(429, retry_after="9999")

    async def _capture(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("asyncio.sleep", side_effect=_capture), pytest.raises(httpx.HTTPStatusError):
        await fn()
    # Cap kicks in.
    assert sleeps == [30.0]


# ─── Exponential backoff math ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_exponential_backoff_doubles_per_attempt():
    sleeps: list[float] = []

    @with_retry(max_attempts=4, backoff_base_sec=2.0, jitter=False)
    async def fn() -> str:
        raise _http_error(500)

    async def _capture(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("asyncio.sleep", side_effect=_capture), pytest.raises(httpx.HTTPStatusError):
        await fn()
    # 3 retries before final raise: 2s, 4s, 8s (no jitter).
    assert sleeps == [2.0, 4.0, 8.0]
