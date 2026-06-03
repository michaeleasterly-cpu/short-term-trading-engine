"""Step 5: fundamentals_quarterly attribution integrity audit."""
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

        # 5a. PK + structure
        cols = await c.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='platform' AND table_name='fundamentals_quarterly' "
            "ORDER BY ordinal_position"
        )
        out["columns"] = [r["column_name"] for r in cols]
        pk = await c.fetch("""
          SELECT pg_get_constraintdef(con.oid) AS cdef FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname='platform' AND c.relname='fundamentals_quarterly' AND con.contype = 'p'
        """)
        out["primary_key"] = [r["cdef"] for r in pk]

        # 5b. Total + classification_id usage
        total = await c.fetchval("SELECT COUNT(*) FROM platform.fundamentals_quarterly")
        out["total_rows"] = total
        # Does fundamentals_quarterly have a classification_id column?
        has_cls = "classification_id" in [r["column_name"] for r in cols]
        out["has_classification_id_column"] = has_cls

        # 5c. Rows before current entity's FPFD (the pre-FPFD pollution)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS rows,
                 COUNT(DISTINCT fq.ticker) AS tickers_affected
          FROM platform.fundamentals_quarterly fq
          JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
          WHERE tc.lifetime_end IS NULL
            AND tc.first_public_filing_date IS NOT NULL
            AND fq.period_end_date < tc.first_public_filing_date
        """)
        out["rows_before_active_cls_fpfd"] = dict(r)

        # 5d. Rows with no matching active classification (orphan)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS orphans
          FROM platform.fundamentals_quarterly fq
          WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_classifications tc
            WHERE tc.ticker = fq.ticker AND tc.lifetime_end IS NULL
          )
        """)
        out["rows_without_active_classification"] = r["orphans"]

        # 5e. Rows whose ticker has multiple classifications (potential predecessor attribution)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS rows, COUNT(DISTINCT fq.ticker) AS tickers
          FROM platform.fundamentals_quarterly fq
          WHERE EXISTS (
            SELECT 1 FROM platform.ticker_classifications tc
            WHERE tc.ticker = fq.ticker
            GROUP BY tc.ticker HAVING COUNT(*) > 1
          )
        """)
        out["rows_for_tickers_with_multiple_classifications"] = dict(r)

        # 5f. Per-ticker missing-period inference cohort (the validator's FAIL set)
        # — already done elsewhere; re-summarize headcounts
        # Active universe with metadata
        n_active = await c.fetchval("""
          SELECT COUNT(*) FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL AND sec_document_type_primary IS NOT NULL
        """)
        out["active_with_metadata"] = n_active

        # 5g. SPAC/unit tickers in fundamentals
        spac_in_fq = await c.fetchval("""
          SELECT COUNT(DISTINCT fq.ticker) FROM platform.fundamentals_quarterly fq
          JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
          WHERE tc.asset_class = 'spac' AND tc.lifetime_end IS NULL
        """)
        out["spac_tickers_in_fundamentals"] = spac_in_fq

        # 5h. Foreign-form filers (20-F, 40-F, 10-K) with quarterly data — cadence mismatch
        r = await c.fetchrow("""
          SELECT tc.sec_document_type_primary, COUNT(DISTINCT fq.ticker)
          FROM platform.fundamentals_quarterly fq
          JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
          WHERE tc.lifetime_end IS NULL AND tc.sec_document_type_primary IS NOT NULL
          GROUP BY tc.sec_document_type_primary
        """)
        # That returned multiple rows; restructure:
        rows2 = await c.fetch("""
          SELECT tc.sec_document_type_primary AS form,
                 COUNT(DISTINCT fq.ticker) AS tickers,
                 COUNT(*) AS rows
          FROM platform.fundamentals_quarterly fq
          JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
          WHERE tc.lifetime_end IS NULL AND tc.sec_document_type_primary IS NOT NULL
          GROUP BY tc.sec_document_type_primary
          ORDER BY rows DESC
        """)
        out["fundamentals_by_filer_form"] = [{"form": r["form"], "tickers": r["tickers"], "rows": r["rows"]} for r in rows2]

        # 5i. Duplicate logical quarters (same ticker, same period_end_date — should be at most 1)
        rows3 = await c.fetch("""
          SELECT ticker, period_end_date, COUNT(*) AS n
          FROM platform.fundamentals_quarterly
          GROUP BY ticker, period_end_date
          HAVING COUNT(*) > 1
          ORDER BY n DESC LIMIT 10
        """)
        n_dups = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT ticker, period_end_date FROM platform.fundamentals_quarterly
            GROUP BY ticker, period_end_date HAVING COUNT(*) > 1
          ) AS x
        """)
        out["duplicate_logical_quarters"] = n_dups
        out["top_10_duplicate_examples"] = [{"ticker": r["ticker"], "period": r["period_end_date"], "n": r["n"]} for r in rows3]

        # 5j. Per-ticker FAIL count — what we've been calling 144 / now 161 / 111
        r = await c.fetchrow("""
          SELECT COUNT(DISTINCT fq.ticker) AS pre_fpfd_tickers,
                 COUNT(*) AS pre_fpfd_rows
          FROM platform.fundamentals_quarterly fq
          JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
          WHERE tc.lifetime_end IS NULL
            AND tc.first_public_filing_date IS NOT NULL
            AND fq.period_end_date < tc.first_public_filing_date
        """)
        out["pre_fpfd_pollution_summary"] = dict(r)

        # 5k. Recent inserts (since 2026-06-02 — today's bounded live + historical backfill)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS recent_inserts,
                 COUNT(DISTINCT ticker) AS recent_tickers,
                 MIN(recorded_at) AS first_today, MAX(recorded_at) AS last_today
          FROM platform.fundamentals_quarterly
          WHERE recorded_at > '2026-06-02 00:00:00Z'
        """)
        out["recent_inserts_today"] = dict(r)

        # 5l. fundamentals_quarterly_archive + fundamentals_quarterly_quarantine
        for t in ("fundamentals_quarterly_archive", "fundamentals_quarterly_quarantine"):
            try:
                n = await c.fetchval(f"SELECT COUNT(*) FROM platform.{t}")
                out[f"{t}_count"] = n
            except Exception as e:
                out[f"{t}_count_error"] = str(e)[:80]

    await pool.close()
    (OUT / "step5_fundamentals.json").write_text(json.dumps(out, indent=2, default=str))

    print("=== Step 5 fundamentals_quarterly attribution ===")
    print(f"  total rows: {out['total_rows']:,}")
    print(f"  primary key: {out['primary_key']}")
    print(f"  has classification_id column: {out['has_classification_id_column']}")
    print(f"  columns: {len(out['columns'])} cols")
    print()
    pre = out["pre_fpfd_pollution_summary"]
    print(f"  rows BEFORE active cls's FPFD: {pre['pre_fpfd_rows']:,} across {pre['pre_fpfd_tickers']:,} tickers")
    print(f"    = {100*pre['pre_fpfd_rows']/out['total_rows']:.2f}% of all rows")
    print(f"  rows without active classification: {out['rows_without_active_classification']:,}")
    print(f"  rows for tickers with >1 classification: "
          f"{out['rows_for_tickers_with_multiple_classifications']['rows']:,} "
          f"(across {out['rows_for_tickers_with_multiple_classifications']['tickers']:,} tickers)")
    print(f"  duplicate logical quarters: {out['duplicate_logical_quarters']:,}")
    print()
    print(f"  fundamentals by filer form (active universe):")
    for f in out["fundamentals_by_filer_form"]:
        print(f"    {f['form']:8s} {f['tickers']:>5,} tickers  {f['rows']:>8,} rows")
    print()
    print(f"  spac tickers in fundamentals: {out['spac_tickers_in_fundamentals']:,}")
    r = out["recent_inserts_today"]
    print(f"  rows inserted today: {r['recent_inserts']:,} across {r['recent_tickers']:,} tickers ({r['first_today']} → {r['last_today']})")
    print(f"  fundamentals_quarterly_archive count: {out.get('fundamentals_quarterly_archive_count')}")
    print(f"  fundamentals_quarterly_quarantine count: {out.get('fundamentals_quarterly_quarantine_count')}")
    print(f"\noutput: {OUT / 'step5_fundamentals.json'}")


asyncio.run(go())
