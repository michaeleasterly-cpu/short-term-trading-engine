"""Universe selection helpers — populate ``platform.universe_candidates``.

The prescreener answers "what's in scope for engine X today" — engines do
their own scoring/ranking at runtime against this roster. V1 ships only
the ``momentum`` populator; other engines keep their hardcoded universes
until they need this.
"""

from __future__ import annotations

from tpcore.universe.prescreener import prescreen_momentum

__all__ = ["prescreen_momentum"]
