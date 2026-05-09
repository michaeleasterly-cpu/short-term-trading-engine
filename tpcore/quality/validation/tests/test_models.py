"""Invariants for the validation-suite models."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tpcore.quality.validation.models import CheckResult, FailureDetail, SuiteResult


def _failure(ticker: str = "AAPL", reason: str = "ratio_off") -> FailureDetail:
    return FailureDetail(ticker=ticker, reason=reason, expected="1.0", observed="0.25")


def _check(
    *,
    name: str = "splits",
    passed: bool = True,
    total: int = 10,
    failed: int = 0,
    duration_ms: int = 12,
    failures: list[FailureDetail] | None = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        passed=passed,
        total=total,
        failed=failed,
        duration_ms=duration_ms,
        failures=failures or [],
    )


# ────────────────────────────────────────────────────────────────────────────
# FailureDetail
# ────────────────────────────────────────────────────────────────────────────


def test_failure_detail_accepts_required_fields() -> None:
    fd = FailureDetail(ticker="AAPL", reason="ratio_off", expected="1.0", observed="0.25")
    assert fd.ticker == "AAPL"
    assert fd.reason == "ratio_off"
    assert fd.expected == "1.0"
    assert fd.observed == "0.25"


def test_failure_detail_allows_optional_fields_none() -> None:
    fd = FailureDetail(ticker="AAPL", reason="missing", expected=None, observed=None)
    assert fd.expected is None
    assert fd.observed is None


def test_failure_detail_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        FailureDetail(  # type: ignore[call-arg]
            ticker="AAPL", reason="ratio_off", expected=None, observed=None, color="blue"
        )


# ────────────────────────────────────────────────────────────────────────────
# CheckResult
# ────────────────────────────────────────────────────────────────────────────


def test_check_result_valid_construction() -> None:
    cr = _check(name="delistings", passed=True, total=12, failed=0)
    assert cr.name == "delistings"
    assert cr.passed is True
    assert cr.failures == []


def test_check_result_with_failures() -> None:
    cr = _check(passed=False, total=10, failed=1, failures=[_failure()])
    assert cr.failed == 1
    assert cr.failures[0].ticker == "AAPL"


def test_check_result_is_frozen() -> None:
    cr = _check()
    with pytest.raises(ValidationError):
        cr.passed = False  # type: ignore[misc]


def test_check_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CheckResult(  # type: ignore[call-arg]
            name="x", passed=True, total=0, failed=0, duration_ms=0, failures=[], extra=1
        )


# ────────────────────────────────────────────────────────────────────────────
# SuiteResult
# ────────────────────────────────────────────────────────────────────────────


def test_suite_result_valid_construction() -> None:
    started = datetime(2026, 5, 10, 6, 0, tzinfo=UTC)
    finished = datetime(2026, 5, 10, 6, 0, 5, tzinfo=UTC)
    sr = SuiteResult(
        run_id=uuid4(),
        started_at=started,
        finished_at=finished,
        checks=[_check(), _check(name="constituent"), _check(name="delistings")],
        passed=True,
    )
    assert sr.passed is True
    assert len(sr.checks) == 3
    assert sr.started_at == started


def test_suite_result_is_frozen() -> None:
    sr = SuiteResult(
        run_id=uuid4(),
        started_at=datetime(2026, 5, 10, tzinfo=UTC),
        finished_at=datetime(2026, 5, 10, tzinfo=UTC),
        checks=[_check()],
        passed=True,
    )
    with pytest.raises(ValidationError):
        sr.passed = False  # type: ignore[misc]
