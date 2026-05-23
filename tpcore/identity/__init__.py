"""Cross-vendor security identity — TKR-14 smart-key generator + adapter layer.

Per v2.2 spec (`docs/superpowers/specs/2026-05-23-referential-integrity-design-v2.2.md`):
- `tkr14` — pure functions to mint / validate / decode TKR-14 smart-keys.
- `dispatcher` (Phase P4 deliverable) — `ticker_to_classification_id` /
  `classification_id_to_ticker` adapter layer for wire-boundary translation.

Standards anchored: ISO 7064 Mod-97-10 (check digit, LEI precedent),
ISO 3166-1 alpha-2 (country segment), Crockford base32 (issuer-hash charset).
"""
from __future__ import annotations

from tpcore.identity.tkr14 import (
    TKR14_REGEX,
    TKR14Segments,
    crockford_base32,
    decode,
    iso_7064_mod_97_10,
    mint,
    validate,
)

__all__ = [
    "TKR14_REGEX",
    "TKR14Segments",
    "crockford_base32",
    "decode",
    "iso_7064_mod_97_10",
    "mint",
    "validate",
]
