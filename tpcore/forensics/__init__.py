"""Forensics service — monitors AARs and emits triggers.

Per MASTER_PLAN §5: detects drawdown periods, loss clusters, and outlier
losses across per-engine AAR histories. Writes triggers to
``platform.data_quality_log`` (``kind='forensics_trigger'``; Plan 2
consolidation, via ``tpcore.forensics.dql_store``) for operator review (a
Sprint Dossier follow-up is operator-driven, not automated).
"""

from tpcore.forensics.service import (
    DRAWDOWN_DAYS_THRESHOLD,
    DRAWDOWN_PCT_THRESHOLD,
    LOSS_CLUSTER_K,
    MIN_AARS_FOR_OUTLIER,
    OUTLIER_SIGMA,
    ForensicsService,
    TriggerKind,
    detect_drawdown_period,
    detect_loss_cluster,
    detect_outlier_losses,
)

__all__ = [
    "DRAWDOWN_DAYS_THRESHOLD",
    "DRAWDOWN_PCT_THRESHOLD",
    "ForensicsService",
    "LOSS_CLUSTER_K",
    "MIN_AARS_FOR_OUTLIER",
    "OUTLIER_SIGMA",
    "TriggerKind",
    "detect_drawdown_period",
    "detect_loss_cluster",
    "detect_outlier_losses",
]
