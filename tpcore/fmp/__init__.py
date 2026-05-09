"""FMP (Financial Modeling Prep) adapters.

Quarterly fundamentals via FMP's ``/stable/`` API surface. The legacy
``/api/v3`` endpoints were deprecated August 2025 and return 403 for
non-grandfathered subscriptions, so we don't reference them.
"""

from .fundamentals_adapter import FMPFundamentalsAdapter

__all__ = ["FMPFundamentalsAdapter"]
