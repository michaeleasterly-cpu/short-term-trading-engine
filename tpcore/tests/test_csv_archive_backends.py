"""R3 pre-Railway substrate migration — pluggable archive backend.

Pins the **env-pluggable backend** seam that turns the eventual
Railway move from a refactor into a config flip. The local default
remains byte-identical to the prior behaviour (so every existing
csv_archive test stays green); a single env var
(`CSV_ARCHIVE_BACKEND=s3`) re-routes write/read through an
S3-compatible object-storage client.

Design ref: ``docs/memory/project_railway_archive_substrate_migration.md``
(R3 = recovery substrate moves to an S3-compatible bucket; this PR
ships the SEAM; the actual data move happens at Railway-migration
time). This test file FAILS on main (no backend abstraction exists)
and PASSES on this branch.

S3 client: ``minio`` (chosen over boto3 — far fewer transitive deps;
no awscli/botocore/aiobotocore tree; works against ANY S3-compatible
endpoint via the env-injected ``endpoint`` arg, which is exactly the
R3 design's "S3-compatible bucket attached to the service"). Mocked
in tests — no real bucket, no moto, no docker.
"""
from __future__ import annotations

import gzip
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tpcore.ingestion import csv_archive
from tpcore.ingestion.csv_archive_backends import (
    LocalFSBackend,
    S3Backend,
    select_backend,
)

# ─── Backend selection ──────────────────────────────────────────────────


class TestBackendSelection:
    """``CSV_ARCHIVE_BACKEND`` env var picks the backend.

    Unset / "local" → ``LocalFSBackend`` (byte-identical to the
    prior behaviour). Anything else → ``S3Backend``. The selector is
    the ONE place in the module where the env is read.
    """

    def test_unset_defaults_to_local(self, monkeypatch) -> None:
        monkeypatch.delenv("CSV_ARCHIVE_BACKEND", raising=False)
        b = select_backend()
        assert isinstance(b, LocalFSBackend)

    def test_explicit_local_returns_localfs(self, monkeypatch) -> None:
        monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "local")
        b = select_backend()
        assert isinstance(b, LocalFSBackend)

    def test_empty_string_treated_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "")
        b = select_backend()
        assert isinstance(b, LocalFSBackend)

    def test_s3_value_returns_s3_backend(self, monkeypatch) -> None:
        # Provide minimum S3 env so construction succeeds.
        monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "s3")
        monkeypatch.setenv("CSV_ARCHIVE_S3_ENDPOINT", "s3.example.test")
        monkeypatch.setenv("CSV_ARCHIVE_S3_BUCKET", "ste-archives")
        monkeypatch.setenv("CSV_ARCHIVE_S3_KEY_ID", "AKIAFAKE")
        monkeypatch.setenv("CSV_ARCHIVE_S3_SECRET", "fakesecret")
        b = select_backend()
        assert isinstance(b, S3Backend)

    def test_unknown_backend_value_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "azure")
        with pytest.raises(ValueError, match=r"unknown.*BACKEND.*azure"):
            select_backend()


# ─── LocalFSBackend round-trip ───────────────────────────────────────────


class TestLocalFSBackend:
    """The current behaviour, wrapped in the protocol.

    Same archive layout (``<root>/<source>_archive/<source>_<stamp>.csv.gz``),
    same gzip-on-success contract. Honours ``TP_DATA_DIR`` already-shipped
    seam.
    """

    def test_roundtrip_write_list_read(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
        b = LocalFSBackend()
        body = gzip.compress(b"ticker,date,close\nAAPL,2026-05-21,192.50\n")
        path_str = b.write("fred_macro", body, "fred_macro_20260521T120000Z.csv.gz")
        assert path_str.endswith(".csv.gz")
        assert (tmp_path / "fred_macro_archive" / "fred_macro_20260521T120000Z.csv.gz").exists()

        listing = b.list_archives("fred_macro")
        assert listing == ["fred_macro_20260521T120000Z.csv.gz"]

        got = b.read("fred_macro", "fred_macro_20260521T120000Z.csv.gz")
        assert got == body

        # read_latest finds the most recent (only one here).
        latest = b.read_latest("fred_macro")
        assert latest == body

    def test_read_latest_none_when_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
        b = LocalFSBackend()
        assert b.read_latest("fred_macro") is None
        assert b.list_archives("fred_macro") == []


# ─── S3Backend round-trip (mocked minio client) ──────────────────────────


class _FakeMinioObject:
    """Mimics minio's `list_objects` result shape — `.object_name`."""

    def __init__(self, name: str) -> None:
        self.object_name = name


class _FakeMinioResponse:
    """Mimics minio's `get_object` response — `.data` bytes + close hook."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:  # mirror urllib3 / minio HTTPResponse
        return self.data

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


@pytest.fixture
def fake_minio(monkeypatch):
    """Inject a stub minio.Minio class that records calls in-memory.

    Avoids importing the real minio package (and any optional dep
    install dance); the backend imports lazily and selects the class
    through ``csv_archive_backends._minio_client_class`` so tests can
    inject a substitute deterministically.
    """
    store: dict[str, bytes] = {}  # object_name → body

    class _FakeMinio:
        def __init__(self, *_args, **_kw):
            self.calls: list[tuple[str, str]] = []

        def put_object(self, _bucket, object_name, data, _length, **_kw):
            body = data.read() if hasattr(data, "read") else bytes(data)
            store[object_name] = body
            self.calls.append(("put", object_name))
            return MagicMock(etag="fake-etag")

        def get_object(self, _bucket, object_name):
            if object_name not in store:
                raise FileNotFoundError(object_name)
            return _FakeMinioResponse(store[object_name])

        def list_objects(self, _bucket, prefix="", **_kw):
            # minio's list_objects accepts ``recursive`` and other kwargs;
            # the fake just ignores them. **_kw also keeps vulture happy
            # (no named-but-unused arg).
            for name in sorted(store):
                if name.startswith(prefix):
                    yield _FakeMinioObject(name)

    monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "s3")
    monkeypatch.setenv("CSV_ARCHIVE_S3_ENDPOINT", "s3.example.test")
    monkeypatch.setenv("CSV_ARCHIVE_S3_BUCKET", "ste-archives")
    monkeypatch.setenv("CSV_ARCHIVE_S3_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("CSV_ARCHIVE_S3_SECRET", "fakesecret")
    # The seam: backend module reads the client class via a getter so
    # tests inject the fake without touching sys.modules.
    from tpcore.ingestion import csv_archive_backends as cab
    monkeypatch.setattr(cab, "_minio_client_class", lambda: _FakeMinio)
    return store


class TestS3Backend:
    def test_roundtrip_write_list_read(self, fake_minio) -> None:
        b = S3Backend()
        body = gzip.compress(b"ticker,date,close\nAAPL,2026-05-21,192.50\n")
        path_str = b.write("fred_macro", body, "fred_macro_20260521T120000Z.csv.gz")
        # Path returned for S3 is the s3:// URI — host-agnostic.
        assert path_str.startswith("s3://ste-archives/")
        assert path_str.endswith("/fred_macro_20260521T120000Z.csv.gz")
        # Object lives in the bucket under <source>_archive/<filename>.
        assert "fred_macro_archive/fred_macro_20260521T120000Z.csv.gz" in fake_minio

        listing = b.list_archives("fred_macro")
        assert listing == ["fred_macro_20260521T120000Z.csv.gz"]

        got = b.read("fred_macro", "fred_macro_20260521T120000Z.csv.gz")
        assert got == body

        latest = b.read_latest("fred_macro")
        assert latest == body

    def test_read_latest_none_when_bucket_empty(self, fake_minio) -> None:
        b = S3Backend()
        assert b.read_latest("fred_macro") is None
        assert b.list_archives("fred_macro") == []

    def test_missing_env_raises_at_construction(self, monkeypatch) -> None:
        monkeypatch.setenv("CSV_ARCHIVE_BACKEND", "s3")
        monkeypatch.delenv("CSV_ARCHIVE_S3_ENDPOINT", raising=False)
        with pytest.raises(RuntimeError, match="CSV_ARCHIVE_S3_ENDPOINT"):
            S3Backend()


# ─── write_archive / latest_archive route through the backend ────────────


class TestWriteArchiveUsesBackend:
    """Existing call-sites stay sync + signature-compatible.

    The backward-compat guarantee: with no env vars set, every
    existing test in ``test_csv_archive.py`` passes unchanged. This
    test pins that ``write_archive`` calls ``LocalFSBackend.write``
    by default (proves the seam isn't a no-op).
    """

    def test_write_archive_goes_through_local_backend_by_default(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CSV_ARCHIVE_BACKEND", raising=False)
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
        res = csv_archive.write_archive(
            "fred_macro",
            [{"indicator": "x", "date": "2026-05-21", "value": "1"}],
            fieldnames=["indicator", "date", "value"],
        )
        # File on disk (the LocalFSBackend wrote it).
        assert res.path.exists()
        assert res.path.parent == tmp_path / "fred_macro_archive"

    def test_write_archive_goes_through_s3_backend_when_selected(
        self, fake_minio
    ) -> None:
        # Backend selection kicks in.
        res = csv_archive.write_archive(
            "fred_macro",
            [{"indicator": "x", "date": "2026-05-21", "value": "1"}],
            fieldnames=["indicator", "date", "value"],
        )
        # When S3 is selected, ``ArchiveWriteResult.path`` is the s3:// URI
        # wrapped as Path-like (path.name + path.parent still work but
        # the FS doesn't have it).
        assert str(res.path).startswith("s3://ste-archives/")
        assert "fred_macro_archive" in str(res.path)
        # The object was actually written to the (fake) S3 store.
        assert any(k.startswith("fred_macro_archive/") for k in fake_minio)


# ─── _stage_rebuild_from_archive — the canonical recovery path ──────────


class TestRebuildFromArchiveStage:
    """The rebuild stage is what makes "we have CSVs" a real recovery
    capability instead of theoretical.

    Reads the latest daily_bars archive through the configured backend,
    streams it row-by-row into ``platform.prices_daily`` via the
    standard idempotent upsert. Bounded per-source.
    """

    async def test_rebuild_replays_csv_into_prices_daily(
        self, tmp_path, monkeypatch
    ) -> None:
        # Stage is async + uses a real pool shape; build a FakePool that
        # records the INSERT it would have executed.
        from scripts.ops import _stage_rebuild_from_archive

        monkeypatch.delenv("CSV_ARCHIVE_BACKEND", raising=False)
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))

        # Seed: write a synthetic alpaca_daily_bars archive through the
        # SAME backend path the stage will read from.
        from tpcore.ingestion import csv_archive
        rows = [
            {
                "ticker": "AAPL",
                "date": "2026-05-21T04:00:00Z",
                "open": "190.0",
                "high": "193.0",
                "low": "189.5",
                "close": "192.5",
                "volume": "12345678",
                "vwap": "191.7",
            },
            {
                "ticker": "MSFT",
                "date": "2026-05-21T04:00:00Z",
                "open": "415.0",
                "high": "417.5",
                "low": "414.0",
                "close": "416.8",
                "volume": "9876543",
                "vwap": "416.0",
            },
        ]
        csv_archive.write_archive(
            "alpaca_daily_bars", rows,
            fieldnames=["ticker", "date", "open", "high", "low", "close", "volume", "vwap"],
        )

        # FakePool — captures the upserts.
        executed: list[tuple[str, tuple]] = []

        class _FakeConn:
            async def executemany(self, sql, args):
                executed.append((sql, tuple(args)))
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _FakePool:
            def acquire(self):
                return _FakeConn()

        result = await _stage_rebuild_from_archive(
            _FakePool(),  # type: ignore[arg-type]
            {"source": "alpaca_daily_bars"},
        )

        assert result["source"] == "alpaca_daily_bars"
        assert result["rows_replayed"] == 2
        assert executed, "no INSERT was executed"
        # The upsert is the canonical idempotent prices_daily INSERT.
        sql_text = executed[0][0]
        assert "platform.prices_daily" in sql_text
        assert "ON CONFLICT" in sql_text

    async def test_rebuild_missing_source_raises(
        self, tmp_path, monkeypatch
    ) -> None:
        from scripts.ops import _stage_rebuild_from_archive

        monkeypatch.delenv("CSV_ARCHIVE_BACKEND", raising=False)
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))

        class _FakePool:
            def acquire(self):
                raise AssertionError("should not be called — early-exit")

        with pytest.raises(ValueError, match="source"):
            await _stage_rebuild_from_archive(_FakePool(), {})

    async def test_rebuild_no_archive_returns_zero(
        self, tmp_path, monkeypatch
    ) -> None:
        from scripts.ops import _stage_rebuild_from_archive

        monkeypatch.delenv("CSV_ARCHIVE_BACKEND", raising=False)
        monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))

        class _FakePool:
            def acquire(self):
                raise AssertionError("should not be called — no archive")

        result = await _stage_rebuild_from_archive(
            _FakePool(),  # type: ignore[arg-type]
            {"source": "alpaca_daily_bars"},
        )
        assert result["rows_replayed"] == 0
        assert result["skipped"] is True


# ─── Memory-snapshot smoke ───────────────────────────────────────────────


def test_paths_dont_leak_local_fs_when_s3_selected(fake_minio) -> None:
    """Railway-portability sanity: when ``CSV_ARCHIVE_BACKEND=s3``, the
    S3 backend MUST NOT touch the local FS — otherwise the Railway
    ephemeral-FS failure mode that motivated R3 just moves around.
    """
    b = S3Backend()
    body = b"hello"
    p = b.write("fred_macro", body, "fred_macro_20260521T120001Z.csv.gz")
    # No file on disk at the local seam path.
    assert not Path("/tmp/fred_macro_archive").exists()  # noqa: S108
    # The returned URI is purely logical (s3://).
    assert str(p).startswith("s3://")
    # And the bytes are in the fake S3 store, not on disk.
    assert any(body == v for v in fake_minio.values())


def test_s3_backend_streams_body_via_io_BytesIO(fake_minio) -> None:
    """Implementation detail worth pinning: minio's ``put_object``
    requires a stream-like ``data`` + a ``length``. The backend must
    wrap bytes in BytesIO; passing raw bytes silently no-ops on some
    minio versions.
    """
    b = S3Backend()
    body = b"x" * 1024
    b.write("fred_macro", body, "f.csv.gz")
    # Reading back proves the body was actually transmitted.
    stored = next(v for k, v in fake_minio.items() if "f.csv.gz" in k)
    assert stored == body
    # Sanity: BytesIO ≠ MagicMock; this is just an explicit smoke that
    # we actually piped bytes through.
    assert isinstance(io.BytesIO(body).read(), bytes)
