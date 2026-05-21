"""Pluggable backends for ``tpcore.ingestion.csv_archive`` — R3 seam.

The CSV-first archive is the canonical recovery artifact for
catastrophic DB loss; on a persistent local FS (today's Mac default)
the original byte-for-byte behaviour is preserved, but a single env
var (``CSV_ARCHIVE_BACKEND=s3``) re-routes write/read through an
S3-compatible object store so the eventual Railway move is config,
not a refactor.

Design ref: ``docs/memory/project_railway_archive_substrate_migration.md``
(R3 = recovery substrate moves to an attached S3-compatible bucket;
this module ships the SEAM; the actual data move is a one-shot
operator action at Railway-migration time). D2 = detection moves to
Postgres rolling-median, separate PR.

Why ``minio`` over ``boto3``:
    * boto3 brings the full botocore + awscli-family transitive tree
      (urllib3, jmespath, s3transfer, …); a 10MB+ runtime install.
    * ``minio`` is a thin S3-protocol client (single package, no aws-*
      sub-deps) that natively accepts ``endpoint=`` so it works against
      ANY S3-compatible service: Railway's native object store, R2,
      Supabase Storage, MinIO, AWS S3 itself.
    * Operator's R3 line is literally "S3-compatible object-storage
      bucket attached to the service" — that's minio's exact sweet
      spot.

Why a sync (NOT async) Protocol:
    Every existing call-site in ``handlers.py``, ``ops.py``, the
    audit scripts, and the validation handlers is synchronous. The
    archive write is microseconds for the local backend and well
    under the 1-minute light-stage timeout for the S3 backend (a
    1.4 GB archive uploaded over a Railway-internal bucket peer link
    is bounded; the per-handler write is < 10 MB). Forcing async
    here would break ~14 call sites in handlers.py + dump scripts
    for no measurable concurrency benefit — the operator-directive
    "we aren't refactoring the fuck out of it" maps exactly to "keep
    the sync signature".
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

# ─── Protocol ────────────────────────────────────────────────────────────


class ArchiveBackend(Protocol):
    """Sync backend protocol — see module docstring for the why-sync.

    All methods raise on unrecoverable errors so callers (which already
    log + write ``INGESTION_FAILED`` to ``platform.application_log``)
    can surface the failure through the existing path.
    """

    def write(self, source: str, body: bytes, filename: str) -> str:
        """Write ``body`` to ``<source>_archive/<filename>``.

        Returns a backend-specific identifier (filesystem path for
        local, ``s3://bucket/key`` URI for S3) suitable for logging
        + propagating into ``ArchiveWriteResult.path``.
        """
        ...

    def read(self, source: str, filename: str) -> bytes:
        """Return the bytes of ``<source>_archive/<filename>``.

        Raises ``FileNotFoundError`` on miss.
        """
        ...

    def read_latest(self, source: str) -> bytes | None:
        """Return the bytes of the most-recent archive for ``source``,
        or ``None`` if no archives exist.
        """
        ...

    def list_archives(self, source: str) -> list[str]:
        """Return archive filenames (NOT full paths) for ``source``,
        sorted ascending by name (so the timestamp-suffixed names sort
        chronologically by construction).
        """
        ...


# ─── LocalFSBackend ──────────────────────────────────────────────────────


class LocalFSBackend:
    """Filesystem backend — byte-identical to the prior behaviour.

    Honours the already-shipped ``TP_DATA_DIR`` env override (the
    PR #76 seam). Default root is ``<repo_root>/data`` — unchanged.
    """

    def _root(self) -> Path:
        # Defer to ``csv_archive.repo_data_dir`` so the existing
        # ``TP_DATA_DIR`` env seam AND the existing test-fixture
        # monkeypatch contract (``monkeypatch.setattr(csv_archive,
        # "repo_data_dir", lambda: tmp_path)`` — used by every
        # csv_archive test + the audit-script tests + the
        # handler-end-to-end tests) keep working unchanged. Single
        # source of truth: csv_archive.repo_data_dir is the canonical
        # function — the backend is a thin re-router, not a
        # competing root-resolver.
        from tpcore.ingestion.csv_archive import repo_data_dir  # noqa: PLC0415
        return repo_data_dir()

    def _archive_dir(self, source: str) -> Path:
        d = self._root() / f"{source}_archive"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write(self, source: str, body: bytes, filename: str) -> str:
        target = self._archive_dir(source) / filename
        target.write_bytes(body)
        return str(target)

    def read(self, source: str, filename: str) -> bytes:
        target = self._archive_dir(source) / filename
        if not target.exists():
            raise FileNotFoundError(str(target))
        return target.read_bytes()

    def read_latest(self, source: str) -> bytes | None:
        names = self.list_archives(source)
        if not names:
            return None
        return self.read(source, names[-1])

    def list_archives(self, source: str) -> list[str]:
        d = self._archive_dir(source)
        return sorted(p.name for p in d.glob(f"{source}_*.csv.gz"))


# ─── S3Backend ───────────────────────────────────────────────────────────


def _minio_client_class():
    """Return the minio.Minio class. Indirected so tests can substitute
    a stub WITHOUT importing the real minio package.

    Lazy-imports ``minio`` only when the S3 backend is actually
    selected — keeps the local-default path free of the (otherwise
    unused) dependency at import time.
    """
    from minio import Minio  # noqa: PLC0415 — intentional lazy import
    return Minio


class S3Backend:
    """S3-compatible bucket backend via the ``minio`` client.

    Reads connection params from env at construction:
        * ``CSV_ARCHIVE_S3_ENDPOINT`` — host:port (no scheme; minio
          adds it via ``secure=`` flag below).
        * ``CSV_ARCHIVE_S3_BUCKET`` — destination bucket.
        * ``CSV_ARCHIVE_S3_KEY_ID`` / ``CSV_ARCHIVE_S3_SECRET`` —
          access credentials.
        * ``CSV_ARCHIVE_S3_REGION`` — optional region hint
          (default: blank, fine for non-AWS S3-compatibles).
        * ``CSV_ARCHIVE_S3_SECURE`` — "true"/"false"; default "true"
          (set "false" only for an in-cluster MinIO over plain HTTP).

    Object layout mirrors LocalFSBackend's directory layout:
    ``<bucket>/<source>_archive/<filename>``. The ``/`` is logical in
    S3 (object names are flat) but every S3-compatible UI / CLI / SDK
    treats it as a folder separator, so the operator's mental model
    stays the same.

    Returns ``s3://<bucket>/<source>_archive/<filename>`` URIs from
    ``write()`` — host-agnostic, machine-readable, and the standard
    representation across aws-cli / rclone / minio-mc.
    """

    def __init__(self) -> None:
        endpoint = os.environ.get("CSV_ARCHIVE_S3_ENDPOINT", "").strip()
        bucket = os.environ.get("CSV_ARCHIVE_S3_BUCKET", "").strip()
        key_id = os.environ.get("CSV_ARCHIVE_S3_KEY_ID", "").strip()
        secret = os.environ.get("CSV_ARCHIVE_S3_SECRET", "").strip()
        region = os.environ.get("CSV_ARCHIVE_S3_REGION", "").strip() or None
        secure = os.environ.get("CSV_ARCHIVE_S3_SECURE", "true").lower() != "false"

        missing = [
            name for name, val in (
                ("CSV_ARCHIVE_S3_ENDPOINT", endpoint),
                ("CSV_ARCHIVE_S3_BUCKET", bucket),
                ("CSV_ARCHIVE_S3_KEY_ID", key_id),
                ("CSV_ARCHIVE_S3_SECRET", secret),
            ) if not val
        ]
        if missing:
            raise RuntimeError(
                f"S3Backend: missing required env vars: {', '.join(missing)}. "
                "See docs/OPERATIONS.md §archive-substrate for the full list."
            )

        self._bucket = bucket
        # Construct the client through the seam so tests can substitute.
        minio_cls = _minio_client_class()
        self._client = minio_cls(
            endpoint=endpoint,
            access_key=key_id,
            secret_key=secret,
            region=region,
            secure=secure,
        )

    def _object_name(self, source: str, filename: str) -> str:
        return f"{source}_archive/{filename}"

    def _uri(self, object_name: str) -> str:
        return f"s3://{self._bucket}/{object_name}"

    def write(self, source: str, body: bytes, filename: str) -> str:
        import io  # noqa: PLC0415 — tiny stdlib import in hot path is fine
        object_name = self._object_name(source, filename)
        # minio.put_object requires a stream + explicit length. Passing
        # raw bytes is rejected at runtime by some minio versions, hence
        # the explicit BytesIO wrapper (and the regression-pin test).
        stream = io.BytesIO(body)
        self._client.put_object(
            self._bucket, object_name, stream, len(body),
            content_type="application/gzip",
        )
        return self._uri(object_name)

    def read(self, source: str, filename: str) -> bytes:
        object_name = self._object_name(source, filename)
        resp = self._client.get_object(self._bucket, object_name)
        try:
            # minio response is a urllib3.HTTPResponse-shaped object;
            # ``.read()`` is the canonical buffered read, ``.data`` is
            # available on the in-test fake. Prefer read() for real
            # client behaviour.
            if hasattr(resp, "read"):
                return resp.read()
            return bytes(resp.data)  # type: ignore[attr-defined]
        finally:
            close_fn = getattr(resp, "close", None)
            if callable(close_fn):
                close_fn()
            release_fn = getattr(resp, "release_conn", None)
            if callable(release_fn):
                release_fn()

    def read_latest(self, source: str) -> bytes | None:
        names = self.list_archives(source)
        if not names:
            return None
        return self.read(source, names[-1])

    def list_archives(self, source: str) -> list[str]:
        prefix = f"{source}_archive/"
        names: list[str] = []
        for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
            full = obj.object_name
            if not full.endswith(".csv.gz"):
                continue
            # Strip the bucket-prefix so callers see filename-only,
            # matching the LocalFSBackend contract.
            name = full[len(prefix):]
            if name:  # guard against the prefix-itself entry some S3s emit
                names.append(name)
        return sorted(names)


# ─── Selector ────────────────────────────────────────────────────────────


def select_backend() -> ArchiveBackend:
    """Return the backend selected by ``CSV_ARCHIVE_BACKEND`` env.

    * Unset / empty / ``"local"`` → ``LocalFSBackend`` (the
      byte-identical default — every existing test passes unchanged).
    * ``"s3"`` → ``S3Backend`` (constructs with required env, raises
      ``RuntimeError`` if any required var is missing).
    * Anything else → ``ValueError`` (no silent fallback; an unknown
      value is operator error and the operator must see it).
    """
    choice = os.environ.get("CSV_ARCHIVE_BACKEND", "").strip().lower()
    if choice in ("", "local"):
        return LocalFSBackend()
    if choice == "s3":
        return S3Backend()
    raise ValueError(
        f"unknown CSV_ARCHIVE_BACKEND={choice!r}; "
        "expected one of: '' (default → local), 'local', 's3'"
    )


__all__ = [
    "ArchiveBackend",
    "LocalFSBackend",
    "S3Backend",
    "select_backend",
]
