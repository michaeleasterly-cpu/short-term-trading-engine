"""Tiny CLI for emitting a single ``platform.application_log`` row from bash.

Invoked by ``run_data_operations.sh`` to instrument the bash-only steps
(audit / validation re-check / matview refresh / CSV compression / emit
DATA_OPERATIONS_COMPLETE) so they appear in the daemon progress panel
alongside the 15 ops.py --update stages.

Usage::

    scripts/_log_event.py \\
        --run-id <uuid> \\
        --event-type INGESTION_START|INGESTION_COMPLETE|INGESTION_FAILED \\
        --stage-name wrapper_audit \\
        [--message "free-text"] \\
        [--severity INFO|WARNING|ERROR]

The progress panel's SQL ``WHERE data->>'stage' IS NOT NULL`` picks up
any row this script writes; using the same ``--run-id`` as the daemon's
STARTUP event correlates wrapper steps with the rest of the run.

Exit code is always 0 unless DATABASE_URL is missing or asyncpg can't
connect — a logging failure must not crash the wrapper.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid


async def _amain(args: argparse.Namespace) -> int:
    import asyncpg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("_log_event: DATABASE_URL not set", file=sys.stderr)
        return 1
    try:
        run_id = uuid.UUID(args.run_id)
    except ValueError:
        print(f"_log_event: invalid --run-id {args.run_id!r}", file=sys.stderr)
        return 1

    data = {"stage": args.stage_name, "source": "data_operations_daemon"}
    if args.message:
        data["message_extra"] = args.message

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"_log_event: connect failed: {exc}", file=sys.stderr)
        return 1
    try:
        await conn.execute(
            """
            INSERT INTO platform.application_log
                (engine, run_id, event_type, severity, message, data)
            VALUES ('ops', $1, $2, $3, $4, $5::jsonb)
            """,
            run_id,
            args.event_type,
            args.severity,
            args.message or f"{args.stage_name} {args.event_type.lower()}",
            json.dumps(data),
        )
    finally:
        await conn.close()
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--run-id", required=True, help="UUID matching the daemon's STARTUP run_id.")
    p.add_argument(
        "--event-type",
        required=True,
        choices=("INGESTION_START", "INGESTION_COMPLETE", "INGESTION_FAILED"),
    )
    p.add_argument("--stage-name", required=True, help="e.g. wrapper_audit, wrapper_compress")
    p.add_argument("--message", default=None)
    p.add_argument("--severity", default="INFO", choices=("INFO", "WARNING", "ERROR"))
    args = p.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":  # pragma: no cover
    main()
