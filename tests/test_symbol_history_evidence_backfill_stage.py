"""Stage-level tests for ``_stage_symbol_history_evidence_backfill``
(spec PR #442 + plan PR #443; impl 2026-06-02).

Pins the 14 hard invariants from the brief / plan §3-§9 + the manifest
schema + the bulk/S3-first source sentinel + the at-most-one-httpx-get
AST sentinel. Hermetic: mock pool, fake S3 backend, fake SEC zip,
in-memory synthetic data. No real DB, no real network.
"""
from __future__ import annotations

import ast
import gzip
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")
os.environ.setdefault("FMP_API_KEY", "test-fmp-api-key")


# ────────────────────────────────────────────────────────────────
# Source sentinels (string-match on scripts/ops.py)
# ────────────────────────────────────────────────────────────────


def _ops_text() -> str:
    return (
        Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    ).read_text(encoding="utf-8")


def _stage_body() -> str:
    text = _ops_text()
    start = text.find("async def _stage_symbol_history_evidence_backfill")
    end = text.find("def _mint_tkr14_predecessor", start)
    assert start > 0 and end > start, (
        "could not locate the symbol_history_evidence_backfill stage "
        "body in scripts/ops.py"
    )
    return text[start:end]


def test_stage_registered_in_stage_specs() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}  # noqa: SLF001
    assert "symbol_history_evidence_backfill" in names


def test_dry_run_default_true() -> None:
    """The brief / plan §6.1 pin ``dry_run`` default to True."""
    body = _stage_body()
    assert 'dry_run = _to_bool(cfg.get("dry_run", True))' in body


def test_at_most_one_httpx_get_call_site() -> None:
    """Static AST sentinel: at most ONE ``httpx.AsyncClient.get`` call
    site anywhere in scripts/ops.py for the symbol-history stage body
    + its private helpers. Per-row HTTP is the producer-hard-stop
    enforced by plan §7."""
    text = _ops_text()
    tree = ast.parse(text)

    targets = (
        "_stage_symbol_history_evidence_backfill",
        "_fetch_fmp_symbol_change_bulk",
        "_fmp_symbol_change_download",
        "_build_sec_ticker_cik_crosswalk",
    )
    n_get_calls = 0

    class _GetVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            nonlocal n_get_calls
            func = node.func
            # `client.get(...)` shape (Attribute -> attr='get').
            if isinstance(func, ast.Attribute) and func.attr == "get":
                n_get_calls += 1
            self.generic_visit(node)

    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in targets:
            _GetVisitor().visit(fn)
        elif isinstance(fn, ast.FunctionDef) and fn.name in targets:
            _GetVisitor().visit(fn)

    # The single legitimate call site lives in _fmp_symbol_change_download.
    # ``.get`` on a dict (cfg.get(...)) also looks like Attribute(attr='get');
    # those are stage-local and counted too — so we relax the upper bound
    # to a tight ceiling that fires when a NEW httpx.AsyncClient.get is
    # added.
    # Sentinel form: assert the *literal* substring "client.get(" appears
    # at most once across the four target functions' source.
    fn_text = ""
    for fn in ast.walk(tree):
        if (
            isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
            and fn.name in targets
        ):
            fn_text += ast.unparse(fn) + "\n"
    assert fn_text.count("client.get(") <= 1, (
        "stage source must contain at most one ``client.get(...)`` call "
        "site (the bulk-GET in _fmp_symbol_change_download). Per-ticker "
        "crawl is a producer-hard-stop."
    )


def test_use_bulk_zip_false_raises_in_source() -> None:
    """The stage source explicitly raises on ``use_bulk_zip=false``."""
    body = _stage_body()
    assert 'use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", True))' in body
    # The raise+message references the producer-hard-stop language.
    assert "producer-hard-stop" in body or "per-ticker crawl" in body
    assert "raise RuntimeError" in body


def test_manifest_schema_pinned() -> None:
    """The 13-column manifest CSV is the operator-facing contract."""
    from scripts.ops import _symbol_history_evidence_manifest_columns
    cols = _symbol_history_evidence_manifest_columns()
    assert cols == (
        "oldSymbol",
        "newSymbol",
        "change_date",
        "companyName",
        "old_cik_resolved",
        "old_cik_source",
        "new_cik_resolved",
        "new_cik_source",
        "predecessor_classification_id_minted",
        "classification_action",
        "ticker_history_written",
        "issuer_securities_written",
        "disposition",
    )


def test_sentinel_date_routes_to_data_quality_log_in_source() -> None:
    body = _stage_body()
    assert "FMP_SYMBOL_CHANGE_SENTINEL_DATE" in body
    assert "fmp_symbol_change_sentinel_date" in body
    assert "data_quality_log" in body


def test_fmp_only_kind_emitted_in_source() -> None:
    body = _stage_body()
    assert "fmp_only_no_issuer" in body


def test_idempotent_upsert_sql_present() -> None:
    body = _stage_body()
    assert "ON CONFLICT (classification_id, valid_from)" in body
    assert (
        "ON CONFLICT (issuer_id, classification_id, valid_from)" in body
    )
    assert "ON CONFLICT (id) DO NOTHING" in body
    assert "ON CONFLICT (issuer_id) DO NOTHING" in body
    # No DELETE statements in the additive-only stage.
    assert "DELETE FROM platform.fundamentals_quarterly" not in body
    assert "DELETE FROM platform.ticker_history" not in body
    assert "DELETE FROM platform.issuer_securities" not in body


def test_no_fundamentals_quarterly_writes() -> None:
    """The stage is additive-only on ticker_history / issuer_securities /
    ticker_classifications — it must NEVER write to fundamentals_quarterly.

    We allow the literal in the docstring (where the operator-facing
    non-goal is enumerated) but forbid it in any SQL or runtime
    expression.
    """
    body = _stage_body()
    # Strip the docstring (triple-quoted block at function start).
    # Match the first triple-quoted block and remove it.
    import re as _re
    body_no_doc = _re.sub(
        r'""".*?"""', "", body, count=1, flags=_re.DOTALL,
    )
    forbidden_patterns = (
        "platform.fundamentals_quarterly",
        "INSERT INTO fundamentals_quarterly",
        "DELETE FROM fundamentals_quarterly",
        "UPDATE fundamentals_quarterly",
        "FROM platform.fundamentals_quarterly",
    )
    for pat in forbidden_patterns:
        assert pat not in body_no_doc, (
            f"symbol_history_evidence_backfill must not reference "
            f"fundamentals_quarterly in executable code; found {pat!r}"
        )


def test_tkr14_predecessor_uses_z_venue() -> None:
    text = _ops_text()
    start = text.find("def _mint_tkr14_predecessor")
    end = text.find("def _resolve_old_cik_from_crosswalk", start)
    body = text[start:end]
    # ipo_venue=IPOVenue.OTHER  ("Z" sentinel)
    assert "IPOVenue.OTHER" in body or "_IPO.OTHER" in body
    assert "AssetClass.STOCK" in body or "_AC.STOCK" in body


# ────────────────────────────────────────────────────────────────
# Behavioural tests — hermetic mocks
# ────────────────────────────────────────────────────────────────


def _mock_pool(
    tc_rows: list[dict],
    captured: dict[str, list],
    *,
    fetchrow_open_window: dict | None = None,
    fetchrow_factory: Any = None,
    insert_failure_table: str | None = None,
) -> MagicMock:
    """Build a mock asyncpg pool.

    Args:
      tc_rows: current ``ticker_classifications`` snapshot returned
        by the §4 ``SELECT lifetime_end IS NULL`` query.
      captured: dict the test inspects post-call. Keys:
        ``issuers`` / ``ticker_classifications`` / ``issuer_securities`` /
        ``ticker_history`` / ``data_quality_log`` for the bulk
        ``executemany`` paths PLUS
        ``th_updates`` (Option B UPDATE param tuples) /
        ``th_single_inserts`` (Option B INSERT param tuples) /
        ``dql_single_inserts`` (in-loop dql writes).
      fetchrow_open_window: pre-existing open-ended ``ticker_history``
        row the Option B guard SELECT returns. Single static row
        for tests that touch one classification_id.
      fetchrow_factory: optional callable ``(sql, *args) -> row|None``
        for tests that need per-call dynamic responses (e.g.,
        idempotency).
      insert_failure_table: name of a table (``platform.ticker_history``
        for the Option B INSERT failure test) whose ``conn.execute``
        INSERT should raise to assert transactional rollback.
    """
    conn = MagicMock()

    async def _fetch(sql: str, *_args: Any, **_kw: Any) -> list[dict]:
        if "FROM platform.ticker_classifications" in sql:
            return tc_rows
        return []

    async def _fetchrow(sql: str, *args: Any, **_kw: Any) -> Any:
        if fetchrow_factory is not None:
            return fetchrow_factory(sql, *args)
        if "FROM platform.ticker_history" in sql:
            return fetchrow_open_window
        return None

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(return_value=0)

    async def _execute(sql: str, *args: Any) -> None:
        if "UPDATE platform.ticker_history" in sql:
            captured.setdefault("th_updates", []).append(args)
            return
        if "INSERT INTO platform.ticker_history" in sql:
            captured.setdefault("th_single_inserts", []).append(args)
            if insert_failure_table == "platform.ticker_history":
                raise RuntimeError("simulated INSERT failure")
            return
        if "INSERT INTO platform.data_quality_log" in sql:
            captured.setdefault("dql_single_inserts", []).append(args)
            return
        return

    conn.execute = AsyncMock(side_effect=_execute)

    async def _executemany(sql: str, args: list[tuple]) -> None:
        if "INSERT INTO platform.issuers" in sql:
            captured.setdefault("issuers", []).extend(args)
        elif "INSERT INTO platform.ticker_classifications" in sql:
            captured.setdefault("ticker_classifications", []).extend(args)
        elif "INSERT INTO platform.issuer_securities" in sql:
            captured.setdefault("issuer_securities", []).extend(args)
        elif "INSERT INTO platform.ticker_history" in sql:
            captured.setdefault("ticker_history", []).extend(args)
        elif "INSERT INTO platform.data_quality_log" in sql:
            captured.setdefault("data_quality_log", []).extend(args)

    conn.executemany = AsyncMock(side_effect=_executemany)

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


class _FakeBackend:
    """In-memory backend implementing the ArchiveBackend protocol."""

    def __init__(
        self,
        existing: dict[str, bytes] | None = None,
        write_returns_different_bytes: bool = False,
    ) -> None:
        self._store: dict[str, bytes] = dict(existing or {})
        self._write_corrupt = write_returns_different_bytes

    def write(self, source: str, body: bytes, filename: str) -> str:
        if self._write_corrupt:
            # Simulate R2 returning DIFFERENT bytes on re-read (parity
            # mismatch case).
            self._store[filename] = body + b"\x00CORRUPT"
        else:
            self._store[filename] = body
        return f"fake://{source}/{filename}"

    def read(self, source: str, filename: str) -> bytes:
        return self._store[filename]

    def read_latest(self, source: str) -> bytes | None:
        if not self._store:
            return None
        return self._store[sorted(self._store.keys())[-1]]

    def list_archives(self, source: str) -> list[str]:
        return sorted(self._store.keys())


def _gzipped_json(rows: list[dict]) -> bytes:
    return gzip.compress(json.dumps(rows).encode("utf-8"))


def _archive_filename_at(when: datetime) -> str:
    return f"fmp_symbol_change_{when.strftime('%Y%m%dT%H%MZ')}.csv.gz"


@pytest.fixture(autouse=True)
def _stub_crosswalk_and_select_backend(monkeypatch: pytest.MonkeyPatch):
    """All tests get a stub for the SEC cross-walk + the archive
    backend selector. Individual tests override the cross-walk + backend
    state by mutating the per-test container."""
    container = {
        "crosswalk": {},
        "backend": _FakeBackend(),
    }

    async def _fake_build_crosswalk(*, log: Any) -> dict:
        return container["crosswalk"]

    def _fake_select_backend() -> Any:
        return container["backend"]

    from scripts import ops as _ops
    monkeypatch.setattr(
        _ops, "_build_sec_ticker_cik_crosswalk", _fake_build_crosswalk,
    )
    # The stage imports select_backend INSIDE _fetch_fmp_symbol_change_bulk
    # — patch the canonical source.
    from tpcore.ingestion import csv_archive_backends as _backends
    monkeypatch.setattr(
        _backends, "select_backend", _fake_select_backend,
    )
    return container


@pytest.mark.asyncio
async def test_use_bulk_zip_false_raises(_stub_crosswalk_and_select_backend, tmp_path):
    """Producer-hard-stop: ``use_bulk_zip=false`` raises BEFORE any
    HTTP call or DB read."""
    from scripts.ops import _stage_symbol_history_evidence_backfill
    pool = _mock_pool(tc_rows=[], captured={})
    with pytest.raises(RuntimeError, match="use_bulk_zip"):
        await _stage_symbol_history_evidence_backfill(
            pool,
            {"use_bulk_zip": False, "manifest_path": str(tmp_path / "m.csv")},
        )


@pytest.mark.asyncio
async def test_archive_first_short_circuits_provider(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When a fresh archive exists, the stage does NOT call FMP."""
    from scripts import ops as _ops
    # Seed a fresh archive (~1 hour old).
    now = datetime.now(UTC) - timedelta(hours=1)
    fname = _archive_filename_at(now)
    payload = _gzipped_json([
        {"oldSymbol": "AAA", "newSymbol": "BBB", "date": "2020-06-01",
         "companyName": "Test Co"},
    ])
    _stub_crosswalk_and_select_backend["backend"] = _FakeBackend({fname: payload})

    download_called: dict[str, bool] = {"value": False}

    async def _no_download(**_kw: Any) -> bytes:
        download_called["value"] = True
        return _gzipped_json([])

    import scripts.ops as _ops_mod
    _ops_mod._fmp_symbol_change_download = _no_download  # type: ignore[assignment]

    pool = _mock_pool(
        tc_rows=[{
            "ticker": "BBB",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Test Co",
        }],
        captured={},
    )
    out = await _ops._stage_symbol_history_evidence_backfill(
        pool, {
            "dry_run": True,
            "manifest_path": str(tmp_path / "m.csv"),
        },
    )
    assert download_called["value"] is False, (
        "fresh archive must short-circuit the provider download"
    )
    assert out["dry_run"] is True


@pytest.mark.asyncio
async def test_archive_after_download_parity_mismatch_raises(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When R2 returns DIFFERENT bytes after write, the parity check
    hard-stops the run BEFORE any DB write."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "AAA", "newSymbol": "BBB", "date": "2020-06-01",
         "companyName": "Test Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]
    _stub_crosswalk_and_select_backend["backend"] = _FakeBackend(
        write_returns_different_bytes=True,
    )

    captured: dict[str, list] = {}
    pool = _mock_pool(tc_rows=[], captured=captured)

    with pytest.raises(RuntimeError, match="archive parity check"):
        await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
            "dry_run": False,
            "force_download": True,
            "local_cache_path": str(tmp_path / "cache.json.gz"),
            "manifest_path": str(tmp_path / "m.csv"),
        })

    # No DB writes hit.
    assert "ticker_history" not in captured
    assert "issuer_securities" not in captured


@pytest.mark.asyncio
async def test_sentinel_date_routes_to_data_quality_log_and_skips_history(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """1969-12-31 sentinel-date rows emit a data_quality_log row with
    ``kind='fmp_symbol_change_sentinel_date'`` AND do NOT produce a
    ticker_history row."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "1969-12-31",
         "companyName": "Pre-Epoch Holdings"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[],
        captured=captured,
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["rows_skipped_sentinel_date"] == 1
    assert out["ticker_history_written"] == 0
    assert out["data_quality_log_written"] == 1
    # data_quality_log row carries the kind in its notes payload.
    dql_rows = captured.get("data_quality_log", [])
    assert dql_rows, "data_quality_log INSERT must fire for sentinel-date row"
    notes_json = dql_rows[0][1]
    payload_dict = json.loads(notes_json)
    assert payload_dict["kind"] == "fmp_symbol_change_sentinel_date"


@pytest.mark.asyncio
async def test_same_cik_ticker_change_disposition(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When old_cik == new_cik (resolved via cross-walk), the stage
    runs the Option B forward-fix three-step sequence (close pre-existing
    open-ended row + rewrite ticker to oldSymbol, then INSERT a new
    open-ended row for newSymbol) instead of an additive INSERT that
    would trip the GiST EXCLUDE constraint. No new ticker_classifications
    predecessor is minted."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Same Issuer Inc"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",  # same as old
            "country": "US",
            "current_legal_name": "Same Issuer Inc",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        fetchrow_open_window={
            "valid_from": date(2008, 7, 7),
            "ticker": "NEWX",
        },
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["rows_same_cik_ticker_change"] == 1
    assert out["ticker_classifications_written"] == 0
    assert out["issuer_securities_written"] == 0
    # Option B: one UPDATE closing the open window + one INSERT for
    # the new open-ended newSymbol row.
    assert out["same_cik_window_closed"] == 1
    assert out["same_cik_current_inserted"] == 1
    # Bulk ticker_history INSERTs are NOT used on the same-CIK path.
    assert "ticker_history" not in captured
    # The Option B UPDATE carries (change_date, oldSymbol, cls).
    upd = captured.get("th_updates", [])
    assert len(upd) == 1
    assert upd[0] == (date(2020, 6, 1), "OLDX", "USSQ20FAAAAA01")
    # The new open-ended row is (cls, newSymbol, change_date).
    ins = captured.get("th_single_inserts", [])
    assert len(ins) == 1
    assert ins[0] == ("USSQ20FAAAAA01", "NEWX", date(2020, 6, 1))


@pytest.mark.asyncio
async def test_different_issuer_reuse_disposition(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When old_cik != new_cik (both resolved), the stage emits:
      * ticker_history (predecessor cls_id + oldSymbol)
      * issuer_securities (predecessor issuer_id + predecessor cls_id)
      * historical ticker_classifications row (lifetime_end non-NULL)
      * issuer row (parent FK target)
    """
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Original Delisted Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001111111", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FNEWXX01",
            "cik": "0002222222",  # different from old
            "country": "US",
            "current_legal_name": "Brand-New Registrant",
        }],
        captured=captured,
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["rows_different_issuer_reuse"] == 1
    assert out["ticker_history_written"] == 1
    assert out["ticker_classifications_written"] == 1
    assert out["issuer_securities_written"] == 1
    assert out["issuers_written"] == 1


@pytest.mark.asyncio
async def test_fmp_only_unresolved_mints_predecessor_classification(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """FMP-only row (oldSymbol not in SEC cross-walk) mints a TKR-14
    predecessor with discovery_source='F' (FMP), inserts a
    ticker_classifications row with lifetime_end = change_date, inserts
    a ticker_history row, SKIPS issuer_securities, and emits a
    data_quality_log row with kind='fmp_only_no_issuer'."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Untracked Predecessor Corp"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    # Crosswalk EMPTY → oldCIK unresolved.
    _stub_crosswalk_and_select_backend["crosswalk"] = {}

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FNEWXX01",
            "cik": "0002222222",
            "country": "US",
            "current_legal_name": "Brand-New Registrant",
        }],
        captured=captured,
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["rows_fmp_only_unresolved"] == 1
    assert out["ticker_classifications_written"] == 1
    assert out["ticker_history_written"] == 1
    assert out["issuer_securities_written"] == 0
    assert out["data_quality_log_written"] == 1

    # Inserted ticker_classifications row carries the F discovery source.
    tc_rows = captured.get("ticker_classifications", [])
    assert len(tc_rows) == 1
    inserted = tc_rows[0]
    # Schema order: id, ticker, country, cik, asset_class, ipo_venue,
    # discovery_source, status, lifetime_start, lifetime_end,
    # current_legal_name, source.
    minted_id = inserted[0]
    assert minted_id.startswith("US")  # country segment
    assert minted_id[3] == "Z"          # ipo_venue Z sentinel
    assert minted_id[6] == "F"          # discovery_source F (FMP)
    assert inserted[1] == "OLDX"
    assert inserted[3] is None           # cik unknown
    # lifetime_end is the change_date (non-NULL marker).
    assert inserted[9] == date(2020, 6, 1)

    # data_quality_log carries the fmp_only_no_issuer kind.
    dql_rows = captured.get("data_quality_log", [])
    assert dql_rows
    payload_dict = json.loads(dql_rows[0][1])
    assert payload_dict["kind"] == "fmp_only_no_issuer"


@pytest.mark.asyncio
async def test_oldsymbol_equals_newsymbol_skipped(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Per plan §3.2: rows where oldSymbol == newSymbol are skipped."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "AAA", "newSymbol": "AAA", "date": "2020-06-01",
         "companyName": "Echo Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    captured: dict[str, list] = {}
    pool = _mock_pool(tc_rows=[], captured=captured)

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["rows_skipped_same_symbol"] == 1
    # No table inserts.
    assert "ticker_history" not in captured
    assert "issuer_securities" not in captured
    assert "ticker_classifications" not in captured


@pytest.mark.asyncio
async def test_idempotent_writes_use_on_conflict_do_nothing(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Sentinel: the live INSERT statements all use
    ``ON CONFLICT ... DO NOTHING`` so reruns are safe."""
    # Source-only — the runtime test for idempotency would need a real
    # DB; the source sentinel is the deterministic proxy.
    body = _stage_body()
    # 4 idempotency clauses, one per table.
    assert body.count("ON CONFLICT") >= 4
    assert body.count("DO NOTHING") >= 4


@pytest.mark.asyncio
async def test_predecessor_lifetime_end_nonnull(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Per plan §2.3 + §5.3: every minted predecessor ticker_classifications
    row carries lifetime_end = change_date (non-NULL marker)."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Some Predecessor"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {}
    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FNEWXX01",
            "cik": "0002222222",
            "country": "US",
            "current_legal_name": "Brand-New Registrant",
        }],
        captured=captured,
    )

    await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    tc_rows = captured.get("ticker_classifications", [])
    assert tc_rows
    # lifetime_end (position 9) is non-NULL and equals change_date.
    assert tc_rows[0][9] is not None
    assert tc_rows[0][9] == date(2020, 6, 1)


# ────────────────────────────────────────────────────────────────
# Option B forward-fix tests (2026-06-02; same-CIK GiST overlap fix)
# ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_cik_ticker_change_closes_open_ended_current_window_before_insert(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Option B: the stage's same-CIK path runs UPDATE-then-INSERT in
    one transaction, NOT an additive INSERT. The UPDATE closes the
    pre-existing open-ended row (rewriting its ticker to oldSymbol)
    and the INSERT writes the new open-ended row for newSymbol."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2026-05-08",
         "companyName": "Same Issuer Inc"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2008, 7, 7), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USFZ26ODRA4870",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Same Issuer Inc",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        fetchrow_open_window={
            "valid_from": date(2008, 7, 7),
            "ticker": "NEWX",
        },
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    upd = captured.get("th_updates", [])
    assert len(upd) == 1, "Option B requires exactly one UPDATE"
    # UPDATE args: (change_date, oldSymbol, classification_id) per
    # the SET valid_to=$1, ticker=$2 WHERE classification_id=$3
    # AND valid_to IS NULL AND valid_from < $1 statement.
    assert upd[0] == (date(2026, 5, 8), "OLDX", "USFZ26ODRA4870")

    ins = captured.get("th_single_inserts", [])
    assert len(ins) == 1, "Option B requires exactly one INSERT"
    # INSERT args: (classification_id, newSymbol, change_date)
    # per the VALUES ($1, $2, $3, NULL) statement.
    assert ins[0] == ("USFZ26ODRA4870", "NEWX", date(2026, 5, 8))

    # No bulk ticker_history insert path on the same-CIK case.
    assert "ticker_history" not in captured

    # Output counters reflect the Option B path.
    assert out["same_cik_window_closed"] == 1
    assert out["same_cik_current_inserted"] == 1


@pytest.mark.asyncio
async def test_same_cik_ticker_change_does_not_violate_gist_overlap_fixture(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Replicates the live failure fixture (USFZ26ODRA4870 existing
    `[2008-07-07, infinity)` vs attempted `[2025-01-01, 2026-05-08)`).
    Option B emits UPDATE-then-INSERT — NOT a direct INSERT into a
    `[2025-01-01, 2026-05-08)` historical window that would overlap
    the open-ended row and trip ticker_history_no_overlap."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2026-05-08",
         "companyName": "USFZ Same Issuer"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2008, 7, 7), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USFZ26ODRA4870",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "USFZ Same Issuer",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        fetchrow_open_window={
            "valid_from": date(2008, 7, 7),
            "ticker": "NEWX",
        },
    )

    await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    # The bulk ticker_history insert path would have produced the
    # offending `[2025-01-01, 2026-05-08)` daterange via the planning
    # heuristic (`change_date.year - 1`); Option B never reaches that
    # bulk path.
    bulk_th = captured.get("ticker_history", [])
    assert bulk_th == [], (
        "Option B must not enqueue a direct bulk INSERT for the same-"
        "CIK case (that's the GiST overlap path that hard-failed live)"
    )

    # Instead the UPDATE-then-INSERT sequence fires.
    assert len(captured.get("th_updates", [])) == 1
    assert len(captured.get("th_single_inserts", [])) == 1


@pytest.mark.asyncio
async def test_same_cik_operation_is_transactional(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When the Option B INSERT step raises, the parent ``conn.transaction``
    aexit fires with the exception (asyncpg rolls back the UPDATE).
    The stage propagates the failure rather than silently dropping
    the same-CIK case."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Failing Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Failing Co",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        fetchrow_open_window={
            "valid_from": date(2008, 7, 7),
            "ticker": "NEWX",
        },
        insert_failure_table="platform.ticker_history",
    )

    with pytest.raises(RuntimeError, match="simulated INSERT failure"):
        await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
            "dry_run": False,
            "force_download": True,
            "local_cache_path": str(tmp_path / "cache.json.gz"),
            "manifest_path": str(tmp_path / "m.csv"),
        })

    # The UPDATE fired BEFORE the INSERT raised. The transactional
    # rollback is the responsibility of asyncpg's ``conn.transaction``
    # context manager — the test asserts the failure propagates so
    # the operator sees a hard stop, not a silent half-write.
    assert len(captured.get("th_updates", [])) == 1
    assert len(captured.get("th_single_inserts", [])) == 1


@pytest.mark.asyncio
async def test_same_cik_operation_is_idempotent_on_second_run(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When the substrate already reflects the Option B post-state —
    the open-ended row's ``valid_from == change_date`` AND
    ``ticker == newSymbol`` — the stage SKIPS both UPDATE and INSERT
    (silent re-run no-op). The counter
    ``same_cik_already_applied_skipped`` reflects the skip."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Already Applied Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Already Applied Co",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        # Post-Option-B state: open-ended row is the NEW one at
        # change_date.
        fetchrow_open_window={
            "valid_from": date(2020, 6, 1),
            "ticker": "NEWX",
        },
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert captured.get("th_updates", []) == []
    assert captured.get("th_single_inserts", []) == []
    assert out["same_cik_already_applied_skipped"] == 1
    assert out["same_cik_window_closed"] == 0
    assert out["same_cik_current_inserted"] == 0


@pytest.mark.asyncio
async def test_same_cik_unresolvable_window_emits_data_quality_log(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """When the pre-existing open-ended row's ``valid_from > change_date``
    (the ticker change pre-dates the row's start — an unresolvable
    temporal conflict), the stage emits a ``data_quality_log`` row
    with ``kind='same_cik_window_pre_dates_change'`` and SKIPS the
    UPDATE/INSERT."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2026-05-08",
         "companyName": "Conflict Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Conflict Co",
            "lifetime_start": date(2027, 1, 1),
        }],
        captured=captured,
        # Conflict: pre-existing row's valid_from POST-dates the
        # change_date.
        fetchrow_open_window={
            "valid_from": date(2027, 1, 1),
            "ticker": "NEWX",
        },
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert captured.get("th_updates", []) == []
    assert captured.get("th_single_inserts", []) == []
    dql = captured.get("dql_single_inserts", [])
    assert len(dql) == 1
    notes_json = dql[0][1]
    payload_dict = json.loads(notes_json)
    assert payload_dict["kind"] == "same_cik_window_pre_dates_change"
    assert out["same_cik_pre_dates_change_skipped"] == 1


@pytest.mark.asyncio
async def test_different_issuer_reuse_path_unchanged(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """Option B fix is bounded to the same-CIK path. The different-
    issuer reuse case still uses the bulk additive INSERT into
    ``ticker_history`` (a NEW predecessor classification_id has no
    pre-existing rows, so no overlap risk) and does NOT emit any
    UPDATE."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Original Delisted Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001111111", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FNEWXX01",
            "cik": "0002222222",  # different from old
            "country": "US",
            "current_legal_name": "Brand-New Registrant",
            "lifetime_start": date(2019, 1, 1),
        }],
        captured=captured,
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": False,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    # The bulk additive ticker_history insert path is used.
    assert len(captured.get("ticker_history", [])) == 1
    # The Option B per-row UPDATE path is NOT used.
    assert captured.get("th_updates", []) == []
    # The same-CIK counters stay at zero.
    assert out["same_cik_window_closed"] == 0
    assert out["same_cik_current_inserted"] == 0


@pytest.mark.asyncio
async def test_dry_run_does_not_update_valid_to(
    _stub_crosswalk_and_select_backend, tmp_path,
):
    """In dry_run mode the same-CIK path plans the close but
    issues NO ``UPDATE platform.ticker_history`` statement and NO
    new-current ``INSERT``. The plan/manifest still records the
    intent."""
    from scripts import ops as _ops_mod
    payload = _gzipped_json([
        {"oldSymbol": "OLDX", "newSymbol": "NEWX", "date": "2020-06-01",
         "companyName": "Dry Run Co"},
    ])

    async def _do_download(**_kw: Any) -> bytes:
        return payload

    _ops_mod._fmp_symbol_change_download = _do_download  # type: ignore[assignment]

    _stub_crosswalk_and_select_backend["crosswalk"] = {
        "OLDX": [("0001234567", date(2010, 1, 1), None)],
    }

    captured: dict[str, list] = {}
    pool = _mock_pool(
        tc_rows=[{
            "ticker": "NEWX",
            "classification_id": "USSQ20FAAAAA01",
            "cik": "0001234567",
            "country": "US",
            "current_legal_name": "Dry Run Co",
            "lifetime_start": date(2008, 7, 7),
        }],
        captured=captured,
        fetchrow_open_window={
            "valid_from": date(2008, 7, 7),
            "ticker": "NEWX",
        },
    )

    out = await _ops_mod._stage_symbol_history_evidence_backfill(pool, {
        "dry_run": True,
        "force_download": True,
        "local_cache_path": str(tmp_path / "cache.json.gz"),
        "manifest_path": str(tmp_path / "m.csv"),
    })

    assert out["dry_run"] is True
    # Dry-run is the planning-only path; no UPDATEs / INSERTs hit
    # the connection.
    assert captured.get("th_updates", []) == []
    assert captured.get("th_single_inserts", []) == []
    # Planning counter still reflects the intent.
    assert out["same_cik_window_close_planned"] == 1


def test_fundamentals_quarterly_untouched_source_sentinel() -> None:
    """Forward-fix scope guard: the stage source contains zero
    ``fundamentals_quarterly`` references in executable code (the
    docstring's non-goal-list mention is allowed). Ensures the
    Option B PR keeps strict scope; cleanup is a SEPARATE PR."""
    import re as _re
    body = _stage_body()
    body_no_doc = _re.sub(
        r'""".*?"""', "", body, count=1, flags=_re.DOTALL,
    )
    assert "fundamentals_quarterly" not in body_no_doc, (
        "symbol_history_evidence_backfill stage body must not reference "
        "fundamentals_quarterly in executable code"
    )


def test_plan_doc_corrects_ticker_history_pk_claim() -> None:
    """Plan §5.1 amendment: the natural-key claim was empirically
    wrong in the spec PR. The actual schema declares a 2-col PK
    ``(classification_id, valid_from)`` + GiST EXCLUDE
    ``ticker_history_no_overlap``. The amended plan must reflect
    this and call the correction out."""
    from pathlib import Path as _P
    plan_path = (
        _P(__file__).resolve().parents[1]
        / "docs" / "superpowers" / "plans"
        / "2026-06-02-symbol-history-evidence-backfill-plan.md"
    )
    text = plan_path.read_text(encoding="utf-8")
    # 2-col PK is named explicitly.
    assert "(classification_id, valid_from)" in text
    # GiST EXCLUDE is named explicitly.
    assert "ticker_history_no_overlap" in text
    assert "EXCLUDE USING gist" in text
    # Correction is acknowledged so a future grooming pass can't
    # silently revert.
    assert (
        "Schema-audited correction" in text
        or "schema-audited correction" in text.lower()
    )
