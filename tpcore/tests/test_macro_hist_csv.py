"""Tests for the canonical one-time historical-CSV macro ingest path
(`handle_macro_indicators` hist_csv branch / `_ingest_macro_hist_csv`).

Pure: fake pool captures the executemany payload; write_archive is
stubbed so no archive file is written and the test stays deterministic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tpcore.ingestion import handlers


class _Conn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def executemany(self, sql: str, rows) -> None:
        self._sink.append((sql, list(rows)))

    async def fetchval(self, *a, **k):  # skip-guard path (must NOT be hit)
        raise AssertionError("skip-guard ran — hist branch must bypass it")


class _CM:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def __aenter__(self) -> _Conn:
        return _Conn(self._sink)

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.sink: list = []

    def acquire(self) -> _CM:
        return _CM(self.sink)


@pytest.fixture(autouse=True)
def _stub_archive(monkeypatch):
    class _A:
        path = "/tmp/_stub_fred_macro_hist.csv.gz"
        rows_written = 0

    monkeypatch.setattr(
        "tpcore.ingestion.csv_archive.write_archive",
        lambda *a, **k: _A(),
    )


def _csv(tmp_path: Path, body: str) -> str:
    p = tmp_path / "hy.csv"
    p.write_text("DATE,BAMLH0A0HYM2\n" + body)
    return str(p)


# Task #18 P5: writes go to macro_data via upsert_macro_data_bitemporal
# instead of executemany on the (renamed) legacy table. Tests stub the
# helper as a call-recorder so the mock _Conn doesn't need a full
# SCD-2 fetchrow fixture.
_macro_emit_calls: list[dict] = []


async def _record_emit(_conn, *, source: str, rows) -> dict[str, int]:
    _macro_emit_calls.append({"source": source, "rows": list(rows)})
    return {"inserted": len(rows), "revised": 0, "no_change": 0}


@pytest.fixture(autouse=True)
def _stub_macro_emit(monkeypatch):
    _macro_emit_calls.clear()
    monkeypatch.setattr(
        "tpcore.ingestion.macro_data_emit.upsert_macro_data_bitemporal",
        _record_emit,
    )


async def test_parses_skips_missing_and_upserts(tmp_path) -> None:
    csv = _csv(tmp_path, "1996-12-31,3.13\n1997-01-01,.\n1997-01-02,3.06\n2008-11-21,19.92\n")
    pool = _Pool()
    n = await handlers._ingest_macro_hist_csv(pool, csv, "hy_spread")  # noqa: SLF001
    assert n == 3  # the "." row skipped, not zeroed
    assert len(_macro_emit_calls) == 1 and _macro_emit_calls[0]["source"] == "fred"
    rows = _macro_emit_calls[0]["rows"]
    assert {r[0] for r in rows} == {"hy_spread"}            # only target indicator
    assert [str(r[1]) for r in rows] == ["1996-12-31", "1997-01-02", "2008-11-21"]
    assert float(rows[-1][2]) == 19.92                       # value fidelity
    assert all(r[3] is None for r in rows)                   # value_text NULL (XOR)


async def test_routes_via_handler_and_bypasses_skip_guard(tmp_path) -> None:
    csv = _csv(tmp_path, "2000-01-03,5.01\n")
    pool = _Pool()
    # _Conn.fetchval raises if the skip-guard runs — proving the hist
    # branch short-circuits before it (and never touches FREDAdapter).
    n = await handlers.handle_macro_indicators(
        pool, {"hist_csv_path": csv, "hist_indicator": "hy_spread"}
    )
    assert n == 1
    assert len(_macro_emit_calls) == 1
    assert _macro_emit_calls[0]["rows"][0][0] == "hy_spread"


async def test_empty_csv_raises(tmp_path) -> None:
    p = tmp_path / "e.csv"
    p.write_text("DATE,BAMLH0A0HYM2\n")
    with pytest.raises(RuntimeError, match="empty or header-only"):
        await handlers._ingest_macro_hist_csv(_Pool(), str(p), "hy_spread")  # noqa: SLF001


async def test_all_missing_raises(tmp_path) -> None:
    csv = _csv(tmp_path, "1997-01-01,.\n1997-02-17,.\n")
    with pytest.raises(RuntimeError, match="zero parseable rows"):
        await handlers._ingest_macro_hist_csv(_Pool(), csv, "hy_spread")  # noqa: SLF001
