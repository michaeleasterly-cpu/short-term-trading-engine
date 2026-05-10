"""Unified ingestion engine — persistent worker that runs the formerly
standalone Sunday/MON-FRI ingest crons against a single shared
schedule table (``platform.ingestion_jobs``).

The engine wakes every 60s, picks up due jobs, dispatches by
``job_name`` to a handler in ``handlers``, and writes the result back.
A failed handler updates ``last_status``/``last_error`` and the engine
keeps going — one bad job never takes the worker down.
"""
from __future__ import annotations

from tpcore.ingestion.engine import IngestionEngine, JobResult
from tpcore.ingestion.handlers import HANDLERS, HandlerFn

__all__ = ["IngestionEngine", "JobResult", "HANDLERS", "HandlerFn"]
