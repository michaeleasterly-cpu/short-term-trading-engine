"""SP-G — pre-emission ledger budget gate.

The rate-limit fence (spec §4.1): the second rate limit *on top of* the
SP-A cumulative-DSR fence. SP-A makes more trials harder to graduate;
this gate further bounds how many configurations the LLM may even
propose, so the operator's review budget is not overwhelmed and the
multiple-testing pollution per target stays visibly bounded.

Engine-FREE: reads ``tpcore.lab.ledger.cumulative_n_trials`` and emits
an ``LedgerBudgetExhausted`` if ``cumulative + expected_trials > quota``.
The Anthropic SDK is NEVER invoked on the rejected path (no ledger
spend, no network — spec §3.4 step 1; spec §8.1).

The default ``EMISSION_QUOTA_PER_TARGET = 20`` per target is the
operator-confirmed Q2 default (per the §10 operator decisions). It is
intentionally low: an operator review session can plausibly absorb 20
draft PRs across the roster; SP-A's monotone-harder DSR fence does the
heavy lifting beyond that.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tpcore.lab.ledger import cumulative_n_trials

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


# Q2 (operator-confirmed default, spec §10): 20 per target. NEVER hand-
# raise this in source; the operator-only knob is an env var that the
# agent reads (the spec §4.1 invariant: not LLM-decided). The constant
# is the FROZEN default; an env override is the explicit operator
# action.
EMISSION_QUOTA_PER_TARGET: int = 20


class LedgerBudgetExhausted(Exception):
    """Raised pre-emission when ``cumulative + expected_trials > quota``.

    The agent maps this to a clear operator message and returns WITHOUT
    invoking the Anthropic SDK (spec §3.4 step 1). The exception carries
    the budget triple for the message: cumulative, expected, quota.
    """

    def __init__(
        self, *, target: str, cumulative: int, expected: int, quota: int
    ) -> None:
        self.target = target
        self.cumulative = cumulative
        self.expected = expected
        self.quota = quota
        super().__init__(
            f"SP-G emission budget exhausted for target {target!r}: "
            f"cumulative={cumulative} + expected={expected} > quota={quota}. "
            f"This is the rate-limit fence (spec §4.1); operator may "
            f"raise the quota via env override (NOT via the LLM)."
        )


async def check_budget(
    pool: asyncpg.Pool,
    *,
    target: str,
    expected_trials: int,
    quota: int = EMISSION_QUOTA_PER_TARGET,
    now: datetime | None = None,
) -> int:
    """Read the SP-A cumulative trial count and verify ``cumulative +
    expected_trials <= quota``. Returns the cumulative count on success;
    raises ``LedgerBudgetExhausted`` on failure.

    ``now`` is the strict ``<`` boundary handed to
    ``cumulative_n_trials`` (the SP-A read shape); the default
    ``datetime.now(UTC)`` is overridable for deterministic tests.
    """
    if expected_trials < 1:
        raise ValueError(
            f"expected_trials must be >=1; got {expected_trials!r} "
            f"(the EmittedSpec validator should have caught this earlier)"
        )
    if quota < 0:
        raise ValueError(f"quota must be >=0; got {quota!r}")
    boundary = now or datetime.now(UTC)
    cumulative = await cumulative_n_trials(pool, target, boundary)
    if cumulative + expected_trials > quota:
        raise LedgerBudgetExhausted(
            target=target,
            cumulative=cumulative,
            expected=expected_trials,
            quota=quota,
        )
    return cumulative


__all__ = [
    "EMISSION_QUOTA_PER_TARGET",
    "LedgerBudgetExhausted",
    "check_budget",
]
