"""Pydantic v2 models for the Data Validation Suite.

All three are frozen — once a check or suite run completes, its result is
immutable. The orchestrator (`suite.py`) is the only producer; readers
(`capital_gate.py`, the CLI) consume but never mutate.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FailureDetail(BaseModel):
    """One fixture entry that failed a check."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    reason: str
    expected: str | None = None
    observed: str | None = None


class CheckResult(BaseModel):
    """Outcome of a single check (delistings | constituent | splits)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    total: int
    failed: int
    duration_ms: int
    failures: list[FailureDetail]


class SuiteResult(BaseModel):
    """Aggregate outcome of one suite run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    started_at: datetime
    finished_at: datetime
    checks: list[CheckResult]
    passed: bool


__all__ = ["CheckResult", "FailureDetail", "SuiteResult"]
