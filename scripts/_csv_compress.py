"""Shared gzip helper for the backfill load scripts.

Compresses a CSV in place after a successful upsert. The load scripts
also gain transparent .gz read support so re-runs work whether the CSV
was already compressed or not.
"""
from __future__ import annotations

import gzip
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import IO

logger = logging.getLogger("scripts._csv_compress")


def open_csv_text(path: Path) -> Callable[[], IO]:
    """Return a factory that opens ``path`` for text reading whether it's
    a plain ``.csv`` or a ``.csv.gz``. Returns a callable so callers can
    use ``with opener() as fh:`` without leaking file handles."""
    if path.suffix == ".gz":
        return lambda: gzip.open(path, "rt", encoding="utf-8")
    return lambda: path.open("r", encoding="utf-8")


def gzip_in_place(path: Path, compresslevel: int = 6) -> Path:
    """Compress ``path`` → ``path.gz`` and remove the original. Returns the
    new gz path. No-op (logs warning) if the input is already .gz."""
    if path.suffix == ".gz":
        logger.warning("gzip_in_place: %s already .gz, skipping", path)
        return path
    gz = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=compresslevel) as dst:
        shutil.copyfileobj(src, dst)
    original_size = path.stat().st_size
    path.unlink()
    new_size = gz.stat().st_size
    logger.info(
        "compressed %s: %d B -> %d B (%.1f%%)",
        path.name, original_size, new_size,
        100.0 * new_size / max(original_size, 1),
    )
    return gz
