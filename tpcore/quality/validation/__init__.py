"""Data Validation Suite — gates engine graduation from paper to live.

See `docs/superpowers/specs/2026-05-10-data-validation-suite-design.md`.
"""
from __future__ import annotations

from .models import CheckResult, FailureDetail, SuiteResult

__all__ = ["CheckResult", "FailureDetail", "SuiteResult"]
