"""Step 1: schema inventory + FK graph. Read-only."""
import asyncio
import json
import os
from pathlib import Path

import asyncpg

OUT = Path("/tmp/audit")
OUT.mkdir(exist_ok=True)


async def go() -> None:
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"],
        statement_cache_size=0,
    )
    async with pool.acquire() as c:
        out: dict = {}

        # 1a. All schemas
        rows = await c.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
            "('pg_catalog','information_schema','pg_toast') AND nspname NOT LIKE 'pg_temp_%' "
            "ORDER BY nspname"
        )
        out["schemas"] = [r["nspname"] for r in rows]

        # 1b. All tables in platform schema with row counts
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname,
                   c.reltuples::bigint AS estimated_rows,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   obj_description(c.oid) AS comment
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname IN ('platform','public')
            ORDER BY n.nspname, c.relname
            """
        )
        out["tables"] = [
            {"schema": r["nspname"], "name": r["relname"],
             "estimated_rows": r["estimated_rows"], "total_bytes": r["total_bytes"],
             "comment": r["comment"]}
            for r in rows
        ]
        # Exact counts only for small tables (< 100k estimated) to avoid scanning huge ones
        for t in out["tables"]:
            if t["schema"] == "platform" and t["estimated_rows"] < 100000:
                try:
                    n = await c.fetchval(f'SELECT COUNT(*) FROM {t["schema"]}.{t["name"]}')
                    t["exact_rows"] = n
                except Exception as e:
                    t["exact_rows_error"] = str(e)[:80]

        # 1c. All PKs
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname, conname,
                   pg_get_constraintdef(con.oid) AS cdef
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND con.contype = 'p'
            ORDER BY c.relname
            """
        )
        out["primary_keys"] = [
            {"table": r["relname"], "constraint": r["conname"], "definition": r["cdef"]}
            for r in rows
        ]

        # 1d. All FKs
        rows = await c.fetch(
            """
            SELECT
                n.nspname AS source_schema, c.relname AS source_table,
                conname,
                pg_get_constraintdef(con.oid) AS cdef
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND con.contype = 'f'
            ORDER BY c.relname, conname
            """
        )
        out["foreign_keys"] = [
            {"table": r["source_table"], "constraint": r["conname"], "definition": r["cdef"]}
            for r in rows
        ]

        # 1e. All UNIQUE constraints (non-PK)
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname, conname,
                   pg_get_constraintdef(con.oid) AS cdef
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND con.contype = 'u'
            ORDER BY c.relname, conname
            """
        )
        out["unique_constraints"] = [
            {"table": r["relname"], "constraint": r["conname"], "definition": r["cdef"]}
            for r in rows
        ]

        # 1f. All CHECK constraints
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname, conname,
                   pg_get_constraintdef(con.oid) AS cdef
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND con.contype = 'c'
            ORDER BY c.relname, conname
            """
        )
        out["check_constraints"] = [
            {"table": r["relname"], "constraint": r["conname"], "definition": r["cdef"]}
            for r in rows
        ]

        # 1g. All EXCLUDE constraints
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname, conname,
                   pg_get_constraintdef(con.oid) AS cdef
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND con.contype = 'x'
            ORDER BY c.relname, conname
            """
        )
        out["exclude_constraints"] = [
            {"table": r["relname"], "constraint": r["conname"], "definition": r["cdef"]}
            for r in rows
        ]

        # 1h. All indexes
        rows = await c.fetch(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='platform' ORDER BY tablename, indexname"
        )
        out["indexes"] = [
            {"table": r["tablename"], "index": r["indexname"], "definition": r["indexdef"]}
            for r in rows
        ]

        # 1i. All triggers
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname AS table_name, t.tgname AS trigger_name,
                   pg_get_triggerdef(t.oid) AS tdef
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND NOT t.tgisinternal
            ORDER BY c.relname, t.tgname
            """
        )
        out["triggers"] = [
            {"table": r["table_name"], "trigger": r["trigger_name"], "definition": r["tdef"]}
            for r in rows
        ]

        # 1j. Tables with no PK
        rows = await c.fetch(
            """
            SELECT n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'platform' AND c.relkind = 'r'
              AND NOT EXISTS (
                SELECT 1 FROM pg_constraint con
                WHERE con.conrelid = c.oid AND con.contype = 'p'
              )
            ORDER BY c.relname
            """
        )
        out["tables_without_pk"] = [r["relname"] for r in rows]

        # 1k. All columns in platform schema (for FK-graph synthesis later)
        rows = await c.fetch(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'platform'
            ORDER BY table_name, ordinal_position
            """
        )
        cols_by_table: dict = {}
        for r in rows:
            cols_by_table.setdefault(r["table_name"], []).append({
                "name": r["column_name"],
                "type": r["data_type"],
                "nullable": r["is_nullable"],
                "default": r["column_default"],
            })
        out["columns_by_table"] = cols_by_table

    await pool.close()

    (OUT / "step1_schema_inventory.json").write_text(json.dumps(out, indent=2, default=str))

    # Print summary
    print(f"=== Step 1 schema inventory summary ===")
    print(f"schemas: {out['schemas']}")
    print(f"platform tables: {sum(1 for t in out['tables'] if t['schema']=='platform')}")
    print(f"primary keys: {len(out['primary_keys'])}")
    print(f"foreign keys: {len(out['foreign_keys'])}")
    print(f"unique constraints (non-PK): {len(out['unique_constraints'])}")
    print(f"check constraints: {len(out['check_constraints'])}")
    print(f"exclude constraints: {len(out['exclude_constraints'])}")
    print(f"indexes: {len(out['indexes'])}")
    print(f"triggers: {len(out['triggers'])}")
    print(f"tables without PK: {len(out['tables_without_pk'])}: {out['tables_without_pk']}")
    print(f"output written: {OUT / 'step1_schema_inventory.json'}")
    sizes = sorted(
        [(t["name"], t["estimated_rows"], t["total_bytes"]) for t in out["tables"] if t["schema"]=="platform"],
        key=lambda x: -x[2],
    )
    print()
    print(f"=== Top 20 platform tables by total bytes ===")
    for n, er, b in sizes[:20]:
        print(f"  {n:50s}  est_rows={er:>15,}  bytes={b:>15,}")


asyncio.run(go())
