"""Consolidated Defect-Register panel classifier (#254, Phase DR3).

Pure, Streamlit-free, DB-free — mirrors dashboard_components/escalation.py.
The console is a READ-ONLY RENDERER of the existing SoT: this takes the
already-composed ``ops.defect_register.consolidated_defects`` rows and
only classifies+formats them. It recomputes NO predicate and re-derives
NO defect state — the register already joined both Ladders verbatim +
the review-found anti-join open-set; this just colors what it returns.
There is NO write surface (spec §5 OUT — no dashboard write button).

Duck-typed on the frozen DR1/DR2 ``DefectRow`` field contract
(``defect_ref, origin, lane, summary, state, opened_at, fix_ref``) so
this module — and its unit tests — never import the ``ops``-package
module (the documented ``scripts/ops.py`` ↔ ``ops/`` package-shadow
hazard stays entirely out of the pure layer).

Return contract (identical to escalation.py / health.py roll-up
classifiers): ``(color, summary, detail)`` — color in
{"green","amber","red"}, detail = list[(label, color, text)] — so the
existing ``_render_health_row`` + expander loop renders it unchanged.
"""
from __future__ import annotations

from typing import Any

_Detail = list[tuple[str, str, str]]
_Result = tuple[str, str, _Detail]


def classify_defect_register(rows: list[Any]) -> _Result:
    """Classify the consolidated defect rows for the audit panel.

    Each ``row`` is an ``ops.defect_register.DefectRow`` (consumed
    verbatim — its ``state`` is the register's, derived from the
    Ladders' / the review anti-join predicate; never re-derived here).
    An OPEN defect is red (an open defect is an open operator-action
    gate); a non-open (fixed / structural-parked) one is amber; none is
    green.
    """
    if not rows:
        return "green", "No open defects (escalation + review-found)", []

    detail: _Detail = []
    n_open = 0
    for r in rows:
        state = str(getattr(r, "state", "open"))
        is_open = state == "open"
        if is_open:
            n_open += 1
        fix = getattr(r, "fix_ref", None)
        label = f"[{getattr(r, 'lane', '?')}] {getattr(r, 'defect_ref', '?')}"
        text = (
            f"{state} — origin={getattr(r, 'origin', '?')} "
            f":: {getattr(r, 'summary', '')}"
            + (f" (fix={fix})" if fix else "")
        )
        detail.append((label, "red" if is_open else "amber", text))

    if n_open:
        return (
            "red",
            f"{n_open} open defect(s) of {len(rows)} — "
            f"`python -m ops.defect_register list`",
            detail,
        )
    return (
        "amber",
        f"{len(rows)} defect(s), all resolved/parked (none open)",
        detail,
    )


__all__ = ["classify_defect_register"]
