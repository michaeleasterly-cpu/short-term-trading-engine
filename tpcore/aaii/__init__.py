"""AAII Sentiment Survey adapter (no auth, anti-bot-fragile).

``https://www.aaii.com/files/surveys/sentiment.xls`` — a single legacy
OLE2 ``.xls`` workbook with the full weekly bull/neutral/bearish
history since 1987. A plain request 403s (anti-bot); a browser-shaped
request returns the real file (verified 2026-05-16). 403 is permanent
per the canonical ``with_retry`` → ``DataProviderOutage``.
"""
from __future__ import annotations

from .adapter import AAIIAdapter, AAIISentimentRecord, parse_sentiment_workbook

__all__ = ["AAIIAdapter", "AAIISentimentRecord", "parse_sentiment_workbook"]
