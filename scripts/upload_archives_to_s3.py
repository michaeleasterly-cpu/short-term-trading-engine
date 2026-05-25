"""One-shot bulk-upload of the local CSV archive corpus to an
S3-compatible bucket — pre-Railway-cutover seeding.

The R3 archive substrate (`tpcore/ingestion/csv_archive_backends.py`)
is env-pluggable: `CSV_ARCHIVE_BACKEND=s3` flips reads/writes from
local FS to S3. Before flipping that env var on the Railway service,
the existing local archive corpus must already be in the bucket so
historical-replay paths (manifest replay, splits diff) keep working.

This script walks the local archive directories under
`data/<source>_archive/` (or `TP_DATA_DIR/<source>_archive/`) and
uploads every `.csv.zst` + `.csv.gz` + `.csv` file to the bucket
under a parallel key prefix. Idempotent: skips objects already in
the bucket with matching content-length + SHA-256 (if a sidecar
`.sha256` file is present in the local tree, the script uses it
directly; otherwise it computes the hash from the local file).

Usage:
    python scripts/upload_archives_to_s3.py --dry-run
    python scripts/upload_archives_to_s3.py --commit

Env requirements (read from .env or shell):
    CSV_ARCHIVE_S3_ENDPOINT
    CSV_ARCHIVE_S3_BUCKET
    CSV_ARCHIVE_S3_KEY_ID
    CSV_ARCHIVE_S3_SECRET
    TP_DATA_DIR (optional; defaults to the repo's data/ dir)

Exit codes:
    0 — all files uploaded (or already present); manifest dumped to stdout
    1 — config error (missing env, unreachable bucket)
    2 — partial failure (some uploads errored); per-file errors on stderr
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError
except ImportError:
    print(
        "ERROR: boto3 not installed in this environment. "
        "Run `.venv/bin/pip install boto3` first.",
        file=sys.stderr,
    )
    sys.exit(1)


_ARCHIVE_SUFFIXES: tuple[str, ...] = (".csv.zst", ".csv.gz", ".csv")


@dataclass(frozen=True)
class UploadPlan:
    local_path: Path
    s3_key: str
    size_bytes: int
    sha256: str


def _data_dir() -> Path:
    """Resolve the archive root the same way csv_archive does."""
    env = os.environ.get("TP_DATA_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1] / "data"


def _walk_archives(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.endswith("_archive"):
            continue
        for path in sorted(child.rglob("*")):
            if path.is_file() and any(
                path.name.endswith(suf) for suf in _ARCHIVE_SUFFIXES
            ):
                yield path


def _sha256(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        return sidecar.read_text().strip().split()[0]
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_plan(root: Path) -> list[UploadPlan]:
    plan: list[UploadPlan] = []
    for path in _walk_archives(root):
        rel = path.relative_to(root).as_posix()
        plan.append(
            UploadPlan(
                local_path=path,
                s3_key=rel,
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
            )
        )
    return plan


def _already_uploaded(client, bucket: str, plan: UploadPlan) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=plan.s3_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise
    if head["ContentLength"] != plan.size_bytes:
        return False
    remote_sha = head.get("Metadata", {}).get("sha256", "")
    return remote_sha == plan.sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="actually upload (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit no-op (default behavior)")
    parser.add_argument("--limit", type=int, default=0, help="cap files (debug)")
    args = parser.parse_args(argv)

    if args.commit and args.dry_run:
        print("ERROR: --commit and --dry-run are mutually exclusive", file=sys.stderr)
        return 1

    endpoint = os.environ.get("CSV_ARCHIVE_S3_ENDPOINT", "").strip()
    bucket = os.environ.get("CSV_ARCHIVE_S3_BUCKET", "").strip()
    key_id = os.environ.get("CSV_ARCHIVE_S3_KEY_ID", "").strip()
    secret = os.environ.get("CSV_ARCHIVE_S3_SECRET", "").strip()
    if not all([endpoint, bucket, key_id, secret]):
        print(
            "ERROR: set CSV_ARCHIVE_S3_ENDPOINT / _BUCKET / _KEY_ID / _SECRET "
            "in .env or shell.",
            file=sys.stderr,
        )
        return 1
    # The codebase's minio-based S3Backend (tpcore/ingestion/csv_archive_backends.py)
    # expects host:port without scheme; boto3 wants a full URL. Normalize so the
    # env-var SoT can stay scheme-less and this script still works.
    if "://" not in endpoint:
        endpoint = f"https://{endpoint}"

    root = _data_dir()
    print(f"Walking archive root: {root}")
    plan = _build_plan(root)
    if args.limit:
        plan = plan[: args.limit]
    total_bytes = sum(p.size_bytes for p in plan)
    print(f"Found {len(plan)} archive files ({total_bytes:_} bytes total)")

    if not args.commit:
        print("DRY-RUN — pass --commit to upload. Sample (up to 10):")
        for p in plan[:10]:
            print(f"  {p.size_bytes:>12_}  {p.sha256[:16]}…  {p.s3_key}")
        return 0

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        config=BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            s3={"addressing_style": "virtual"},
            signature_version="s3v4",
        ),
    )

    uploaded = skipped = errored = 0
    for p in plan:
        try:
            if _already_uploaded(client, bucket, p):
                skipped += 1
                continue
            with p.local_path.open("rb") as fh:
                client.put_object(
                    Bucket=bucket,
                    Key=p.s3_key,
                    Body=fh,
                    Metadata={"sha256": p.sha256},
                )
            uploaded += 1
            if uploaded % 50 == 0:
                print(f"  …{uploaded} uploaded, {skipped} skipped, {errored} errored")
        except (ClientError, OSError) as exc:
            errored += 1
            print(f"ERROR uploading {p.s3_key}: {exc}", file=sys.stderr)

    print(
        f"DONE — uploaded={uploaded} skipped={skipped} errored={errored} "
        f"total={len(plan)} bucket={bucket}"
    )
    return 2 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
