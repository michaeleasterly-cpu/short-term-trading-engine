"""Compress every uncompressed ``.csv`` under the backfill dirs.

Safe to run any time — only operates on plain ``.csv`` files (skips
already-compressed ``.csv.gz``). The load scripts now compress
automatically after a successful upsert; this one-shot is for cleaning
up CSVs that were loaded BEFORE the auto-compress was wired up.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._csv_compress import gzip_in_place  # noqa: E402

logger = logging.getLogger("scripts.compress_backfill_csvs")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = [
    REPO_ROOT / "data" / "alpaca_backfill",
    REPO_ROOT / "data" / "fmp_backfill",
    REPO_ROOT / "data" / "corp_actions_backfill",
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dirs", nargs="*", default=[str(d) for d in DEFAULT_DIRS])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    total_in = total_out = 0
    n_files = 0
    for d_str in args.dirs:
        d = Path(d_str)
        if not d.exists():
            continue
        for csv in sorted(d.glob("*.csv")):
            size = csv.stat().st_size
            total_in += size
            if args.dry_run:
                logger.info("would compress %s (%d B)", csv.name, size)
                continue
            gz = gzip_in_place(csv)
            total_out += gz.stat().st_size
            n_files += 1
    if args.dry_run:
        logger.info("dry-run: would process %d B total across all dirs", total_in)
    else:
        ratio = (total_out / total_in * 100) if total_in else 0.0
        logger.info(
            "compressed %d file(s): %d B -> %d B (%.1f%%)",
            n_files, total_in, total_out, ratio,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
