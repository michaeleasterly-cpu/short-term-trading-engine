"""Engine-lane shared helpers (Wave-3 deterministic self-heal).

Sibling of :mod:`tpcore.outage` (HTTP-API retries) and
:mod:`tpcore.data.batched_fetchers` (Supabase statement-timeout retries)
— scoped specifically to the engine-plug call sites that need
transient-DB-error recovery WITHOUT pulling in those neighbors'
domain-specific semantics. See the row-by-row design in
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` (Wave-3: E1 / E2 / E3 / E9).
"""
from __future__ import annotations

from tpcore.engine.transient_retry import (
    DEFAULT_BACKOFF_BASE_SEC,
    DEFAULT_BACKOFF_CAP_SEC,
    DEFAULT_MAX_ATTEMPTS,
    fetch_with_transient_retry,
    is_transient_db_error,
)

__all__ = [
    "DEFAULT_BACKOFF_BASE_SEC",
    "DEFAULT_BACKOFF_CAP_SEC",
    "DEFAULT_MAX_ATTEMPTS",
    "fetch_with_transient_retry",
    "is_transient_db_error",
]
