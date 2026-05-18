"""Escalation / data-integrity audit classifiers (#189).

Pure, Streamlit-free, DB-free — mirrors dashboard_components/health.py.
The console is a READ-ONLY RENDERER of the existing SoT: these take
already-fetched rows / the already-policy-annotated undispositioned
list and only classify+format them. They recompute NO predicate (that
reimplementation is exactly how the console drifted).

Return contract (identical to health.py roll-up classifiers):
``(color, summary, detail)`` — color in {"green","amber","red"},
detail = list[(label, color, text)] — so the existing
``_render_health_row`` + expander loop render them unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_Detail = list[tuple[str, str, str]]
_Result = tuple[str, str, _Detail]


def _age(ts: Any) -> str:
    if not isinstance(ts, datetime):
        return "age?"
    delta = datetime.now(UTC) - ts
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def classify_source_holds(rows: list[dict[str, Any]]) -> _Result:
    """Open Data-Supervisor per-source holds (DATA_SOURCE_HELD with no
    later DATA_SOURCE_CLEARED). Any open hold is red — a held source is
    a stood-down feed the operator must drive to disposition."""
    if not rows:
        return "green", "No open source holds", []
    detail: _Detail = [
        (str(r.get("source", "?")), "red",
         f"held {_age(r.get('held_at'))} — {r.get('reason', '')}")
        for r in rows
    ]
    return "red", f"{len(rows)} source(s) held (Data Supervisor)", detail


def classify_undispositioned(items: list[str]) -> _Result:
    """Undispositioned data-lane escalations. ``items`` is
    ``build_weekly_digest(...).undispositioned`` VERBATIM (already
    policy-annotated). Non-empty = red (rung-3: an undispositioned
    escalation is an open operator-action gate)."""
    if not items:
        return "green", "No undispositioned escalations", []
    detail: _Detail = [(f"#{i + 1}", "red", s)
                        for i, s in enumerate(items)]
    return (
        "red",
        f"{len(items)} undispositioned — `python -m ops.weekly_digest "
        f"disposition <ref> <converted|structural|removed>`",
        detail,
    )


def _red(stale: Any, confidence: Any) -> bool:
    return bool(stale) or (confidence is not None and confidence < 1.0)


def classify_cross_table_audit(rows: list[dict[str, Any]]) -> _Result:
    """Latest cross_table_audit.* per source (the auditheal-persisted
    layer classify_validation is blind to). Red on stale OR
    confidence<1.0 — the platform's standard red predicate."""
    if not rows:
        return "amber", "No cross-table audit rows recorded", []
    detail: _Detail = []
    worst = "green"
    nred = 0
    for r in rows:
        src = str(r.get("source", "?")).replace("cross_table_audit.", "")
        if _red(r.get("stale"), r.get("confidence")):
            detail.append((src, "red", "latest: FAILED"))
            worst = "red"
            nred += 1
        else:
            detail.append((src, "green", "latest: clean"))
    summary = (
        f"All {len(rows)} cross-table check(s) clean" if worst == "green"
        else f"{nred}/{len(rows)} cross-table check(s) FAILED — see detail"
    )
    return worst, summary, detail


def classify_recent_escalations(rows: list[dict[str, Any]]) -> _Result:
    """Trailing-window DATA_REPAIR_ESCALATED + AdapterContractDrift.
    Each row carries a ``resolved`` bool (terminal/disposition seen).
    Any OPEN (unresolved) escalation in the window is red."""
    if not rows:
        return "green", "No escalations in the last 7 days", []
    detail: _Detail = []
    n_open = 0
    for r in rows:
        when = r.get("recorded_at")
        when_s = when.strftime("%Y-%m-%d") if isinstance(
            when, datetime) else "?"
        if r.get("resolved"):
            detail.append(
                (f"{r.get('etype', '?')} ref={r.get('ref', '?')}",
                 "amber", f"{when_s} resolved — {r.get('message', '')}"))
        else:
            n_open += 1
            detail.append(
                (f"{r.get('etype', '?')} ref={r.get('ref', '?')}",
                 "red", f"{when_s} OPEN — {r.get('message', '')}"))
    if n_open:
        return "red", f"{n_open} OPEN escalation(s) (last 7d)", detail
    return "amber", f"{len(rows)} escalation(s) resolved (last 7d)", detail


__all__ = [
    "classify_cross_table_audit",
    "classify_recent_escalations",
    "classify_source_holds",
    "classify_undispositioned",
]
