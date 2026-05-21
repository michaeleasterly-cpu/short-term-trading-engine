"""Reference-bundle loader — Task #25 §5.

Reads ``docs/lab_emitter_references/<name>.md`` files; builds
``ReferenceExcerpt`` instances. The 3 mandatory-always-include bundles
(``dsr_ntrials_discipline``, ``regime_aware_trading``,
``market_structure_primer``) are unioned into every load call regardless
of ``names``.

Fail-loud on:
- a named bundle that does not exist on disk
- an empty file (zero bytes)
- a stub file (contains ``[operator-pending content]`` marker) — the
  spec §7.4-§7.5 stub sentinel used by the spec-rewrite subagent

The loader is pure (no DB, no network). All file reads happen at call
time; nothing cached at module import.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tpcore.lab.llm_finder import MANDATORY_REFERENCE_BUNDLES

_DEFAULT_REFERENCE_ROOT: Final[Path] = Path(__file__).resolve().parents[3] / "docs" / "lab_emitter_references"
_STUB_MARKER: Final[str] = "[operator-pending content]"


@dataclass(frozen=True, slots=True)
class ReferenceExcerpt:
    """One reference bundle file loaded into the finder context."""

    name: str
    path: Path
    content: str
    byte_count: int
    is_mandatory: bool


class ReferenceNotFoundError(FileNotFoundError):
    """Named bundle does not exist on disk (or is outside the reference root)."""


class ReferenceStubError(ValueError):
    """Named bundle contains the stub sentinel marker — operator-pending content."""


class ReferenceEmptyError(ValueError):
    """Named bundle file is empty (zero bytes)."""


def load_reference_bundles(
    names: tuple[str, ...] = (),
    *,
    root: Path | None = None,
) -> tuple[ReferenceExcerpt, ...]:
    """Load named bundles + the 3 mandatory-always-include bundles.

    Args:
        names: tuple of bundle names (without ``.md`` extension). The 3
            mandatory bundles are unioned in regardless of this argument.
        root: override the reference root (test seam; defaults to
            ``docs/lab_emitter_references/``).

    Returns:
        Tuple of ``ReferenceExcerpt`` in deterministic order: mandatory
        bundles first (alphabetical by name), then the requested
        non-mandatory bundles in the order given.

    Raises:
        ReferenceNotFoundError: a requested bundle file does not exist.
        ReferenceStubError: a bundle contains the stub-sentinel marker.
        ReferenceEmptyError: a bundle file is zero bytes.
    """
    root = root if root is not None else _DEFAULT_REFERENCE_ROOT

    mandatory_seen: set[str] = set()
    excerpts: list[ReferenceExcerpt] = []

    for name in sorted(MANDATORY_REFERENCE_BUNDLES):
        excerpts.append(_load_one(name, root, is_mandatory=True))
        mandatory_seen.add(name)

    for name in names:
        if name in mandatory_seen:
            continue
        excerpts.append(_load_one(name, root, is_mandatory=False))

    return tuple(excerpts)


def _load_one(name: str, root: Path, *, is_mandatory: bool) -> ReferenceExcerpt:
    """Read one bundle; raise the loud failure if anything's off."""
    path = root / f"{name}.md"
    if not path.is_file():
        raise ReferenceNotFoundError(
            f"reference bundle '{name}' not found at {path}"
        )
    content = path.read_text(encoding="utf-8")
    byte_count = len(content.encode("utf-8"))
    if byte_count == 0:
        raise ReferenceEmptyError(
            f"reference bundle '{name}' at {path} is empty (0 bytes)"
        )
    if _STUB_MARKER in content:
        raise ReferenceStubError(
            f"reference bundle '{name}' at {path} contains stub sentinel "
            f"'{_STUB_MARKER}' — operator-pending content not authored yet"
        )
    return ReferenceExcerpt(
        name=name,
        path=path,
        content=content,
        byte_count=byte_count,
        is_mandatory=is_mandatory,
    )


def available_bundles(root: Path | None = None) -> tuple[str, ...]:
    """Enumerate all ``.md`` files in the reference root (alphabetical)."""
    root = root if root is not None else _DEFAULT_REFERENCE_ROOT
    if not root.is_dir():
        return ()
    return tuple(sorted(p.stem for p in root.glob("*.md")))


__all__ = [
    "ReferenceEmptyError",
    "ReferenceExcerpt",
    "ReferenceNotFoundError",
    "ReferenceStubError",
    "available_bundles",
    "load_reference_bundles",
]
