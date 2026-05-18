"""SP3 evidence loader (H-S3-9 / D1). The planner re-derives every gate
number from the frozen LabResult JSON sidecar — NEVER the rendered
markdown (re-scraping prose rendered by a template is fragile for the
load-bearing automated-MODIFY gate). extra=forbid ⇒ a tampered/extra
field is a hard reject.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from tpcore.lab.models import LabResult


class EvidenceError(RuntimeError):
    """The cited Lab dossier's evidence sidecar is missing, unreadable,
    or fails LabResult model-validation (tampered/extra field)."""


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


__all__ = ["EvidenceError", "load_labresult_sidecar"]
