from __future__ import annotations

from pathlib import Path

from tpcore.lab.models import LabResult

LAB_DIR = Path(__file__).resolve().parents[2] / "docs" / "lab"


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
- Credibility: {r.credibility_score}  (gate ≥ 60)
- Held metrics: {r.held_metrics}

## 2. Winning parameters vs current engine defaults
{diff}

## 3. Ranked alternatives
{alts}

## 4. Next step (SP3 — NOT applied by the Lab)
{_next_step(r)}

## 5. Credibility rubric
{r.credibility_rubric}
"""


def _next_step(r: LabResult) -> str:
    if r.recommended_exit == "none":
        return "- Verdict FAILED — iterate; nothing to graduate."
    if r.recommended_exit == "fold_existing":
        return (f"- Fold the §2 param diff into `{r.target_engine}` "
                f"(SP3 Engine Change Request → re-gate). Lab does not apply it.")
    return ("- Promote to a new engine via tpcore/templates/engine_template/ "
            "+ engine_readiness (SP3). Lab does not scaffold it.")


def dossier_path(r: LabResult) -> Path:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    day = r.generated_at.strftime("%Y-%m-%d")
    return LAB_DIR / f"{day}-{r.candidate}-{r.verdict}-seed{r.seed}.md"


def write_lab_dossier(r: LabResult) -> Path:
    p = dossier_path(r)
    p.write_text(render_lab_dossier(r))
    return p
