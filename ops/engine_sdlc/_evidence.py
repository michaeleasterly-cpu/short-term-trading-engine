"""SP3 evidence loader (H-S3-9 / D1). The planner re-derives every gate
number from the frozen LabResult JSON sidecar — NEVER the rendered
markdown (re-scraping prose rendered by a template is fragile for the
load-bearing automated-MODIFY gate). extra=forbid ⇒ a tampered/extra
field is a hard reject.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from tpcore.lab.models import LabResult


class EvidenceError(RuntimeError):
    """The cited Lab dossier's evidence sidecar is missing, unreadable,
    or fails LabResult model-validation (tampered/extra field)."""


# The real dossier filename SoT (ops/lab/dossier.py::dossier_path):
#   f"{day}-{candidate}-{verdict}-seed{seed}.md"
# where day == generated_at.strftime("%Y-%m-%d") (3 hyphen tokens),
# candidate ∈ [A-Za-z0-9_-]+ (may itself contain hyphens), verdict ∈
# {SURVIVED, FAILED}, seed an int. The parse anchors on the
# unambiguous ends: the leading YYYY-MM-DD, the trailing `seed<int>`,
# and the verdict token immediately before it — candidate is whatever
# lies between (so a hyphenated candidate is reconstructed faithfully).
_DOSSIER_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-(?P<candidate>.+)-(?P<verdict>SURVIVED|FAILED)"
    r"-seed(?P<seed>\d+)$")


def parse_dossier_name(md_path: str | Path) -> tuple[str, str, int]:
    """Parse the ECR-cited dossier filename into (candidate, verdict,
    seed) using the frozen ``ops/lab/dossier.py`` format. Raises
    ``EvidenceError`` on any name that does not match the SoT shape (a
    cited path that is not a real Lab dossier name is itself a reject)."""
    stem = Path(md_path).stem
    m = _DOSSIER_NAME_RE.match(stem)
    if m is None:
        raise EvidenceError(
            f"cited dossier path {Path(md_path).name!r} does not match "
            f"the Lab dossier filename format "
            f"{{day}}-{{candidate}}-{{verdict}}-seed{{seed}}.md")
    return m["candidate"], m["verdict"], int(m["seed"])


def assert_identity_fresh(lr: LabResult, md_path: str | Path) -> None:
    """Spec §5.4 / H-S3-6(b): the sidecar must be identity-fresh — its
    ``candidate``/``seed`` (and, when present, ``verdict``) must match
    the ECR's CITED dossier path. A perfectly-valid sidecar from a
    DIFFERENT Lab run sitting at the cited path is a hard reject (a
    forged/stale ECR cannot launder a real-but-other dossier). Raises
    ``EvidenceError`` on ANY token mismatch; mutates nothing.
    """
    cand, verdict, seed = parse_dossier_name(md_path)
    if lr.candidate != cand:
        raise EvidenceError(
            f"sidecar candidate {lr.candidate!r} != cited dossier path "
            f"candidate {cand!r} — the sidecar is from a DIFFERENT Lab "
            f"run (identity-stale, H-S3-6b hard reject)")
    if lr.seed != seed:
        raise EvidenceError(
            f"sidecar seed {lr.seed} != cited dossier path seed {seed} "
            f"— the sidecar is from a DIFFERENT Lab run (identity-stale, "
            f"H-S3-6b hard reject)")
    if lr.verdict != verdict:
        raise EvidenceError(
            f"sidecar verdict {lr.verdict!r} != cited dossier path "
            f"verdict {verdict!r} — the sidecar does not match the cited "
            f"dossier (identity-stale, H-S3-6b hard reject)")


def load_labresult_sidecar(md_path: str | Path) -> LabResult:
    md = Path(md_path)
    sidecar = md.with_suffix(".json")
    if not sidecar.is_file():
        raise EvidenceError(
            f"no LabResult sidecar for dossier {md.name!r} "
            f"(expected {sidecar.name}); re-run the Lab to regenerate it")
    try:
        return LabResult.model_validate_json(sidecar.read_text())
    except ValidationError as exc:
        raise EvidenceError(
            f"LabResult sidecar {sidecar.name} failed validation "
            f"(tampered / extra field / extra=forbid): {exc}") from exc


__all__ = [
    "EvidenceError",
    "assert_identity_fresh",
    "load_labresult_sidecar",
    "parse_dossier_name",
]
