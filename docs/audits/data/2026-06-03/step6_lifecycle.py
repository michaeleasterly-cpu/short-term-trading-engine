"""Step 6: lifecycle + corporate-action substrate audit."""
import asyncio
import json
import os
from pathlib import Path

import asyncpg

OUT = Path("/tmp/audit")


async def go() -> None:
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"],
        statement_cache_size=0,
    )
    async with pool.acquire() as c:
        out: dict = {}

        # 6a. ticker_lifecycle_events
        n = await c.fetchval("SELECT COUNT(*) FROM platform.ticker_lifecycle_events")
        rows = await c.fetch("""
          SELECT form_type, COUNT(*) AS n
          FROM platform.ticker_lifecycle_events GROUP BY form_type ORDER BY n DESC
        """)
        out["ticker_lifecycle_events"] = {
            "total": n,
            "by_form_type": [{"form": r["form_type"], "n": r["n"]} for r in rows],
        }

        # 6b. corporate_events
        n = await c.fetchval("SELECT COUNT(*) FROM platform.corporate_events")
        rows = await c.fetch("""
          SELECT event_kind, COUNT(*) AS n FROM platform.corporate_events
          GROUP BY event_kind ORDER BY n DESC
        """)
        out["corporate_events"] = {
            "total": n,
            "by_kind": [{"kind": r["event_kind"], "n": r["n"]} for r in rows],
        }

        # 6c. corporate_actions
        n = await c.fetchval("SELECT COUNT(*) FROM platform.corporate_actions")
        rows = await c.fetch("""
          SELECT action_type, COUNT(*) AS n FROM platform.corporate_actions
          GROUP BY action_type ORDER BY n DESC LIMIT 15
        """)
        out["corporate_actions"] = {
            "total": n,
            "by_action_type": [{"type": r["action_type"], "n": r["n"]} for r in rows],
        }

        # 6d. issuer_history
        n = await c.fetchval("SELECT COUNT(*) FROM platform.issuer_history")
        rows = await c.fetch("""
          SELECT source, COUNT(*) AS n FROM platform.issuer_history GROUP BY source ORDER BY n DESC
        """)
        out["issuer_history"] = {
            "total": n,
            "by_source": [{"source": r["source"], "n": r["n"]} for r in rows],
        }

        # 6e. ticker_classifications.issuer_lifecycle_state distribution
        rows = await c.fetch("""
          SELECT issuer_lifecycle_state, COUNT(*) AS n
          FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL
          GROUP BY issuer_lifecycle_state ORDER BY n DESC
        """)
        out["active_cls_by_lifecycle_state"] = [
            {"state": r["issuer_lifecycle_state"], "n": r["n"]} for r in rows
        ]

        # 6f. tickers WITH symbol change in symbol_history_evidence_backfill but NO change in ticker_history
        sources = await c.fetch("""
          SELECT source, COUNT(*) FROM platform.ticker_classifications
          WHERE source LIKE 'symbol_history_evidence_backfill%'
          GROUP BY source ORDER BY COUNT(*) DESC
        """)
        out["symbol_history_arc_classifications_by_source"] = [{"source": r["source"], "n": r["count"]} for r in sources]

    await pool.close()
    (OUT / "step6_lifecycle.json").write_text(json.dumps(out, indent=2, default=str))

    print("=== Step 6 lifecycle + corp-action substrate ===")
    e = out["ticker_lifecycle_events"]
    print(f"  ticker_lifecycle_events: {e['total']:,} rows")
    for f in e["by_form_type"]:
        print(f"    {f['form']!s:10s} {f['n']:>6,}")
    print()
    e = out["corporate_events"]
    print(f"  corporate_events: {e['total']:,} rows")
    for f in e["by_kind"]:
        print(f"    {f['kind']!s:30s} {f['n']:>6,}")
    print()
    e = out["corporate_actions"]
    print(f"  corporate_actions: {e['total']:,} rows")
    for f in e["by_action_type"][:10]:
        print(f"    {f['type']!s:20s} {f['n']:>6,}")
    print()
    e = out["issuer_history"]
    print(f"  issuer_history: {e['total']:,} rows")
    for f in e["by_source"]:
        print(f"    {f['source']!s:20s} {f['n']:>6,}")
    print()
    print(f"  active cls by issuer_lifecycle_state:")
    for r in out["active_cls_by_lifecycle_state"]:
        print(f"    {r['state']!s:25s} {r['n']:>6,}")
    print()
    print(f"  symbol_history_arc populated classifications:")
    for r in out["symbol_history_arc_classifications_by_source"]:
        print(f"    {r['source']!s:60s} {r['n']:>6,}")
    print(f"\noutput: {OUT / 'step6_lifecycle.json'}")


asyncio.run(go())
