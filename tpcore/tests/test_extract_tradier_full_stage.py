"""``ops.py --stage extract_tradier_full`` — wide-universe Tradier CSV
extractor (NO DB writes).

Migrated 2026-05-21 from ``scripts/extract_tradier_full.py`` (orphan-
scripts zero-allowlist sweep; operator overruled the prior keep-as-
helper disposition). The stage walks the full Tradier-tradable US
equity + ETF universe via ``/v1/markets/lookup`` and pulls daily
history from 2000-01-01 → today for each name, streaming bars into a
single CSV.

Asserts the stage (1) is registered as ``--stage extract_tradier_full``
+ NOT in the daily ``--update`` cadence + carries the
``HEAVY_STAGE_TIMEOUT_SEC`` budget, (2) hard-fails on missing
Tradier env vars, (3) caches the symbol-enumeration CSV on the
first run + reuses it on resume, (4) skips already-done tickers
from a previous bars-CSV (resumability), (5) writes a usable CSV
header + bar rows to disk, (6) respects the ``max_symbols`` smoke-
test knob, and (7) the sentinel verifies the legacy script file is
gone + the allowlist entry was removed.

No real Tradier or Postgres touched. ``httpx.AsyncClient`` is
patched to a fake that serves canned JSON. pytest-xdist ops-shadow
group per the package-shadow rule.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | str) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> dict:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _FakeClient:
    """Minimal stand-in for ``httpx.AsyncClient``. Drives:

    * ``/v1/markets/lookup`` → returns ``security`` list
    * ``/v1/markets/history`` → returns ``day`` rows per symbol
    """

    def __init__(
        self, *,
        lookup_securities: list[dict],
        history_by_symbol: dict[str, list[dict]],
    ) -> None:
        self._lookup_securities = lookup_securities
        self._history_by_symbol = history_by_symbol
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(
        self, path: str, params: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.get_calls.append((path, params or {}))
        if path == "/v1/markets/lookup":
            return _FakeResponse(
                200,
                {"securities": {"security": self._lookup_securities}},
            )
        if path == "/v1/markets/history":
            sym = (params or {}).get("symbol", "")
            days = self._history_by_symbol.get(sym, [])
            return _FakeResponse(200, {"history": {"day": days}})
        return _FakeResponse(404, "")


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch, client: _FakeClient,
) -> None:
    """Replace the ``httpx.AsyncClient`` constructor in the stage's
    namespace so the entire run goes through ``_FakeClient``."""
    def _make(*_a: Any, **_k: Any) -> _FakeClient:
        return client

    monkeypatch.setattr("httpx.AsyncClient", _make)


async def test_happy_path_extracts_two_symbols_and_writes_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-run path: enumerates universe, writes the symbols CSV,
    fetches per-symbol bars, streams rows into the bars CSV."""
    monkeypatch.setenv("TRADIER_PRODUCTION_TOKEN", "test-token")
    securities = [
        {"symbol": "AAA", "exchange": "N", "type": "stock", "description": "A"},
        {"symbol": "BBB", "exchange": "Q", "type": "etf", "description": "B"},
    ]
    history = {
        "AAA": [
            {"date": "2024-01-02", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100},
        ],
        "BBB": [
            {"date": "2024-01-02", "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.05, "volume": 200},
            {"date": "2024-01-03", "open": 2.05, "high": 2.2, "low": 2.0, "close": 2.15, "volume": 250},
        ],
    }
    client = _FakeClient(
        lookup_securities=securities, history_by_symbol=history,
    )
    _install_fake_httpx(monkeypatch, client)

    result = await ops._stage_extract_tradier_full(
        pool=None,
        config={
            "out_dir": str(tmp_path),
            "end_date": "2024-01-31",
        },
    )

    # Symbols CSV written.
    symbols_csv = tmp_path / "tradier_symbols_full.csv"
    assert symbols_csv.exists()
    with symbols_csv.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["symbol", "exchange", "type", "description"]
    assert {r[0] for r in rows[1:]} == {"AAA", "BBB"}

    # Bars CSV has a header + 3 rows (1 + 2).
    bars_csv = tmp_path / "tradier_bars_full.csv"
    assert bars_csv.exists()
    with bars_csv.open(encoding="utf-8") as fh:
        bars_rows = list(csv.reader(fh))
    assert bars_rows[0] == [
        "ticker", "date", "open", "high", "low", "close", "volume",
    ]
    assert len(bars_rows) - 1 == 3

    assert result["tickers_processed"] == 2
    assert result["tickers_fetched"] == 2
    assert result["rows_appended"] == 3
    assert result["symbols_total"] == 2
    assert result["tickers_already_done"] == 0


async def test_resumability_skips_already_done_tickers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bars CSV pre-populated with AAA rows ⇒ the stage only
    processes BBB on this run. Resumability proof."""
    monkeypatch.setenv("TRADIER_PRODUCTION_TOKEN", "test-token")
    # Pre-write the bars CSV header + an AAA row.
    bars_csv = tmp_path / "tradier_bars_full.csv"
    with bars_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["ticker", "date", "open", "high", "low", "close", "volume"],
        )
        w.writerow(
            ["AAA", "2023-01-02", "1.0", "1.1", "0.9", "1.05", "100"],
        )
    # Also pre-write the symbols CSV so the stage uses cached path.
    symbols_csv = tmp_path / "tradier_symbols_full.csv"
    with symbols_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "exchange", "type", "description"])
        w.writerow(["AAA", "N", "stock", "A"])
        w.writerow(["BBB", "Q", "etf", "B"])

    client = _FakeClient(
        lookup_securities=[],
        history_by_symbol={
            "BBB": [
                {"date": "2024-01-02", "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.05, "volume": 200},
            ],
        },
    )
    _install_fake_httpx(monkeypatch, client)

    result = await ops._stage_extract_tradier_full(
        pool=None,
        config={"out_dir": str(tmp_path), "end_date": "2024-01-31"},
    )

    assert result["tickers_already_done"] == 1  # AAA pre-populated
    assert result["tickers_processed"] == 1     # only BBB ran
    assert result["tickers_fetched"] == 1


async def test_max_symbols_caps_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_symbols=1`` ⇒ only the first un-done ticker runs.
    Pin-tests the smoke-test knob."""
    monkeypatch.setenv("TRADIER_PRODUCTION_TOKEN", "test-token")
    securities = [
        {"symbol": "AAA", "exchange": "N", "type": "stock", "description": "A"},
        {"symbol": "BBB", "exchange": "Q", "type": "etf", "description": "B"},
        {"symbol": "CCC", "exchange": "N", "type": "stock", "description": "C"},
    ]
    history = {
        s: [{"date": "2024-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        for s in ("AAA", "BBB", "CCC")
    }
    client = _FakeClient(
        lookup_securities=securities, history_by_symbol=history,
    )
    _install_fake_httpx(monkeypatch, client)

    result = await ops._stage_extract_tradier_full(
        pool=None,
        config={
            "out_dir": str(tmp_path),
            "max_symbols": 1,
            "end_date": "2024-01-31",
        },
    )
    assert result["tickers_processed"] == 1
    assert result["tickers_fetched"] == 1
    assert result["symbols_total"] == 3


async def test_missing_token_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``TRADIER_PRODUCTION_TOKEN`` (or alias) ⇒ ``SystemExit``
    with a usable message — the stage cannot silently emit empty
    output."""
    monkeypatch.delenv("TRADIER_PRODUCTION_TOKEN", raising=False)
    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TRADIER"):
        await ops._stage_extract_tradier_full(
            pool=None,
            config={"out_dir": str(tmp_path)},
        )


def test_stage_registered_operator_on_demand_with_heavy_timeout() -> None:
    """Registration-pin: stage in ``_STAGE_SPECS`` + ``KNOWN_STAGES``,
    NOT in ``OPS_UPDATE_STAGES``, carries the heavy-timeout budget
    (the legacy script took ~80 minutes on a full run)."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "extract_tradier_full" in spec_names
    assert "extract_tradier_full" in ops.KNOWN_STAGES
    assert "extract_tradier_full" not in OPS_UPDATE_STAGES, (
        "extract_tradier_full is operator-on-demand only — it must "
        "NOT be in the daily --update cadence (full Tradier extract "
        "is multi-hour wall time)"
    )
    timeout = next(
        t for n, _, t in ops._STAGE_SPECS if n == "extract_tradier_full"
    )
    assert timeout == ops.HEAVY_STAGE_TIMEOUT_SEC


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: ``scripts/extract_tradier_full.py`` is gone + the
    allowlist entry was removed."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/extract_tradier_full.py"
    assert not script.exists()
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"extract_tradier_full"' not in text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
