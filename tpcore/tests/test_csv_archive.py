"""Unit tests for the CSV-first archive layer (BAMLH0A0HYM2 defence).

Pins the contract that motivated the module: a vendor silently
truncating its history must be caught by ``detect_shrinkage`` on the
*next* ingest, not weeks later when the operator notices the DB shrank.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tpcore.ingestion import csv_archive


@pytest.fixture
def _archive_root(tmp_path, monkeypatch):
    """Point the archive layer at a tmp dir so tests don't touch data/."""
    monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
    return tmp_path


def _rows(n: int) -> list[dict]:
    return [{"indicator": "credit_spread", "date": f"2026-01-{i % 28 + 1:02d}", "value": str(i)} for i in range(n)]


class TestWriteArchive:
    def test_write_creates_gzip_and_counts(self, _archive_root) -> None:
        res = csv_archive.write_archive(
            "fred_macro", _rows(100), fieldnames=["indicator", "date", "value"],
        )
        assert res.path.exists()
        assert res.path.suffix == ".gz"
        assert res.rows_written == 100
        assert res.rows_rejected == 0
        assert csv_archive.count_archive_rows(res.path) == 100

    def test_validator_rejects_bad_rows(self, _archive_root) -> None:
        rows = _rows(10) + [{"indicator": "", "date": "2026-01-01", "value": "9"}]
        res = csv_archive.write_archive(
            "fred_macro", rows, fieldnames=["indicator", "date", "value"],
            validator=lambda r: bool(r.get("indicator")),
        )
        assert res.rows_written == 10
        assert res.rows_rejected == 1

    def test_empty_input_still_writes_archive(self, _archive_root) -> None:
        res = csv_archive.write_archive("fred_macro", [], fieldnames=["a", "b"])
        assert res.path.exists()
        assert res.rows_written == 0
        assert csv_archive.count_archive_rows(res.path) == 0

    def test_roundtrip_read(self, _archive_root) -> None:
        res = csv_archive.write_archive(
            "fred_macro", _rows(5), fieldnames=["indicator", "date", "value"],
        )
        back = list(csv_archive.read_archive_rows(res.path))
        assert len(back) == 5
        assert back[0]["indicator"] == "credit_spread"


class TestShrinkageDetection:
    def test_first_run_returns_none(self, _archive_root) -> None:
        res = csv_archive.write_archive(
            "fred_macro", _rows(7500), fieldnames=["indicator", "date", "value"],
        )
        report = csv_archive.detect_shrinkage(
            "fred_macro", res.rows_written, exclude_path=res.path,
        )
        assert report is None  # no predecessor to compare against

    def test_stable_run_not_over_threshold(self, _archive_root) -> None:
        # Run 1 — establish the baseline.
        csv_archive.write_archive(
            "fred_macro", _rows(7500), fieldnames=["indicator", "date", "value"],
            now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        # Run 2 — same size + a few new rows. No shrinkage.
        r2 = csv_archive.write_archive(
            "fred_macro", _rows(7505), fieldnames=["indicator", "date", "value"],
            now=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        )
        report = csv_archive.detect_shrinkage(
            "fred_macro", r2.rows_written, exclude_path=r2.path,
        )
        assert report is not None
        assert report.previous_rows == 7500
        assert report.over_threshold is False

    def test_truncation_event_is_flagged(self, _archive_root) -> None:
        """The BAMLH0A0HYM2 scenario: 7,500 rows → vendor truncates to 785."""
        csv_archive.write_archive(
            "fred_macro", _rows(7500), fieldnames=["indicator", "date", "value"],
            now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        truncated = csv_archive.write_archive(
            "fred_macro", _rows(785), fieldnames=["indicator", "date", "value"],
            now=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        )
        report = csv_archive.detect_shrinkage(
            "fred_macro", truncated.rows_written, exclude_path=truncated.path,
        )
        assert report is not None
        assert report.previous_rows == 7500
        assert report.current_rows == 785
        # (7500-785)/7500 ≈ 0.895 — way over the 20% threshold.
        assert report.over_threshold is True
        assert report.shrinkage_pct == pytest.approx(0.895, abs=0.01)

    def test_log_shrinkage_warning_noop_when_under_threshold(self, _archive_root) -> None:
        # Should not raise; just a no-op when under threshold.
        rep = csv_archive.ShrinkageReport(
            source="fred_macro", current_rows=100, previous_rows=101,
            previous_archive="x", shrinkage_pct=0.01, over_threshold=False,
        )
        csv_archive.log_shrinkage_warning(rep)  # no exception = pass

    def test_assert_not_shrunk_raises_over_threshold(self) -> None:
        """Producer hard-stop: a full-snapshot pull that shrank past
        the threshold must RAISE so the stage fails loudly (the
        daily_bars producer-guard pattern, generalised via the
        existing shrinkage detector — no new per-source thresholds)."""
        rep = csv_archive.ShrinkageReport(
            source="fred_macro", current_rows=785, previous_rows=7500,
            previous_archive="prev.csv.gz", shrinkage_pct=0.895,
            over_threshold=True,
        )
        with pytest.raises(
            csv_archive.ProducerShrinkageError, match="fred_macro"
        ):
            csv_archive.assert_not_shrunk(rep)

    def test_assert_not_shrunk_noop_under_threshold_or_none(self) -> None:
        under = csv_archive.ShrinkageReport(
            source="fred_macro", current_rows=100, previous_rows=101,
            previous_archive="x", shrinkage_pct=0.01, over_threshold=False,
        )
        csv_archive.assert_not_shrunk(under)  # not over_threshold → no raise
        csv_archive.assert_not_shrunk(None)   # first run → no raise


class TestRepoDataDirEnvSeam:
    """Prep 1 — ``TP_DATA_DIR`` env seam (pure local no-op today).

    Host-agnostic seam for the deferred pre-Railway archive-substrate
    migration. When the env var is unset the path must be byte-identical
    to the prior ``<repo_root>/data`` expression — zero behaviour change.
    """

    _PRIOR_EXPR = (
        Path(csv_archive.__file__).resolve().parent.parent.parent / "data"
    )

    def test_default_unset_is_byte_identical_to_prior_expression(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("TP_DATA_DIR", raising=False)
        got = csv_archive.repo_data_dir()
        assert got == self._PRIOR_EXPR
        assert str(got) == str(self._PRIOR_EXPR)

    def test_env_set_relocates_archive_root(
        self, tmp_path, monkeypatch
    ) -> None:
        target = tmp_path / "relocated_data"
        monkeypatch.setenv("TP_DATA_DIR", str(target))
        assert csv_archive.repo_data_dir() == target
        # archive_dir_for must compose off the override, untouched.
        ad = csv_archive.archive_dir_for("fred_macro")
        assert ad == target / "fred_macro_archive"
        assert ad.is_dir()

    def test_empty_string_env_treated_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("TP_DATA_DIR", "")
        assert csv_archive.repo_data_dir() == self._PRIOR_EXPR
