"""The single RemediationSpec registry — one entry per cross-table
check. Clockwork: ``test_auditheal`` asserts the key set is EXACTLY
the CROSS_TABLE_CHECKS key set, so adding a cross-table check fails
the build until a deliberate remediate/escalate decision is recorded.

Launch scope: ONLY the two tradier_options_chains checks that
``cross_ref_cleanup`` proves-safe to delete are remediable.
Everything else escalates honestly — no proven-safe auto-delete
exists (deleting an earnings/fundamentals row for a transiently
prices-missing ticker would destroy correct data).
"""
from __future__ import annotations

from tpcore.audit.cross_table import CROSS_TABLE_CHECKS

from .spec import RemediationSpec

_CROSS_REF: dict[str, str] = {}  # cross_ref_cleanup takes no params

_NO_SAFE_DELETE = (
    "no proven-safe canonical remediation — deleting these rows is not "
    "additive-safe (a ticker transiently absent from prices_daily, a "
    "real integrity defect to investigate, etc.); escalate to the "
    "operator. Honest, not a rollout gap."
)

_REMEDIABLE = {
    "tradier_options_chains/expiration_in_past",
    "tradier_options_chains/orphan_no_prices",
}


def _spec_for_check(table: str, check_name: str) -> RemediationSpec:
    key = f"{table}/{check_name}"
    if key in _REMEDIABLE:
        return RemediationSpec(
            check_key=key, table=table, check_name=check_name,
            remediable=True, stage="cross_ref_cleanup",
            params=dict(_CROSS_REF), max_attempts=2,
        )
    return RemediationSpec(
        check_key=key, table=table, check_name=check_name,
        remediable=False, escalate_reason=_NO_SAFE_DELETE,
    )


REMEDIATION_SPECS: dict[str, RemediationSpec] = {
    c.key: _spec_for_check(c.table, c.check_name)
    for c in CROSS_TABLE_CHECKS
}


def spec_for(check_key: str) -> RemediationSpec | None:
    """RemediationSpec for a ``<table>/<check_name>`` key, or None if
    unknown (treated as escalate — never silently ignored)."""
    return REMEDIATION_SPECS.get(check_key)


def registry_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) vs the CROSS_TABLE_CHECKS key set."""
    known = {c.key for c in CROSS_TABLE_CHECKS}
    have = set(REMEDIATION_SPECS)
    return known - have, have - known


__all__ = ["REMEDIATION_SPECS", "registry_drift", "spec_for"]
