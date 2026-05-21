"""Entrypoint so ``python -m tpcore.backtest`` runs the overfitting CLI."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
