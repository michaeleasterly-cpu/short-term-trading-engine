"""``tpcore.backtest.universe.DEFAULT_BACKTEST_UNIVERSE`` — single SoT
for the 50-name backtest universe.

Migrated 2026-05-21 from the dual-hardcoded ``DEFAULT_UNIVERSE`` tuple
that previously lived in both
``scripts/backfill_backtest_universe.py:DEFAULT_UNIVERSE`` and
``ops/cron_corporate_actions.py:UNIVERSE`` (kept in sync via a comment
— a known accretion path). The constant moved to
``tpcore/backtest/universe.py`` so consumers on the installed package
path can import it directly; ``ops/cron_corporate_actions.py`` now
imports it. The legacy script was deleted as part of the orphan-
scripts zero-allowlist sweep.

Asserts (1) the constant lives at the canonical import path,
(2) ``ops/cron_corporate_actions.py``'s ``UNIVERSE`` is the SAME
object (proves the de-duplication landed and the comment-only sync
discipline is retired), (3) the universe has the expected 50 names
including the broad-market trio + a stable bag of liquid US equities,
(4) the legacy ``scripts/backfill_backtest_universe.py`` is gone +
the orphan allowlist entry was removed.
"""
from __future__ import annotations

from pathlib import Path


def test_default_backtest_universe_is_a_50_name_tuple() -> None:
    """Pin the size + shape so a refactor cannot silently drop a name
    (e.g., a stray comma-elision that would orphan a sector slice)."""
    from tpcore.backtest.universe import DEFAULT_BACKTEST_UNIVERSE
    assert isinstance(DEFAULT_BACKTEST_UNIVERSE, tuple)
    assert len(DEFAULT_BACKTEST_UNIVERSE) == 50
    # Every entry is an uppercase ticker token.
    for sym in DEFAULT_BACKTEST_UNIVERSE:
        assert isinstance(sym, str)
        assert sym == sym.upper()
        assert sym.replace(".", "").replace("-", "").isalpha(), sym
    # The broad-market trio is the deliberate anchor — keep it pinned.
    assert {"SPY", "QQQ", "IWM"}.issubset(set(DEFAULT_BACKTEST_UNIVERSE))


def test_cron_corporate_actions_imports_canonical_constant() -> None:
    """``ops/cron_corporate_actions.py::UNIVERSE`` must be the SAME
    constant — the de-duplication is the entire point of the
    migration. ``is`` (identity), not just value equality, locks in
    that the cron module imports from ``tpcore.backtest.universe``
    rather than re-defining a parallel tuple."""
    from ops import cron_corporate_actions
    from tpcore.backtest.universe import DEFAULT_BACKTEST_UNIVERSE

    assert cron_corporate_actions.UNIVERSE is DEFAULT_BACKTEST_UNIVERSE, (
        "ops/cron_corporate_actions.py::UNIVERSE must be the SAME "
        "object as tpcore.backtest.universe.DEFAULT_BACKTEST_UNIVERSE "
        "— the post-migration single-SoT contract"
    )


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: ``scripts/backfill_backtest_universe.py`` is gone +
    the allowlist entry was removed."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/backfill_backtest_universe.py"
    assert not script.exists(), (
        "scripts/backfill_backtest_universe.py must be deleted — the "
        "constant moved to tpcore.backtest.universe and the all-active "
        "sweep at ops.py --stage daily_bars covers the 50 names."
    )
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"backfill_backtest_universe"' not in text
