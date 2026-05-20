"""``ops.py --stage compare_baselines`` — trade-log equivalence diff.

Migrated 2026-05-21 from ``scripts/compare_baselines.py`` (orphan-
scripts zero-allowlist sweep; catalog at
``docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md``).
Operator overruled the prior "keep as ops helper" disposition; the
canonical path is now the stage, the script was deleted.

Asserts the stage (1) wraps ``tpcore.backtest.compare_trade_lists``
honestly (no parallel diff logic), (2) is registered in
``_STAGE_SPECS`` + ``KNOWN_STAGES`` and NOT in ``OPS_UPDATE_STAGES``
(operator-on-demand, not daily-cadence), (3) returns the expected
detail-dict shape for both equivalent and non-equivalent inputs,
(4) hard-fails with a usable message on missing-arg / missing-file
inputs, and (5) the sentinel checks the script file is gone + the
allowlist entry is removed.

No real DB / Alpaca / FMP touched. Pure file I/O against
``tmp_path`` CSV fixtures. pytest-xdist ops-shadow group per the
package-shadow rule.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


_TRADE_LOG_HEADER = (
    "ticker", "entry_date", "entry_price",
    "exit_date", "exit_price", "pnl_pct",
    "direction", "exit_reason",
)


def _write_trade_log(path: Path, rows: list[tuple[str, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_TRADE_LOG_HEADER)
        for row in rows:
            w.writerow(row)


def _row(
    *, ticker: str = "AAPL", entry_date: str = "2024-01-02",
    entry_price: str = "100.00", exit_date: str = "2024-01-05",
    exit_price: str = "105.00", pnl_pct: str = "0.05",
    direction: str = "LONG", exit_reason: str = "TP_HIT",
) -> tuple[str, ...]:
    return (
        ticker, entry_date, entry_price, exit_date,
        exit_price, pnl_pct, direction, exit_reason,
    )


async def test_equivalent_baselines_returns_equivalent_true(
    tmp_path: Path,
) -> None:
    """Identical trade logs ⇒ ``equivalent=True`` + matching trade
    counts. Pool unused (the stage is pure file I/O)."""
    baseline = tmp_path / "baseline.csv"
    candidate = tmp_path / "candidate.csv"
    rows = [_row()]
    _write_trade_log(baseline, rows)
    _write_trade_log(candidate, rows)

    result = await ops._stage_compare_baselines(
        pool=None,
        config={
            "baseline": str(baseline),
            "candidate": str(candidate),
        },
    )
    assert result["equivalent"] is True
    assert result["baseline_trades"] == 1
    assert result["candidate_trades"] == 1
    assert result["baseline_path"] == str(baseline)
    assert result["candidate_path"] == str(candidate)
    # Defaults must match the canonical equivalence-API defaults — the
    # stage is a thin wrapper, not a re-implementation.
    from tpcore.backtest.equivalence import (
        DEFAULT_TOL_PNL_PCT,
        DEFAULT_TOL_PRICE,
    )
    assert result["tol_pnl_pct"] == DEFAULT_TOL_PNL_PCT
    assert result["tol_price"] == DEFAULT_TOL_PRICE


async def test_divergent_baselines_returns_equivalent_false(
    tmp_path: Path,
) -> None:
    """A pnl_pct delta beyond the tolerance ⇒ ``equivalent=False``.
    Confirms the stage propagates the ``compare_trade_lists`` verdict
    rather than masking it."""
    baseline = tmp_path / "baseline.csv"
    candidate = tmp_path / "candidate.csv"
    _write_trade_log(baseline, [_row(pnl_pct="0.05")])
    _write_trade_log(
        candidate,
        [_row(pnl_pct="0.10")],  # 5pp delta — far above default 1e-6
    )

    result = await ops._stage_compare_baselines(
        pool=None,
        config={
            "baseline": str(baseline),
            "candidate": str(candidate),
        },
    )
    assert result["equivalent"] is False
    assert "summary" in result and result["summary"]


async def test_tolerance_overrides_propagate(tmp_path: Path) -> None:
    """``tol_pnl_pct`` override widens the tolerance enough to
    re-classify a divergent pair as equivalent. Pin-tests the
    operator's tolerance knob is plumbed through."""
    baseline = tmp_path / "baseline.csv"
    candidate = tmp_path / "candidate.csv"
    _write_trade_log(baseline, [_row(pnl_pct="0.05")])
    _write_trade_log(candidate, [_row(pnl_pct="0.0500001")])  # 1e-7 delta

    result = await ops._stage_compare_baselines(
        pool=None,
        config={
            "baseline": str(baseline),
            "candidate": str(candidate),
            "tol_pnl_pct": "1e-3",
        },
    )
    assert result["equivalent"] is True
    assert result["tol_pnl_pct"] == 1e-3


async def test_missing_required_param_raises(tmp_path: Path) -> None:
    """Missing ``baseline`` or ``candidate`` ⇒ ``SystemExit`` with a
    usable error message (mirrors the legacy script's argparse
    ``required=True`` semantics)."""
    baseline = tmp_path / "baseline.csv"
    _write_trade_log(baseline, [_row()])

    with pytest.raises(SystemExit, match="required"):
        await ops._stage_compare_baselines(
            pool=None,
            config={"baseline": str(baseline)},  # candidate missing
        )


async def test_nonexistent_file_raises(tmp_path: Path) -> None:
    """A path that doesn't exist ⇒ ``SystemExit`` with the path in
    the message — the stage cannot pretend the file existed and
    silently emit a green diff."""
    missing = tmp_path / "does-not-exist.csv"
    baseline = tmp_path / "baseline.csv"
    _write_trade_log(baseline, [_row()])

    with pytest.raises(SystemExit, match="not found"):
        await ops._stage_compare_baselines(
            pool=None,
            config={
                "baseline": str(baseline),
                "candidate": str(missing),
            },
        )


def test_stage_registered_operator_on_demand_only() -> None:
    """Registration-pin: ``compare_baselines`` is in ``_STAGE_SPECS``
    + ``KNOWN_STAGES`` (so ``--stage compare_baselines`` resolves) but
    NOT in ``OPS_UPDATE_STAGES`` (so the daily ``--update`` cadence
    never fires it)."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "compare_baselines" in spec_names
    assert "compare_baselines" in ops.KNOWN_STAGES
    assert "compare_baselines" not in OPS_UPDATE_STAGES, (
        "compare_baselines is operator-on-demand only — it must NOT "
        "be added to OPS_UPDATE_STAGES (the daily --update cadence)"
    )


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: the legacy ``scripts/compare_baselines.py`` file is
    gone and the orphan allowlist entry has been removed — locks in
    the migration so a stray script revival can't silently re-orphan
    the path."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/compare_baselines.py"
    assert not script.exists(), (
        "scripts/compare_baselines.py must be deleted after the "
        "migration — the canonical path is ops.py --stage."
    )
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"compare_baselines"' not in text, (
        "compare_baselines allowlist entry must be removed when the "
        "stage lands; leaving it would block a future genuine orphan "
        "from being flagged"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
