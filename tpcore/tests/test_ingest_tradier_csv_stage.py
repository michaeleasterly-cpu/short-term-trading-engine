"""``ops.py --stage ingest_tradier_csv`` — Tradier CSV → prices_daily
streaming loader.

Migrated 2026-05-21 from ``scripts/ingest_tradier_csv.py`` (orphan-
scripts zero-allowlist sweep; operator overruled the prior keep-as-
helper disposition). The stage streams the wide Tradier CSV
produced by ``--stage extract_tradier_full`` into
``platform.prices_daily`` with the Alpaca-active filter +
``ON CONFLICT DO NOTHING`` idempotency.

Asserts the stage (1) is registered as ``--stage ingest_tradier_csv``
+ NOT in the daily ``--update`` cadence + carries the
``HEAVY_STAGE_TIMEOUT_SEC`` budget, (2) skips rows whose ticker is
not in the Alpaca-active set, (3) skips malformed / non-finite /
overflow OHLC rows (the 50k bad-row class the production load
hit), (4) honours ``no_alpaca_filter=true`` (load every CSV ticker),
(5) hard-fails on a missing CSV path, (6) emits the canonical
``ON CONFLICT (ticker, date) DO NOTHING`` SQL via
``executemany``, and (7) the sentinel verifies the legacy script
file is gone + the allowlist entry was removed.

No real DB / Alpaca touched. The pool fakes ``executemany`` and the
Alpaca client is patched in-body. pytest-xdist ops-shadow group per
the package-shadow rule.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def executemany(
        self, sql: str, batch: list[tuple],
    ) -> None:
        self.executemany_calls.append((sql, list(batch)))


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


def _write_tradier_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["ticker", "date", "open", "high", "low", "close", "volume"],
        )
        for row in rows:
            w.writerow(row)


def _patch_alpaca(
    monkeypatch: pytest.MonkeyPatch, *, active: set[str],
) -> None:
    async def _fake_fetch(_client: Any) -> list[dict[str, str]]:
        return [{"symbol": s} for s in sorted(active)]

    monkeypatch.setattr(
        "tpcore.data.ingest_alpaca_bars.fetch_active_us_equities",
        _fake_fetch,
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_alpaca_bars._alpaca_headers",
        lambda: {"APCA-API-KEY-ID": "x", "APCA-API-SECRET-KEY": "y"},
    )
    monkeypatch.setattr(
        "tpcore.data.ingest_alpaca_bars._alpaca_broker_base",
        lambda: "https://broker-api.alpaca.markets",
    )


async def test_filters_to_alpaca_active_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tickers outside the Alpaca-active set are skipped (counted in
    ``rows_skipped_filter``); active tickers are batched into the
    INSERT."""
    csv_path = tmp_path / "bars.csv"
    _write_tradier_csv(csv_path, [
        ["AAA", "2024-01-02", "1.0", "1.1", "0.9", "1.05", "100"],
        ["BBB", "2024-01-02", "2.0", "2.1", "1.9", "2.05", "200"],
        ["ZZZ", "2024-01-02", "3.0", "3.1", "2.9", "3.05", "300"],  # not active
    ])
    _patch_alpaca(monkeypatch, active={"AAA", "BBB"})
    conn = _Conn()

    result = await ops._stage_ingest_tradier_csv(
        _Pool(conn), config={"csv": str(csv_path)},
    )

    assert result["rows_read"] == 3
    assert result["rows_skipped_filter"] == 1
    assert result["rows_skipped_malformed"] == 0
    assert result["rows_attempted"] == 2
    assert result["tickers_seen"] == 2
    # One executemany batch with 2 rows.
    assert len(conn.executemany_calls) == 1
    sql, batch = conn.executemany_calls[0]
    assert "INSERT INTO platform.prices_daily" in sql
    assert "ON CONFLICT (ticker, date) DO NOTHING" in sql
    assert {row[0] for row in batch} == {"AAA", "BBB"}


async def test_overflow_and_nonfinite_rows_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production load hit ~50k rows where Tradier emitted Inf /
    overflow OHLC — the stage MUST skip them rather than overflow
    ``NUMERIC(20,6)`` on insert."""
    csv_path = tmp_path / "bars.csv"
    _write_tradier_csv(csv_path, [
        ["AAA", "2024-01-02", "1.0", "1.1", "0.9", "1.05", "100"],
        # Non-finite (Inf) — must be rejected.
        ["BBB", "2024-01-02", "Inf", "1.1", "0.9", "1.05", "100"],
        # Overflow (>= 1e14) — must be rejected.
        ["BBB", "2024-01-03", "1e15", "1.1", "0.9", "1.05", "100"],
        # Malformed (missing field) — must be rejected.
        ["CCC", "2024-01-02", "", "1.1", "0.9", "1.05", "100"],
    ])
    _patch_alpaca(monkeypatch, active={"AAA", "BBB", "CCC"})
    conn = _Conn()

    result = await ops._stage_ingest_tradier_csv(
        _Pool(conn), config={"csv": str(csv_path)},
    )

    assert result["rows_read"] == 4
    assert result["rows_skipped_filter"] == 0
    assert result["rows_skipped_malformed"] == 3
    assert result["rows_attempted"] == 1
    assert result["tickers_seen"] == 1


async def test_no_alpaca_filter_loads_every_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``no_alpaca_filter=true`` ⇒ EVERY CSV ticker passes — the
    Alpaca-asset fetch never happens (an internal guard, but also
    proves the operator's research-dataset knob is honoured)."""
    csv_path = tmp_path / "bars.csv"
    _write_tradier_csv(csv_path, [
        ["AAA", "2024-01-02", "1.0", "1.1", "0.9", "1.05", "100"],
        ["ZZZ", "2024-01-02", "3.0", "3.1", "2.9", "3.05", "300"],
    ])
    # NOTE: we deliberately do NOT patch the Alpaca fetcher — if the
    # stage ever calls it with no_alpaca_filter=true, the test will
    # error out on the real fetch.
    conn = _Conn()
    result = await ops._stage_ingest_tradier_csv(
        _Pool(conn),
        config={
            "csv": str(csv_path), "no_alpaca_filter": "true",
        },
    )
    assert result["rows_skipped_filter"] == 0
    assert result["rows_attempted"] == 2
    assert result["tickers_seen"] == 2


async def test_missing_csv_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CSV path that does not exist ⇒ ``SystemExit`` with the
    path in the message."""
    _patch_alpaca(monkeypatch, active=set())
    conn = _Conn()
    with pytest.raises(SystemExit, match="CSV not found"):
        await ops._stage_ingest_tradier_csv(
            _Pool(conn),
            config={"csv": str(tmp_path / "missing.csv")},
        )


def test_stage_registered_operator_on_demand_with_heavy_timeout() -> None:
    """Registration-pin: stage in ``_STAGE_SPECS`` + ``KNOWN_STAGES``,
    NOT in ``OPS_UPDATE_STAGES``, carries the heavy-timeout budget
    (the production load was multi-hour)."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "ingest_tradier_csv" in spec_names
    assert "ingest_tradier_csv" in ops.KNOWN_STAGES
    assert "ingest_tradier_csv" not in OPS_UPDATE_STAGES
    timeout = next(
        t for n, _, t in ops._STAGE_SPECS if n == "ingest_tradier_csv"
    )
    assert timeout == ops.HEAVY_STAGE_TIMEOUT_SEC


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: ``scripts/ingest_tradier_csv.py`` is gone + the
    allowlist entry was removed."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/ingest_tradier_csv.py"
    assert not script.exists()
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"ingest_tradier_csv"' not in text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
