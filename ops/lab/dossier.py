from __future__ import annotations

import re
from pathlib import Path

from tpcore.backtest.credibility import CredibilityScore
from tpcore.lab.models import LabResult

LAB_DIR = Path(__file__).resolve().parents[2] / "docs" / "lab"


def _fmt_metrics(d: dict) -> str:
    rows = ["| metric | value |", "| --- | --- |"]
    for k in sorted(d):
        rows.append(f"| {k} | {d[k]} |")
    return "\n".join(rows)


def _fmt_rubric(r: CredibilityScore) -> str:
    dumped = r.model_dump()
    rows = ["| check | value |", "| --- | --- |"]
    for k in sorted(dumped):
        rows.append(f"| {k} | {dumped[k]} |")
    return "\n".join(rows)


def _objective_line(r: LabResult) -> str:
    """SP-D §2.4 — for a non-SHARPE objective, an operator-facing line in
    '## 1. Verdict' naming the objective. The parenthetical is MANDATORY
    copy (the 'the gate is sacred' doctrine, SP-C §6): the metric is
    ranking-only and provably does NOT affect the gate. For SHARPE the
    dossier is byte-identical to pre-SP-D (no line emitted)."""
    from tpcore.lab.target import LabPrimaryMetric

    if r.primary_metric == LabPrimaryMetric.SHARPE:
        return ""
    return (f"\n- **Primary objective:** {r.primary_metric.value} "
            f"(ranking metric — does NOT affect the gate)")


def _objective_block(r: LabResult) -> str:
    """SP-D §2.4 — a '## 2a' block keyed off the declared metric. SHARPE:
    empty (byte-identical pre-SP-D). MAXDD_REDUCTION: max_drawdown is the
    headline, Sharpe demoted. Pure fn of held_metrics + the declared
    metric — no new data, no query, no gate read."""
    from tpcore.lab.target import LabPrimaryMetric

    if r.primary_metric == LabPrimaryMetric.SHARPE:
        return ""
    if r.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION:
        hm = r.held_metrics
        return (
            "\n## 2a. Objective-appropriate summary"
            f"\n- **Headline (max_drawdown):** {hm.get('max_drawdown')}"
            f"\n- Sharpe (secondary): {hm.get('sharpe')}"
            f"\n- Profit factor (secondary): {hm.get('profit_factor')}\n"
        )
    # Reserved objectives have no dossier block until their scorer ships
    # (SP-E). Naming them is harmless (the run could never have ranked —
    # the §4.3 pre-spend fence rejects before any LabResult is built).
    return (
        f"\n## 2a. Objective-appropriate summary"
        f"\n- (objective {r.primary_metric.value} has no dossier block "
        f"yet — reserved, SP-E)\n"
    )


def render_lab_dossier(r: LabResult) -> str:
    diff = "\n".join(
        f"- `{d.name}`: {d.current} → **{d.winning}**" for d in r.param_diff
    ) or "- (no param diff)"
    alts = "\n".join(f"- {a}" for a in r.ranked_alternatives) or "- (none)"
    return f"""# Lab Dossier — {r.candidate} → {r.target_engine} [{r.verdict}]

**Intent:** {r.intent}  **Recommended exit:** {r.recommended_exit}
**Generated:** {r.generated_at.isoformat()}  **Seed:** {r.seed}  **Trials:** {r.n_trials}

## 1. Verdict
- DSR: {r.dsr:.4f}  (gate ≥ 0.95)
- Credibility: {r.credibility_score}  (gate ≥ 60){_objective_line(r)}
- Held metrics:

{_fmt_metrics(r.held_metrics)}
{_objective_block(r)}
## 2. Winning parameters vs current engine defaults
{diff}

## 3. Ranked alternatives
{alts}

## 4. Next step (SP3 — NOT applied by the Lab)
{_next_step(r)}

## 5. Credibility rubric
{_fmt_rubric(r.credibility_rubric)}
"""


def _next_step(r: LabResult) -> str:
    if r.recommended_exit == "none":
        return "- Verdict FAILED — iterate; nothing to graduate."
    if r.recommended_exit == "fold_existing":
        return (
            f"- Fold the §2 param diff into `{r.target_engine}` "
            f"(SP3 Engine Change Request → re-gate). Lab does not apply it.\n"
            "- Readiness gate: this candidate must have passed "
            "`docs/superpowers/checklists/lab_candidate_readiness.md` "
            "BEFORE the run (the Lab-lane sibling of engine_readiness; "
            "a candidate cannot bypass it the way an engine ADD cannot "
            "bypass engine_readiness).")
    return (
        "- Promote to a new engine via tpcore/templates/engine_template/ "
        "+ engine_readiness (SP3). Lab does not scaffold it.\n"
        "- Readiness gate: this candidate must have passed "
        "`docs/superpowers/checklists/lab_candidate_readiness.md` BEFORE "
        "the run (the pre-run Lab-lane sibling of engine_readiness).")


def dossier_path(r: LabResult) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", r.candidate):
        raise ValueError(
            f"unsafe Lab candidate name for a filesystem path: {r.candidate!r}"
        )
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    day = r.generated_at.strftime("%Y-%m-%d")
    return LAB_DIR / f"{day}-{r.candidate}-{r.verdict}-seed{r.seed}.md"


def write_lab_dossier(r: LabResult) -> Path:
    p = dossier_path(r)
    p.write_text(render_lab_dossier(r))
    # H-S3-9 (D1 fix): the automated-MODIFY gate (SP3) re-derives every
    # number from a machine-readable frozen artifact, NEVER scraped
    # rendered markdown. model_dump_json is deterministic field order
    # (frozen pydantic). The .md above is byte-unchanged.
    p.with_suffix(".json").write_text(r.model_dump_json())
    return p


__all__ = ["LAB_DIR", "dossier_path", "render_lab_dossier", "write_lab_dossier"]
