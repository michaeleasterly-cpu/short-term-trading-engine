"""``ops.py --stage compute_fundamental_ratios`` — set-based pb/de UPDATE.

Migrated 2026-05-20 from ``scripts/compute_fundamental_ratios.py``
(orphan-scripts audit; catalog at
``docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md``).

Asserts the stage (1) issues the canonical set-based UPDATE SQL,
(2) toggles the incremental-vs-force WHERE clause off ``config.force``,
(3) returns the expected detail-dict shape, (4) is wired into the
existing ops.py ``--update`` cadence + ``OPS_UPDATE_STAGES`` register,
and (5) does NOT introduce a new daemon — rides the existing
``--update`` flow chained right after ``fundamentals_refresh`` so a
fresh FMP pull's rows get ratios in the same cycle.

No real DB / Alpaca / FMP touched. The pool fakes the two queries the
stage issues (the UPDATE-with-RETURNING and the populated-counts roll-
up). pytest-xdist ops-shadow group per the package-shadow rule.
"""
from __future__ import annotations

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    """Captures SQL + serves canned responses for the two queries the
    stage issues."""

    def __init__(
        self, updated_returning: list[dict[str, str]] | None = None,
        populated_row: dict[str, int] | None = None,
    ) -> None:
        self.updated_returning = updated_returning or []
        self.populated_row = populated_row or {
            "pb_n": 0, "de_n": 0, "total": 0,
        }
        self.fetch_sqls: list[str] = []
        self.fetchrow_sqls: list[str] = []

    async def fetch(self, sql: str, *args):
        self.fetch_sqls.append(sql)
        # The stage's UPDATE-with-RETURNING is the only fetch() call.
        return self.updated_returning

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_sqls.append(sql)
        return self.populated_row


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


async def test_default_incremental_only_writes_null_rows() -> None:
    """Default (no ``force``) ⇒ WHERE clause restricts to rows where
    ``pb IS NULL OR de IS NULL`` — re-runs are cheap no-ops on rows that
    already carry both ratios."""
    conn = _Conn(
        updated_returning=[{"ticker": "AAPL"}, {"ticker": "MSFT"}],
        populated_row={"pb_n": 152_909, "de_n": 152_909, "total": 178_610},
    )
    result = await ops._stage_compute_fundamental_ratios(  # noqa: SLF001
        _Pool(conn), {},
    )
    assert result == {
        "rows_updated": 2, "pb_populated": 152_909,
        "de_populated": 152_909, "total_rows": 178_610, "force": False,
    }
    update_sql = _norm(conn.fetch_sqls[0])
    assert "AND (pb IS NULL OR de IS NULL)" in update_sql, (
        "incremental WHERE clause must restrict to NULL rows by default"
    )


async def test_force_true_overwrites_existing_rows() -> None:
    """``config.force == 'true'`` ⇒ WHERE clause omits the NULL guard —
    mirrors the prior script's ``--force`` flag, used for input-filter
    semantics changes (e.g. the 2026-05-14 degenerate-row tightening
    required a full re-write)."""
    conn = _Conn(updated_returning=[{"ticker": "X"}])
    result = await ops._stage_compute_fundamental_ratios(  # noqa: SLF001
        _Pool(conn), {"force": "true"},
    )
    assert result["force"] is True
    update_sql = _norm(conn.fetch_sqls[0])
    assert "AND (pb IS NULL OR de IS NULL)" not in update_sql, (
        "force=true MUST drop the NULL guard so already-populated rows "
        "get rewritten"
    )


async def test_degenerate_row_filter_present_in_sql() -> None:
    """The tightened input filter (ta>0 AND tl>=0) is the safety net
    against FMP's degenerate inverted-accounting rows (ta=0, tl<0)
    that would otherwise produce de=-1.0. Pin it explicitly so a
    future SQL refactor can't silently drop it."""
    conn = _Conn()
    await ops._stage_compute_fundamental_ratios(_Pool(conn), {})  # noqa: SLF001
    update_sql = _norm(conn.fetch_sqls[0])
    assert "total_assets > 0" in update_sql
    assert "total_liabilities >= 0" in update_sql
    assert "shares_outstanding > 0" in update_sql
    assert "(total_assets - total_liabilities) > 0" in update_sql


async def test_pit_price_join_uses_distinct_on_most_recent_close() -> None:
    """The price-join must pick the MOST RECENT close on-or-before
    ``filing_date`` — the DISTINCT ON pattern. A naive INNER JOIN would
    produce N rows per filing × prior session and wreck the UPDATE."""
    conn = _Conn()
    await ops._stage_compute_fundamental_ratios(_Pool(conn), {})  # noqa: SLF001
    update_sql = _norm(conn.fetch_sqls[0])
    assert "DISTINCT ON (t.ticker, t.filing_date)" in update_sql
    assert "pd.date <= t.filing_date" in update_sql
    assert "ORDER BY t.ticker, t.filing_date, pd.date DESC" in update_sql


async def test_returns_zero_rows_when_no_targets() -> None:
    """Empty UPDATE result + zero populated counts → the detail-dict
    shape stays consistent (no KeyError, no NoneType arithmetic)."""
    conn = _Conn(
        updated_returning=[],
        populated_row={"pb_n": 0, "de_n": 0, "total": 0},
    )
    result = await ops._stage_compute_fundamental_ratios(  # noqa: SLF001
        _Pool(conn), None,
    )
    assert result == {
        "rows_updated": 0, "pb_populated": 0, "de_populated": 0,
        "total_rows": 0, "force": False,
    }


def test_stage_wired_into_existing_update_cadence_after_fundamentals_refresh() -> None:
    """Registration-pin: the stage rides ops.py ``--update`` and chains
    immediately after ``fundamentals_refresh`` so fresh FMP rows get
    ratios in the same cycle. Closes the manual operator step the
    orphan-script catalog flagged."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]  # noqa: SLF001
    assert "compute_fundamental_ratios" in spec_names
    assert "compute_fundamental_ratios" in ops.KNOWN_STAGES
    assert "compute_fundamental_ratios" in OPS_UPDATE_STAGES
    # Chain order: fundamentals_refresh must come immediately before
    # compute_fundamental_ratios (the UPDATE reads the rows the refresh
    # just wrote).
    fr = spec_names.index("fundamentals_refresh")
    cr = spec_names.index("compute_fundamental_ratios")
    assert cr == fr + 1, (
        f"compute_fundamental_ratios must chain RIGHT AFTER "
        f"fundamentals_refresh; got positions {fr} and {cr}"
    )
    # OPS_UPDATE_STAGES (the dashboard's stage register) reflects the
    # same chain.
    fr_dash = OPS_UPDATE_STAGES.index("fundamentals_refresh")
    cr_dash = OPS_UPDATE_STAGES.index("compute_fundamental_ratios")
    assert cr_dash == fr_dash + 1


def test_orphan_allowlist_entry_removed() -> None:
    """Sentinel: ``compute_fundamental_ratios`` MUST NOT appear in the
    no-orphan-scripts allowlist after the migration — the canonical
    code path is now the ops.py stage."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / (
        "scripts/tests/test_no_orphan_scripts.py"
    )
    text = src.read_text(encoding="utf-8")
    assert '"compute_fundamental_ratios"' not in text, (
        "the script's allowlist entry must be removed when the stage "
        "lands; leaving it would block a future genuine orphan from "
        "being flagged"
    )


def test_orphan_script_file_deleted() -> None:
    """Sentinel: the script file itself must be gone — the canonical
    path is the ops.py stage."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / (
        "scripts/compute_fundamental_ratios.py"
    )
    assert not p.exists(), (
        "scripts/compute_fundamental_ratios.py must be deleted after "
        "the migration — the canonical path is ops.py --stage."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
