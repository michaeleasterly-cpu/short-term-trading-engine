"""Canary — DELIBERATELY NON-GRADUATING (spec §4b).

The canary is a permanent paper heartbeat, NOT an alpha engine. It
must never be promoted to live capital. We enforce this BY
CONSTRUCTION: this module intentionally omits the credibility-rubric
write call, so no rubric row is ever written for `canary`, so
`graduation_ready("canary")` is always False, so
`CanaryCapitalGate.assert_can_graduate` always raises. This is the
single, documented deviation from the engine-build compliance
shortlist (CLAUDE.md) — it is intentional, not an omission.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def run_backtest(*args, **kwargs) -> dict:
    """No-op by design — see module docstring. Returns a structured
    marker so callers/operators see the intentional non-graduation."""
    logger.info("canary.backtest.noop_by_design")
    return {"graduating": False,
            "reason": "canary is a permanent paper heartbeat — "
                      "non-graduating by construction (spec §4b)"}
