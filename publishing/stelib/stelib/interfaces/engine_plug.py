"""Base engine-plug ABC.

A "plug" is a self-contained capability inside an engine (e.g. signal
generator, lifecycle analyzer, position sizer). Every plug declares the
engine it belongs to, validates its dependencies up front, and exposes a
healthcheck for the operator dashboard.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseEnginePlug(ABC):
    """Abstract base for any engine plug."""

    #: Name of the engine this plug belongs to (e.g. ``"sigma"``).
    engine_name: str = ""

    @abstractmethod
    def validate_dependencies(self) -> bool:
        """Verify all required upstream services / data providers are reachable.

        Should raise or return ``False`` if anything required is missing.
        """
        raise NotImplementedError

    @abstractmethod
    def healthcheck(self) -> dict:
        """Return a structured healthcheck payload.

        Expected keys (at minimum)::

            {
                "engine": str,
                "plug": str,
                "ok": bool,
                "details": dict,
            }
        """
        raise NotImplementedError
