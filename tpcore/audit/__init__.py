"""Structured platform audits (tpcore SoT for the cross-table layer)."""
from __future__ import annotations

from tpcore.audit.cross_table import (
    CROSS_TABLE_CHECKS,
    CrossTableCheck,
    CrossTableFinding,
    run_cross_table_audit,
)

__all__ = [
    "CROSS_TABLE_CHECKS",
    "CrossTableCheck",
    "CrossTableFinding",
    "run_cross_table_audit",
]
