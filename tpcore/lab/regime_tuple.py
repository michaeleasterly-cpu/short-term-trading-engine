"""SHA12 regime-tuple identity — substrate primitive.

Extracted 2026-05-25 from the retired `tpcore.lab.llm_finder.models`
when the LLM lab/finder/monitor stack was removed (operator directive
"it is out"). The hashed tuple identity is the only piece of that
retired stack that has an active consumer: `reversion/regime_filter.py`
uses it to pin the per-session regime classification to the same
12-char hash the (now-retired) finder would have produced for the
same axes — i.e. it preserves byte-identical regime IDs for Lab
probes already registered against finder-era regime tuples.

Pure: stdlib only. No engine imports, no DB access, no I/O.
"""
from __future__ import annotations

import hashlib


def compute_regime_tuple_id(
    vol: str,
    trend: str,
    macro: str,
    sentiment: str,
) -> str:
    """SHA12 hash of the 4 hash-eligible regime axes.

    Mirrors the retired `tpcore.lab.llm_finder.models._compute_regime_tuple_id`
    byte-for-byte so regime tuples registered before the 2026-05-25
    retirement remain stable. Axis names are sorted before hashing so
    argument order doesn't change the result.
    """
    axes_sorted = sorted(
        [f"v:{vol}", f"t:{trend}", f"m:{macro}", f"s:{sentiment}"]
    )
    digest = hashlib.sha256("|".join(axes_sorted).encode("utf-8")).hexdigest()
    return digest[:12]


__all__ = ["compute_regime_tuple_id"]
