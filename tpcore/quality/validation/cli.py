"""CLI entry point: ``python -m tpcore.quality.validation``.

Loads ``DATABASE_URL`` from the environment (falling back to
``DATABASE_URL_IPV4`` for the local Supabase pooler URL — see project
memory on the dual-URL setup), runs the suite, prints a human-readable
report to stdout, and exits 0 / 1.
"""
from __future__ import annotations

import asyncio
import os
import sys
from io import StringIO

import structlog

from tpcore.db import build_asyncpg_pool

from .models import SuiteResult
from .suite import run_suite

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        result = await run_suite(pool)
    finally:
        await pool.close()

    print(format_report(result))
    return 0 if result.passed else 1


def format_report(result: SuiteResult) -> str:
    """Render a `SuiteResult` as plain text for stdout."""
    out = StringIO()
    duration = (result.finished_at - result.started_at).total_seconds()
    out.write(f"Validation Suite — run_id={result.run_id} duration={duration:.2f}s\n")
    out.write(f"Started:  {result.started_at.isoformat()}\n")
    out.write(f"Finished: {result.finished_at.isoformat()}\n")
    out.write(f"Overall:  {'PASS' if result.passed else 'FAIL'}\n\n")
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        out.write(
            f"  [{status}] {check.name:14s} "
            f"total={check.total:4d} failed={check.failed:4d} "
            f"duration={check.duration_ms}ms\n"
        )
        for failure in check.failures:
            out.write(
                f"      - {failure.ticker:8s} {failure.reason:20s} "
                f"expected={failure.expected} observed={failure.observed}\n"
            )
    return out.getvalue()


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["format_report", "main"]
