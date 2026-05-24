"""Ingestion package — adapter handlers + CSV archive backends.

The standalone IngestionEngine dispatcher (drove the legacy
`platform.ingestion_jobs` schedule table) was retired 2026-05-24
after the deterministic-cascade architecture + `application_log`
event bus replaced it. Source-of-truth for stage runs is now
`scripts/ops.py --stage <name>`; cron-style scheduling is operator
launchd / Railway cron, not a DB-tick loop.
"""
from __future__ import annotations

from tpcore.ingestion.handlers import HANDLERS, HandlerFn

__all__ = ["HANDLERS", "HandlerFn"]
