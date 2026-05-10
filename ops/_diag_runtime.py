"""Throwaway runtime diagnostic — dumps Python version, sys.path, env vars,
and what's actually installed under /usr/local/lib/python3.*/site-packages.

Used to debug ingestion-engine's ModuleNotFoundError on first deploy; will
be deleted once the runtime config is sorted out.
"""
from __future__ import annotations

import glob
import os
import sys


def main() -> int:
    print("=" * 60)
    print(f"PY_VERSION    {sys.version}")
    print(f"EXECUTABLE    {sys.executable}")
    print(f"PREFIX        {sys.prefix}")
    print(f"PYTHONPATH    {os.environ.get('PYTHONPATH')!r}")
    print(f"SYS.PATH:")
    for p in sys.path:
        print(f"  {p}")
    print()
    print(f"GLOB site-packages dirs found:")
    for d in sorted(glob.glob("/usr/local/lib/python3.*/site-packages")):
        print(f"  {d}")
        # Sample a few packages we care about
        for pkg in ("structlog", "asyncpg", "croniter", "sigma"):
            hit = glob.glob(f"{d}/{pkg}*")
            if hit:
                print(f"    {pkg:12s} -> {hit[0]}")
    print()
    print(f"GLOB any python install at all:")
    for d in sorted(glob.glob("/usr/local/lib/python*/")):
        print(f"  {d}")
    print(f"GLOB /usr/lib/python* (system python):")
    for d in sorted(glob.glob("/usr/lib/python*/")):
        print(f"  {d}")
    print("=" * 60)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
