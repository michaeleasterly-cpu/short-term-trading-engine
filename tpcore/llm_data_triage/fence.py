"""Deterministic LLM-PR fence (pure; the load-bearing safety boundary).

Two checks, both evaluated by code the LLM never runs:
- hard_denied_paths: the "body" — any diff here auto-fails/closes.
- provenance_violations: the "brain" — registry edits allowed ONLY as
  additive entries binding an ALREADY-PROVEN stage/params; never a new
  or widened mechanism, never an edit/removal of an existing spec.
The LLM's self-judgement gates nothing; these artifact properties do.
"""
from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatch

# The "body": exact-or-prefix denied; *providers.py via glob.
DENIED_PREFIXES: tuple[str, ...] = (
    "tpcore/risk/",
    "tpcore/order_management/",
    "platform/migrations/",
)
DENIED_EXACT: tuple[str, ...] = (
    "tpcore/risk/limits_profile.py",
    "scripts/run_data_operations.sh",
    "scripts/ops.py",
    "tpcore/quality/validation/capital_gate.py",  # DSR/credibility gate
)
DENIED_GLOBS: tuple[str, ...] = ("*/providers.py", "tpcore/providers.py")


def hard_denied_paths(
    paths: list[str],
    *,
    denied_exact: tuple[str, ...] | None = None,
    denied_prefixes: tuple[str, ...] | None = None,
    denied_globs: tuple[str, ...] | None = None,
) -> list[str]:
    """Lane-agnostic by parameter (FORK-A: one fence object, no clone).

    The denied path set is INJECTED data. Omitting every keyword-only
    arg reproduces today's data-lane behavior **byte-identically** (the
    module-level DENIED_* constants are the defaults) — proven by the
    UNCHANGED #187 fence suite still passing. The engine lane passes its
    own denied set (the engine deterministic-mechanism files + the
    shared protected paths) via these args; it is never hardcoded here.
    """
    de = DENIED_EXACT if denied_exact is None else denied_exact
    dp = DENIED_PREFIXES if denied_prefixes is None else denied_prefixes
    dg = DENIED_GLOBS if denied_globs is None else denied_globs
    out: list[str] = []
    for p in paths:
        if (p in de
                or any(p.startswith(d) for d in dp)
                or any(fnmatch(p, g) for g in dg)):
            out.append(p)
    return out


def _norm(spec: Mapping) -> tuple:
    """Order-independent identity of a spec's mechanism."""
    return (
        spec["stage"],
        frozenset((spec.get("params") or {}).items()),
        bool(spec.get("act")),
        int(spec.get("max_attempts", 0)),
    )


def provenance_violations(
    baseline: Mapping[str, Mapping],
    head: Mapping[str, Mapping],
    baseline_stages: set[str],
) -> list[str]:
    """Pure. baseline/head: {key: {stage, params, act, max_attempts}}
    normalised from HEAL_SPECS/REMEDIATION_SPECS. baseline_stages: the
    set of stages already shipped on main (non-LLM).

    Allowed: ONLY additive new keys whose (stage,params) already exists
    among baseline specs, act=True, max_attempts <= the baseline max for
    that mechanism. Anything else = violation."""
    v: list[str] = []

    # No existing spec may be removed or modified.
    for k, b in baseline.items():
        if k not in head:
            v.append(f"removed existing spec {k!r} (LLM may only ADD)")
        elif _norm(head[k]) != _norm(b):
            v.append(f"modified existing spec {k!r} (LLM may only ADD)")

    proven: set[tuple] = {
        (s["stage"], frozenset((s.get("params") or {}).items()))
        for s in baseline.values() if bool(s.get("act"))
    }
    max_for: dict[tuple, int] = {}
    for s in baseline.values():
        key = (s["stage"], frozenset((s.get("params") or {}).items()))
        max_for[key] = max(max_for.get(key, 0),
                           int(s.get("max_attempts", 0)))

    for k, h in head.items():
        if k in baseline:
            continue  # additive-only; edits handled above
        mech = (h["stage"], frozenset((h.get("params") or {}).items()))
        if not bool(h.get("act")):
            v.append(f"new spec {k!r} does not bind a repair "
                     f"(escalate-only -- not a conversion)")
            continue
        if h["stage"] not in baseline_stages:
            v.append(f"new spec {k!r}: stage {h['stage']!r} is a NEW "
                     f"mechanism (not shipped on main)")
            continue
        if mech not in proven:
            v.append(f"new spec {k!r}: (stage,params) is a NEW "
                     f"mechanism -- only already-proven bindings allowed")
            continue
        if int(h.get("max_attempts", 0)) > max_for.get(mech, 0):
            v.append(f"new spec {k!r}: max_attempts widens the proven "
                     f"bound")
    return v


__all__ = [
    "DENIED_EXACT",
    "DENIED_GLOBS",
    "DENIED_PREFIXES",
    "hard_denied_paths",
    "provenance_violations",
]
