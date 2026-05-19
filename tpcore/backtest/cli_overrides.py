"""Pure CLI-override extraction shared by every engine backtest (Lean P5.1, #5).

Each engine previously carried a byte-identical private ``_overrides_from_args``;
the only difference was the engine-local ``*_OVERRIDE_KEYS`` tuple (data, not
logic). That tuple is now passed in by the caller so this helper holds zero
engine knowledge. Each engine keeps its private ``_overrides_from_args`` as a
thin delegate, so no call site changes.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def overrides_from_args(
    args: argparse.Namespace,
    keys: Sequence[str],
) -> dict[str, object]:
    """Collect non-``None`` CLI overrides for ``keys`` from ``args``.

    For each key in ``keys``, read ``getattr(args, key, None)``; include it in
    the result only when the value is not ``None`` (a missing attribute or an
    explicit ``None`` is dropped; falsy-but-not-``None`` values are kept).

    Byte-equivalent to the engines' former private ``_overrides_from_args``.
    """
    out: dict[str, object] = {}
    for k in keys:
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    return out
