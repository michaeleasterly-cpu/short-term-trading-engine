"""Shared exception types used across engines and tpcore services.

Today's residents:

* :class:`SizingError` — raised by an engine's execution-risk plug
  when no valid position size can be computed (e.g. non-positive
  entry price). Extracted from ``sigma.plugs.execution_risk`` and
  ``reversion.plugs.execution_risk`` on 2026-05-14 — both engines
  declared a byte-identical local copy.
"""
from __future__ import annotations


class SizingError(Exception):
    """Raised when no valid position size can be computed (e.g. price ≤ 0)."""


__all__ = ["SizingError"]
