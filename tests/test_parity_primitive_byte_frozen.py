"""F0 (2026-06-01) — parity primitive byte-freeze sentinel.

The data-parity primitive (``tpcore/parity/data_parity.py``) is the
verdict layer the cutover gate relies on. F0 deliberately does NOT
modify the primitive — it adds a CALLER (the EVALUATE stage) and a
freshness gate. This sentinel pins the SHA-256 of:

  * ``compare_provider_parity`` function source (the verdict function)
  * ``_tol`` helper source (resolves per-feed-class tolerances)
  * the whole module source (catches additions / deletions)

If F0 ever needs to evolve the primitive — e.g. add a NEW feed-class
tolerance — this sentinel will fail to force a DELIBERATE update
(the same precedent as the P0 / P1 / P2 byte-freeze sentinels on
``fundamentals_quarterly_completeness``).

A whitespace-only commit would also flip the SHA — that's
intentional: the bytes-on-disk hash is conservative and surfaces ANY
change. There is no SemanticEqualityHash precedent in this repo, so
source-byte SHA is the chosen mechanism (consistent with
``tests/test_p0_no_validator_semantics_change.py`` and
``tests/test_p2b_lifecycle_evidence_wiring.py``).
"""
from __future__ import annotations

import hashlib
import inspect


def _sha256(src: str) -> str:
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def test_compare_provider_parity_byte_frozen() -> None:
    from tpcore.parity import data_parity

    actual = _sha256(inspect.getsource(data_parity.compare_provider_parity))
    expected = (
        "0892b9f1af6b0f62f0509bbd078756e9bfea5efa94824612612c8da18147db66"
    )
    assert actual == expected, (
        "tpcore.parity.data_parity.compare_provider_parity source SHA "
        f"changed:\n  expected: {expected}\n  actual:   {actual}\n\n"
        "F0 deliberately does NOT modify the primitive — it adds a "
        "caller. If this is an intentional primitive change (new "
        "tolerance, new FeedClass, etc.), update the pinned hash AND "
        "review the cutover_agent's freshness gate for compatibility."
    )


def test_tol_helper_byte_frozen() -> None:
    from tpcore.parity import data_parity

    actual = _sha256(inspect.getsource(data_parity._tol))
    expected = (
        "a932047077e373b7bd3cec0a973f67e35411b14cb9a67e8a70f65d31d155f50b"
    )
    assert actual == expected, (
        "tpcore.parity.data_parity._tol source SHA changed:\n"
        f"  expected: {expected}\n  actual:   {actual}\n\n"
        "If this is intentional (override semantics change), update "
        "the pinned hash."
    )


def test_data_parity_module_byte_frozen() -> None:
    from tpcore.parity import data_parity

    actual = _sha256(inspect.getsource(data_parity))
    expected = (
        "d7d6427b015d708d73b0d66634ef97e1dfbc8e5356c9b1e430f756512f02fbef"
    )
    assert actual == expected, (
        "tpcore/parity/data_parity.py module source SHA changed:\n"
        f"  expected: {expected}\n  actual:   {actual}\n\n"
        "The primitive is supposed to be UNCHANGED in this F0 PR. "
        "If you intentionally touched it, the operator's hard rule "
        "says revert; if a primitive change is required after all, "
        "open a separate spec PR per docs/DEV_PIPELINE_STANDARD.md §1."
    )
