"""Sentinel tests for ``_stage_rebuild_corporate_actions_from_archive``.

PR fix/feed-audit-wave-1-critical-path-blockers — Wave-1 critical-path
heal for the ``corporate_actions_completeness`` shrinkage red on
``main`` (live=109737 vs archive=110630, 0.81% shrinkage).

The stage reads the latest ``.csv.gz`` archive and replays missing
rows back via the canonical ``upsert_corporate_actions`` path. These
tests pin the parsing + upsert-invocation contract without round-
tripping through asyncpg.
"""
from __future__ import annotations

import csv
import gzip
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Use importlib to load scripts/ops.py under a private name —
# bypasses the ops-package-shadow rule (ops/*.py exists as a sibling
# Python package). Same pattern as
# tests/test_stage_historical_delisted_universe.py.
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_rebuild_corp_actions", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_rebuild_corp_actions"] = ops
_spec.loader.exec_module(ops)


pytestmark = pytest.mark.xdist_group("ops_shadow")


@pytest.fixture
def fake_archive(tmp_path: Path) -> Path:
    """Write a minimal CSV archive that matches the
    ``alpaca_corporate_actions`` schema written by
    ``tpcore.ingestion.handlers.handle_corporate_actions``."""
    archive_dir = tmp_path / "alpaca_corporate_actions"
    archive_dir.mkdir()
    archive_path = (
        archive_dir / "alpaca_corporate_actions_20260515_120000.csv.gz"
    )
    rows = [
        {
            "ticker": "AAPL",
            "action_date": "2020-08-31",
            "action_type": "split",
            "ratio": "4",
            "raw": '{"old_rate":1,"new_rate":4}',
        },
        {
            "ticker": "TSLA",
            "action_date": "2020-08-31",
            "action_type": "split",
            "ratio": "5",
            "raw": '{"old_rate":1,"new_rate":5}',
        },
        {
            "ticker": "MSFT",
            "action_date": "2024-02-15",
            "action_type": "dividend",
            "ratio": "0.75",
            "raw": '{"rate":0.75}',
        },
    ]
    with gzip.open(archive_path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ticker", "action_date", "action_type", "ratio", "raw"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return archive_path


# ── R1 — dry_run does not call upsert; parses + returns count ─────────


async def test_R1_dry_run_does_not_upsert(fake_archive: Path) -> None:
    pool = AsyncMock()
    fake_upsert = AsyncMock(return_value=0)
    with patch(
        "tpcore.data.ingest_corporate_actions.upsert_corporate_actions",
        new=fake_upsert,
    ):
        result = await ops._stage_rebuild_corporate_actions_from_archive(
            pool,
            {"archive_path": str(fake_archive), "dry_run": True},
        )
    assert result["rows_parsed"] == 3
    assert result["rows_inserted"] == 0
    assert result["dry_run"] is True
    fake_upsert.assert_not_awaited()


# ── R2 — non-dry-run invokes the canonical upsert ─────────────────────


async def test_R2_non_dry_run_invokes_upsert_corporate_actions(
    fake_archive: Path,
) -> None:
    pool = AsyncMock()
    fake_upsert = AsyncMock(return_value=3)
    with patch(
        "tpcore.data.ingest_corporate_actions.upsert_corporate_actions",
        new=fake_upsert,
    ):
        result = await ops._stage_rebuild_corporate_actions_from_archive(
            pool,
            {"archive_path": str(fake_archive)},
        )
    assert result["rows_parsed"] == 3
    assert result["rows_inserted"] == 3
    fake_upsert.assert_awaited_once()
    # The upsert receives a list[dict] with the normalized keys
    # ``ticker``, ``action_date``, ``action_type``, ``ratio``,
    # ``raw_data``. Inspect the call.
    _pool_arg, actions = fake_upsert.await_args[0]
    assert len(actions) == 3
    assert {a["ticker"] for a in actions} == {"AAPL", "TSLA", "MSFT"}
    aapl = next(a for a in actions if a["ticker"] == "AAPL")
    from datetime import date as date_t
    from decimal import Decimal as Decimal_t
    assert aapl["action_date"] == date_t(2020, 8, 31)
    assert aapl["action_type"] == "split"
    assert aapl["ratio"] == Decimal_t("4")
    assert "replayed_from_archive" in aapl["raw_data"]


# ── R3 — missing archive_path raises (no silent green) ───────────────


async def test_R3_missing_archive_path_raises() -> None:
    pool = AsyncMock()
    with patch(
        "tpcore.ingestion.csv_archive.latest_archive",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="no prior archive"):
            await ops._stage_rebuild_corporate_actions_from_archive(pool, {})


# ── R4 — malformed rows are counted, not silently dropped ────────────


async def test_R4_malformed_rows_counted_as_parse_failures(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "alpaca_corporate_actions"
    archive_dir.mkdir()
    archive_path = (
        archive_dir / "alpaca_corporate_actions_20260515_120000.csv.gz"
    )
    rows = [
        {
            "ticker": "AAPL",
            "action_date": "2020-08-31",
            "action_type": "split",
            "ratio": "4",
            "raw": "{}",
        },
        # Malformed: missing ratio.
        {
            "ticker": "BAD1",
            "action_date": "2024-01-01",
            "action_type": "split",
            "ratio": "",
            "raw": "{}",
        },
        # Malformed: bad date.
        {
            "ticker": "BAD2",
            "action_date": "not-a-date",
            "action_type": "split",
            "ratio": "2",
            "raw": "{}",
        },
    ]
    with gzip.open(archive_path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ticker", "action_date", "action_type", "ratio", "raw"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    pool = AsyncMock()
    fake_upsert = AsyncMock(return_value=1)
    with patch(
        "tpcore.data.ingest_corporate_actions.upsert_corporate_actions",
        new=fake_upsert,
    ):
        result = await ops._stage_rebuild_corporate_actions_from_archive(
            pool, {"archive_path": str(archive_path)},
        )
    assert result["rows_parsed"] == 1
    assert result["parse_failures"] == 2
