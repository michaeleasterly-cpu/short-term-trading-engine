"""Database-backed application logging.

``DBLogHandler`` writes lightweight, structured audit events to
``platform.application_log`` with self-managed retention. Every scheduler
run emits a STARTUP/.../SHUTDOWN timeline tagged with a per-run UUID so
the timeline of a single invocation is queryable. The handler swallows
DB errors — operational logging must never bring down a trading run.
"""
from __future__ import annotations

from tpcore.logging.db_handler import DBLogHandler

__all__ = ["DBLogHandler"]
