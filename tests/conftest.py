"""Tests in this directory exercise top-level modules (``dashboard.py``,
etc.) that don't live inside a package. Add the repo root to ``sys.path``
so ``import dashboard`` works under ``pytest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
