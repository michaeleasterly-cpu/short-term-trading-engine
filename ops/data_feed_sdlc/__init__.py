"""Data Feed Change Request (DFCR) — the planner that mutates the
data-feed roster SoT.

Symmetric to ``ops.engine_sdlc`` (the ECR planner). The DFCR is the
single structured touchpoint for ADD (ONBOARD) / REMOVE (RETIRE) of a
data feed; CUTOVER (provider swap) / EVALUATE / self-heal are
automated per ``.claude/rules/data-feed-roster.md`` (this MVP ships
ADD only — sufficient to unblock the P0 autonomous-self-heal
ProviderBinding additions: fundamentals_quarterly, earnings_events,
sec_insider_transactions, corporate_actions; REMOVE/CUTOVER/EVALUATE
are deferred to a later increment).

The planner reads a filled DFCR block (per
``docs/superpowers/checklists/data_feed_change_request.md``), validates
it against the live ``tpcore.providers._BINDINGS`` +
``tpcore.feeds.FEED_PROFILES`` snapshot, prepares an exact 3-way diff
(ProviderBinding + FeedProfile + audit), and hands the operator the
binary ``APPROVE? (y/n)`` on a planner-validated change.

NO hand-edits to ``tpcore/providers.py`` or ``tpcore/feeds/profile.py``
— the ``.claude/hooks/gate-ecr-dfcr-edits.sh`` hook blocks them.
The planner sets ``CLAUDE_DFCR_RUN=1`` internally for its own atomic
writes (read the hook for the exact env-var override contract).
"""
from __future__ import annotations

from ops.data_feed_sdlc.dfcr import DataFeedChangeRequest, DFCRAction, parse_dfcr
from ops.data_feed_sdlc.planner import (
    ApprovalClass,
    TransitionPlan,
    apply,
    classify,
    validate,
)

__all__ = [
    "ApprovalClass",
    "DFCRAction",
    "DataFeedChangeRequest",
    "TransitionPlan",
    "apply",
    "classify",
    "parse_dfcr",
    "validate",
]
