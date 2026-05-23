"""On-demand pre-cleanup snapshot — take, use, delete.

**Not a backup.** Supabase Pro provides daily backups + 7-day PITR.
This script captures a one-shot pre-Phase-4-cleanup snapshot of a
specific table (or tables) so the cleanup PR has a rollback baseline.
**Delete the snapshot after the cleanup is verified.**

Per v2.1 plan §2.5 (re-scoped 2026-05-23: was daily scheduled; now
on-demand only — Supabase already does the daily-backup job).

Usage::

    # Snapshot a specific table before a cleanup PR:
    bash scripts/run_db_snapshots.sh prices_daily

    # Multiple tables:
    bash scripts/run_db_snapshots.sh corporate_actions fundamentals_quarterly

    # Verify the snapshot + manifest match the live row counts:
    cat data/db_snapshots/<utc_stamp>_manifest.json | jq

    # After the cleanup PR is verified, DELETE the snapshot:
    rm -rf data/db_snapshots/<table>/<utc_stamp>.csv.gz
    rm data/db_snapshots/<utc_stamp>_manifest.json

For full restore protocol see `docs/runbooks/db-snapshots-restore.md`.

Env::
    DATABASE_URL_IPV4  required (asyncpg URL)
    TP_DATA_DIR        optional override; default = repo data/ dir
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import structlog

# Tables eligible for snapshotting (the 14 FK-protected + parent +
# audit logs). Operator picks WHICH to snapshot per cleanup PR.
ALLOWED_TABLES: frozenset[str] = frozenset({
    "ticker_classifications",
    "prices_daily", "insider_transactions", "sec_material_events",
    "corporate_actions", "earnings_events", "fundamentals_quarterly",
    "short_interest", "borrow_rates", "social_sentiment",
    "options_max_pain", "insider_sentiment", "liquidity_tiers",
    "spread_observations", "universe_candidates",
    "application_log", "data_quality_log",
})

SNAPSHOT_TIMEOUT_SEC = 600  # 10min per COPY — generous for prices_daily

logger = structlog.get_logger("scripts.db_snapshots")


def repo_data_dir() -> Path:
    override = os.environ.get("TP_DATA_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data"


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def get_alembic_revision(pool: asyncpg.Pool) -> str:
    row = await pool.fetchrow("SELECT version_num FROM platform.alembic_version")
    return row["version_num"] if row else "unknown"


async def get_row_count(pool: asyncpg.Pool, table: str) -> int:
    row = await pool.fetchrow(f"SELECT count(*) AS n FROM platform.{table}")
    return int(row["n"])


async def snapshot_one_table(pool: asyncpg.Pool, table: str, dst_path: Path) -> None:
    """COPY (SELECT *) FROM platform.<table> TO STDOUT → gzip → dst_path.

    asyncpg native `copy_from_query` (no psql shell-out). Issues
    `SET LOCAL statement_timeout = '10min'` inside a transaction so
    prices_daily (~21M rows) doesn't trip Supabase Pro's 120s default.
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '10min'")
        with gzip.open(dst_path, "wb") as gz:
            await conn.copy_from_query(
                f"SELECT * FROM platform.{table}",
                output=gz, format="csv", header=True,
                timeout=SNAPSHOT_TIMEOUT_SEC,
            )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "tables", nargs="+",
        help=f"Table(s) to snapshot. Must be one of: {sorted(ALLOWED_TABLES)}",
    )
    args = ap.parse_args()

    invalid = [t for t in args.tables if t not in ALLOWED_TABLES]
    if invalid:
        print(f"ERROR: not in allowed set: {invalid}", file=sys.stderr)
        return 2

    db_url_raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not db_url_raw:
        print("ERROR: DATABASE_URL (or DATABASE_URL_IPV4) not set", file=sys.stderr)
        return 1

    snapshots_root = repo_data_dir() / "db_snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)

    pool = await asyncpg.create_pool(
        re.sub(r"^postgresql\+asyncpg://", "postgresql://", db_url_raw),
        min_size=1, max_size=1, command_timeout=900,
    )
    stamp = utc_stamp()
    alembic_rev = await get_alembic_revision(pool)
    manifest: dict[str, object] = {
        "snapshot_stamp": stamp,
        "alembic_revision": alembic_rev,
        "purpose": "pre-cleanup rollback baseline (delete after verification)",
        "completed_at": None,
        "tables": {},
    }

    start = time.time()
    print(f"=== One-shot snapshot of {args.tables} → {snapshots_root} ===", flush=True)
    failures = 0
    for table in args.tables:
        t0 = time.time()
        try:
            row_count = await get_row_count(pool, table)
        except Exception as e:  # noqa: BLE001
            logger.error("db_snapshots.row_count_failed", table=table, error=str(e)[:200])
            manifest["tables"][table] = {"error": f"row_count_failed: {str(e)[:200]}"}
            failures += 1
            continue
        dst = snapshots_root / table / f"{table}_{stamp}.csv.gz"
        try:
            await snapshot_one_table(pool, table, dst)
            file_sha = sha256_of_file(dst)
            size_bytes = dst.stat().st_size
        except Exception as e:  # noqa: BLE001
            logger.error("db_snapshots.copy_failed", table=table, error=str(e)[:200])
            manifest["tables"][table] = {"error": f"copy_failed: {str(e)[:200]}", "rows": row_count}
            failures += 1
            continue
        manifest["tables"][table] = {
            "rows": row_count, "sha256": file_sha, "size_bytes": size_bytes,
            "path": str(dst.relative_to(repo_data_dir().parent)),
            "duration_s": round(time.time() - t0, 2),
        }
        print(
            f"  ✓ {table:<28} {row_count:>10,} rows  {size_bytes/1024/1024:>6.1f}MB  "
            f"sha={file_sha[:8]}...  ({time.time()-t0:.1f}s)",
            flush=True,
        )

    manifest["completed_at"] = datetime.now(UTC).isoformat()
    manifest_path = snapshots_root / f"{stamp}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {manifest_path}", flush=True)
    print(f"Total wall-time: {time.time()-start:.0f}s", flush=True)
    print(
        "\n⚠  REMEMBER: delete these snapshot files after the cleanup is verified.\n"
        "   Supabase Pro daily backup + 7-day PITR is the durable backup story.\n"
        "   This snapshot is ONLY for pre-cleanup rollback.\n",
        flush=True,
    )
    await pool.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
