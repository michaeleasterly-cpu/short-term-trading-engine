"""``RemediationSpec`` — the declarative per-cross-table-check
remediation contract. Mirrors tpcore.selfheal.spec.HealSpec.

The orchestrator holds zero check-specific logic; all knowledge lives
here as data: whether the violation class has a proven canonical
remediation (``remediable``), which ``ops.py --stage`` performs it,
and (if not) the honest ``escalate_reason``. ``remediable=False`` is
honest, not lazy — most cross-table reds (other-table orphans,
integrity) have NO proven-safe auto-delete.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RemediationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # "<table>/<check_name>" — matches CrossTableCheck.key and the
    # data_quality_log source cross_table_audit.<table>.<check_name>.
    check_key: str
    table: str
    check_name: str
    remediable: bool
    # Canonical ops.py stage performing the bounded remediation.
    stage: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    max_attempts: int = 3
    # Required when remediable is False (honest escalation).
    escalate_reason: str = ""

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        if self.remediable:
            if not self.stage:
                raise ValueError(
                    f"RemediationSpec[{self.check_key}]: remediable=True "
                    "requires a stage"
                )
        elif not self.escalate_reason:
            raise ValueError(
                f"RemediationSpec[{self.check_key}]: remediable=False "
                "requires escalate_reason (honest escalation, not a gap)"
            )


__all__ = ["RemediationSpec"]
