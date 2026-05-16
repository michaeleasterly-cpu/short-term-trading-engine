"""ApeWisdom social-sentiment adapter (no auth, paginated).

Scans Reddit communities; refreshes every ~2h. No API key. Pull all
pages of /filter/all-stocks and filter to the engine universe locally
(no per-ticker API filter available).
"""
from __future__ import annotations

from .adapter import ApeWisdomAdapter, SocialSentimentRecord

__all__ = ["ApeWisdomAdapter", "SocialSentimentRecord"]
