"""Drift forcing-test — engine source must declare every ``platform.<feed-table>``
reference in its ``EngineProfile.data_dependencies``.

Invariant
---------
For every PAPER/LIVE engine in ``tpcore.engine_profile._PROFILE``, the set of
``platform.<table>`` tables that the engine's source tree actually references
must be a SUBSET of ``EngineProfile.data_dependencies`` — modulo the
platform-overlay allowlist (tables that are engine outputs / control-plane /
state, not validation-gated external feeds).

Motivation
----------
The existing ``test_dispatchable_engine_declares_data_dependencies`` clockwork
(``tpcore/tests/test_engine_profile.py``) asserts non-empty
``data_dependencies`` on every PAPER/LIVE engine, but NOT a source-match.
That gap let two real-world drifts land between PR #171 (15:07 UTC+8,
2026-05-20) and the audit at 23:50 UTC+8 the same day:

* PR #178 (catalyst): ``_fetch_earnings_events`` added a live
  ``SELECT ... FROM platform.earnings_events`` to ``catalyst/backtest.py:292``
  for the ``event_confirmation_mode="positive_beat_30d"`` overlay, but
  ``data_dependencies`` was not updated.
* PR #180 (momentum): ``_load_earnings_beats`` added the same read at
  ``momentum/backtest.py:485`` for the vol-managed Lab candidate's
  earnings-beat overlay, populated unconditionally onto
  ``MomentumWindowContext.earnings_by_ticker``, but ``data_dependencies``
  was not updated.

This test reds the build on any future PR that re-introduces the same
drift — a source line reading ``platform.<table>`` that the engine has
not declared as a dependency.

Audit reference: ``docs/superpowers/audits/2026-05-20-engine-data-
dependencies-accuracy.md``.

Platform-overlay allowlist
--------------------------
The allowlist below is the **canonical** set of platform tables that do
NOT count as engine-dependencies. Source: PR #171's audit narrative —
these are platform STATE (engine outputs / control-plane logs /
computed-state caches), not validation-gated external feeds. They are
intentionally excluded from ``data_dependencies`` declarations:

* ``data_quality_log``     — write-only logging bus
* ``application_log``      — write-only event bus
* ``open_orders``          — engine output (order state)
* ``risk_state``           — RiskGovernor control-plane state
* ``aar_events``           — engine output (after-action records)
* ``universe_candidates``  — computed cache populated by
  ``tpcore.universe.prescreener``
* ``allocations``          — allocator output

The allowlist is PINNED here (not imported from any other module) so that a
future "convenient" expansion of the allowlist has to come through this
file and a code review — same enforcement shape as the audit doc.
"""
from __future__ import annotations

import re
from pathlib import Path

from tpcore.engine_profile import (
    _PROFILE,
    LifecycleState,
    engine_data_dependencies,
)

# ─── Canonical platform-overlay allowlist (PR #171 docstring) ─────────────
# Tables that are platform STATE (engine outputs / control-plane / computed
# caches), NOT validation-gated external feeds. Discovered references to
# these tables are stripped before the subset check. Mirrored verbatim
# from the audit doc § "Drift-prevention follow-up — source-match audit
# clockwork".
PLATFORM_OVERLAY_ALLOWLIST: frozenset[str] = frozenset({
    "data_quality_log",
    "application_log",
    "open_orders",
    "risk_state",
    "aar_events",
    "universe_candidates",
    "allocations",
})

# ─── Regex for platform.<table> ───────────────────────────────────────────
# `\b` word boundary on both ends — matches the table identifier in
# `FROM platform.prices_daily WHERE …` (the `\b` after `daily` is the
# space). `[a-z_]+` covers every snake_case table name in the schema.
# Greedy by default, which is correct: for a hypothetical
# `platform.fundamentals_quarterly_completeness`, the whole identifier
# would be captured as a single token — a non-existent table name, but
# safely strippable via the allowlist or surfaced as a fail with a
# precise error message. No such cases exist on current main (verified
# by inspection 2026-05-21).
_PLATFORM_TABLE_RE = re.compile(r"\bplatform\.([a-z_]+)\b")

# ─── Engine source-file enumeration ───────────────────────────────────────
# Per the audit methodology: each engine's source tree is
# ``<engine>/{plugs,scheduler.py,backtest.py,order_manager.py}``. Be
# permissive — include any top-level ``.py`` under the engine package
# AND every ``.py`` under ``<engine>/plugs/``. Skip ``__pycache__`` and
# ``tests/``.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _engine_source_files(engine: str) -> list[Path]:
    """Return every ``.py`` source file in the engine package that the
    drift check should scan. Excludes ``__pycache__``, ``tests``, and
    any file whose path segment contains ``tests``."""
    pkg = _REPO_ROOT / engine
    if not pkg.is_dir():
        return []
    files: list[Path] = []
    # Top-level .py files (backtest.py, scheduler.py, order_manager.py,
    # models.py, lab_*.py, diagnose_*.py, etc.) — permissive per the
    # task spec ("be permissive, don't artificially limit").
    for p in pkg.iterdir():
        if p.is_file() and p.suffix == ".py":
            files.append(p)
    # Every .py under plugs/.
    plugs = pkg / "plugs"
    if plugs.is_dir():
        for p in plugs.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    return sorted(files)


def _discover_platform_tables(engine: str) -> dict[str, list[str]]:
    """Walk the engine's source tree and return ``{table_name: [files...]}``
    for every ``platform.<table>`` reference (overlay tables included —
    callers filter). The file list is for error-message provenance."""
    discovered: dict[str, list[str]] = {}
    for src in _engine_source_files(engine):
        text = src.read_text()
        for m in _PLATFORM_TABLE_RE.finditer(text):
            table = m.group(1)
            rel = str(src.relative_to(_REPO_ROOT))
            if rel not in discovered.setdefault(table, []):
                discovered[table].append(rel)
    return discovered


# ─── Tests ────────────────────────────────────────────────────────────────


def _dispatchable_engines() -> list[str]:
    """The engines this drift test gates: PAPER + LIVE in ``_PROFILE``,
    excluding ``allocator`` (it lives under ``tpcore/allocator/`` not
    ``<engine>/`` and is gated by the separate accessor pathway)."""
    return sorted(
        name
        for name, p in _PROFILE.items()
        if p.lifecycle_state in (LifecycleState.PAPER, LifecycleState.LIVE)
        and name != "allocator"
    )


def test_every_engine_source_declares_its_platform_table_reads() -> None:
    """For every PAPER/LIVE engine, ``platform.<table>`` references in the
    engine's source tree (minus the platform-overlay allowlist) must be a
    subset of ``EngineProfile.data_dependencies``. Reds the build on any
    PR that adds a new ``platform.<feed-table>`` read without updating
    the engine's declared deps — the exact drift PR #178 + PR #180 hit."""
    violations: list[str] = []
    for engine in _dispatchable_engines():
        discovered = _discover_platform_tables(engine)
        discovered_tables = frozenset(discovered.keys())
        # Strip the platform-overlay allowlist before the subset check.
        feed_tables = discovered_tables - PLATFORM_OVERLAY_ALLOWLIST
        declared = engine_data_dependencies(engine)
        missing = feed_tables - declared
        if missing:
            for table in sorted(missing):
                files = ", ".join(discovered[table])
                violations.append(
                    f"{engine}: source reads `platform.{table}` "
                    f"(in {files}) but `EngineProfile.data_dependencies` "
                    f"= {sorted(declared)} does not declare it. "
                    f"Either add `{table}` to _PROFILE[{engine!r}]."
                    f"data_dependencies via ECR-MODIFY, or move the "
                    f"read into the {sorted(PLATFORM_OVERLAY_ALLOWLIST)} "
                    f"platform-overlay allowlist if it is platform STATE "
                    f"(not a validation-gated feed)."
                )
    assert not violations, (
        "engine-data-dependencies drift detected — every "
        "`platform.<feed-table>` reference in an engine's source tree "
        "must be declared in EngineProfile.data_dependencies "
        "(modulo the platform-overlay allowlist). See "
        "docs/superpowers/audits/2026-05-20-engine-data-dependencies-"
        "accuracy.md.\n\n" + "\n".join(violations)
    )


def test_drift_extractor_is_not_vacuously_passing() -> None:
    """Non-vacuity sanity proof: the regex + source-walk MUST discover
    AT LEAST ONE expected ``platform.<table>`` reference per dispatchable
    engine. If discovery returns empty for some engine, the main test
    is silently passing — red the build here so a future refactor that
    moves the platform reads (e.g. SQL into a shared helper) doesn't
    silently void the drift gate.

    Expected pins (audit-evidence-derived, 2026-05-20):

    * reversion → `prices_daily` (backtest.py SQL reads + scheduler hand-off)
    * vector    → `prices_daily`
    * momentum  → `prices_daily`
    * sentinel  → `prices_daily`
    * canary    → `prices_daily`
    * catalyst  → `prices_daily`

    All six engines unambiguously read `prices_daily` directly — it is
    the substrate. If discovery for any of them does NOT contain it, the
    regex has regressed or the engine moved its reads to a shared helper
    that the source-walk doesn't cover — both red-the-build cases."""
    for engine in _dispatchable_engines():
        discovered = _discover_platform_tables(engine)
        assert discovered, (
            f"{engine}: drift extractor discovered ZERO `platform.<table>` "
            f"references — the regex or the source-walk has regressed, "
            f"OR the engine moved its platform reads to a shared helper "
            f"outside `<engine>/{{plugs,*.py}}`. Either fix the "
            f"extractor or update _engine_source_files to cover the new "
            f"location (DO NOT delete this test — it is the non-vacuity "
            f"proof that the main drift test isn't silently passing)."
        )
        assert "prices_daily" in discovered, (
            f"{engine}: drift extractor did NOT find `platform.prices_daily` "
            f"— every dispatchable engine reads it as the substrate. "
            f"Discovered tables: {sorted(discovered.keys())}. Fix the "
            f"extractor / source-walk so it covers the engine's actual "
            f"platform reads."
        )


def test_platform_overlay_allowlist_is_pinned_constant() -> None:
    """Pin the allowlist's shape so a casual expansion has to come through
    this file (visible code review). The audit doc says: ``data_quality_log
    / application_log / open_orders / risk_state / aar_events /
    universe_candidates / allocations``. Match it byte-for-byte."""
    assert PLATFORM_OVERLAY_ALLOWLIST == frozenset({
        "data_quality_log",
        "application_log",
        "open_orders",
        "risk_state",
        "aar_events",
        "universe_candidates",
        "allocations",
    }), (
        "PLATFORM_OVERLAY_ALLOWLIST has drifted from the canonical PR #171 "
        "platform-state set. Any change must be evidence-backed (audit "
        "doc + a separate test pin) — the allowlist is the only way a "
        "platform.<table> reference escapes the source-match check."
    )
