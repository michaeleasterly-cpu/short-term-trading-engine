"""Audit extension: archive lineage + universe + identifier conflicts + DOC gate."""
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
    out: dict = {}
    async with pool.acquire() as c:
        # === Universe construction ===
        # universe_candidates breakdown
        rows = await c.fetch("""
          SELECT asset_class, COUNT(*) AS n
          FROM platform.universe_candidates uc
          JOIN platform.ticker_classifications tc ON tc.id = uc.classification_id
          GROUP BY asset_class ORDER BY n DESC
        """)
        out["universe_by_asset_class"] = [{"asset_class": r["asset_class"], "n": r["n"]} for r in rows]
        n_uc = await c.fetchval("SELECT COUNT(*) FROM platform.universe_candidates")
        n_uc_active = await c.fetchval("""
          SELECT COUNT(*) FROM platform.universe_candidates uc
          JOIN platform.ticker_classifications tc ON tc.id = uc.classification_id
          WHERE tc.lifetime_end IS NULL
        """)
        out["universe_total"] = n_uc
        out["universe_active_cls"] = n_uc_active

        # SPACs / units / warrants in tier 1/2 (engine universe)
        rows = await c.fetch("""
          SELECT tc.asset_class, tc.instrument_subtype, lt.tier, COUNT(*) AS n
          FROM platform.liquidity_tiers lt
          JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
          WHERE lt.tier <= 2 AND tc.lifetime_end IS NULL
          GROUP BY tc.asset_class, tc.instrument_subtype, lt.tier
          ORDER BY n DESC
        """)
        out["tier12_by_asset_class_subtype"] = [
            {"asset_class": r["asset_class"], "subtype": r["instrument_subtype"],
             "tier": r["tier"], "n": r["n"]}
            for r in rows
        ]

        # Tickers ending in 'U' (units) or 'W' (warrants) appearing in tier ≤ 2 stock universe
        leakage = await c.fetchval("""
          SELECT COUNT(*) FROM platform.liquidity_tiers lt
          JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
          WHERE lt.tier <= 2 AND tc.asset_class = 'stock' AND tc.lifetime_end IS NULL
            AND (lt.ticker ~ 'U$' OR lt.ticker ~ 'W$')
        """)
        out["stock_class_with_unit_warrant_ticker_suffix"] = leakage

        # === Identifier conflict audit ===
        # CIKs with multiple ACTIVE classifications (potential conflict)
        rows = await c.fetch("""
          SELECT cik, COUNT(*) AS n, array_agg(ticker) AS tickers
          FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL AND cik IS NOT NULL AND cik <> ''
          GROUP BY cik HAVING COUNT(*) > 1
          ORDER BY n DESC LIMIT 10
        """)
        out["active_ciks_with_multiple_classifications"] = [
            {"cik": r["cik"], "n": r["n"], "tickers": r["tickers"]}
            for r in rows
        ]

        # FIGIs duplicated across CIKs (active only)
        rows = await c.fetch("""
          SELECT figi, COUNT(DISTINCT cik) AS distinct_ciks, COUNT(*) AS n_rows
          FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL AND figi IS NOT NULL AND cik IS NOT NULL AND cik <> ''
          GROUP BY figi HAVING COUNT(DISTINCT cik) > 1
          ORDER BY distinct_ciks DESC LIMIT 5
        """)
        out["figis_across_multiple_ciks_active"] = [
            {"figi": r["figi"], "distinct_ciks": r["distinct_ciks"], "n": r["n_rows"]}
            for r in rows
        ]

        # CUSIPs duplicated across CIKs (active only)
        rows = await c.fetch("""
          SELECT cusip, COUNT(DISTINCT cik) AS distinct_ciks, COUNT(*) AS n_rows
          FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL AND cusip IS NOT NULL AND cik IS NOT NULL AND cik <> ''
          GROUP BY cusip HAVING COUNT(DISTINCT cik) > 1
          ORDER BY distinct_ciks DESC LIMIT 5
        """)
        out["cusips_across_multiple_ciks_active"] = [
            {"cusip": r["cusip"], "distinct_ciks": r["distinct_ciks"], "n": r["n_rows"]}
            for r in rows
        ]

        # === Natural-key duplicate audit across substrate ===
        # duplicate (ticker, date) in prices_daily?
        dup_pd = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT ticker, date FROM platform.prices_daily
            GROUP BY ticker, date HAVING COUNT(*) > 1
          ) AS x
        """)
        out["duplicate_ticker_date_prices_daily"] = dup_pd

        # duplicate (ticker, action_date, action_type) in corporate_actions?
        dup_ca = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT ticker, action_date, action_type FROM platform.corporate_actions
            GROUP BY ticker, action_date, action_type HAVING COUNT(*) > 1
          ) AS x
        """)
        out["duplicate_ticker_actiondate_actiontype_corp_actions"] = dup_ca

        # duplicate fundamentals_quarterly natural-key (ticker, period_end_date)
        dup_fq = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT ticker, period_end_date FROM platform.fundamentals_quarterly
            GROUP BY ticker, period_end_date HAVING COUNT(*) > 1
          ) AS x
        """)
        out["duplicate_ticker_period_fq"] = dup_fq

        # === Foreign-issuer + asset_class breakdown of ACTIVE engine universe ===
        rows = await c.fetch("""
          SELECT tc.asset_class,
                 tc.sec_document_type_primary,
                 COUNT(*) AS n
          FROM platform.liquidity_tiers lt
          JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
          WHERE lt.tier <= 2 AND tc.lifetime_end IS NULL
          GROUP BY tc.asset_class, tc.sec_document_type_primary
          ORDER BY n DESC LIMIT 15
        """)
        out["tier12_asset_form_breakdown"] = [
            {"asset_class": r["asset_class"], "form": r["sec_document_type_primary"], "n": r["n"]}
            for r in rows
        ]

    await pool.close()
    (OUT / "step_ext.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))


asyncio.run(go())
