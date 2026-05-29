"""Hold a Postgres advisory lock for the data-ops run.

Bash subshell helper for scripts/run_data_operations.sh. Acquires
``pg_try_advisory_lock(hashtext('data_ops_run'))`` on a dedicated
connection, prints ``ACQUIRED`` or ``HELD`` to stdout, then sleeps
until SIGTERM. On exit the connection closes and Postgres releases
the lock automatically.

Why a separate process: pg_advisory_lock is connection-scoped, but
the data-ops wrapper runs many different commands across many
asyncpg-pool connections. A holder-process keeps the lock alive for
the whole wrapper duration without coupling to any other connection.

Cross-container concurrency: this is what guards a cron container
and the lane-service operator-trigger container against running the
script simultaneously. The existing ``LOCK_DIR`` mkdir lock only
guards same-host overlap.
"""
from __future__ import annotations

import os
import signal
import sys
import time

import psycopg  # psycopg3 — declared in pyproject.toml

LOCK_KEY_TEXT = "data_ops_run"


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get(
        "DATABASE_URL_IPV4"
    )
    if not dsn:
        print(
            "ERROR no DATABASE_URL or DATABASE_URL_IPV4 set",
            file=sys.stderr,
        )
        sys.exit(2)
    return dsn


def _acquire() -> psycopg.Connection:
    conn = psycopg.connect(_resolve_dsn(), autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY_TEXT,),
        )
        row = cur.fetchone()
    got_lock = bool(row[0]) if row else False
    if not got_lock:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        print("HELD", flush=True)
        sys.exit(1)
    print("ACQUIRED", flush=True)
    return conn


def _install_signal_handlers(conn: psycopg.Connection) -> None:
    def _release(signum: int, _frame) -> None:  # noqa: ARG001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _release)
    signal.signal(signal.SIGINT, _release)


def main() -> None:
    conn = _acquire()
    _install_signal_handlers(conn)
    # Idle until signaled. The bash wrapper SIGTERMs us on exit.
    while True:
        time.sleep(60)
        # Cheap keepalive so connection-idle timeouts don't kill us.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception:  # noqa: BLE001
            # Connection died — the lock is also gone. Exit so the
            # wrapper's EXIT trap notices.
            sys.exit(3)


if __name__ == "__main__":
    main()
