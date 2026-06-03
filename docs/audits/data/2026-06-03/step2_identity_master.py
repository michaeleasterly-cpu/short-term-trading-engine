"""Step 2: identity master audit. Read-only."""
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

        # 2a. ticker_classifications — coverage of identifiers + temporal cols
        r = await c.fetchrow("""
          SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE lifetime_end IS NULL) AS active,
            COUNT(*) FILTER (WHERE lifetime_end IS NOT NULL) AS retired,
            COUNT(*) FILTER (WHERE cik IS NOT NULL AND cik <> '') AS w_cik,
            COUNT(*) FILTER (WHERE figi IS NOT NULL) AS w_figi,
            COUNT(*) FILTER (WHERE cusip IS NOT NULL) AS w_cusip,
            COUNT(*) FILTER (WHERE isin IS NOT NULL) AS w_isin,
            COUNT(*) FILTER (WHERE first_public_filing_date IS NOT NULL) AS w_fpfd,
            COUNT(*) FILTER (WHERE lifetime_start = DATE '1900-01-01') AS sentinel_ls,
            COUNT(*) FILTER (WHERE lifetime_start IS NULL) AS null_ls,
            COUNT(*) FILTER (WHERE country IS NOT NULL) AS w_country,
            COUNT(*) FILTER (WHERE gics_sector IS NOT NULL) AS w_gics,
            COUNT(*) FILTER (WHERE asset_class IS NOT NULL) AS w_asset_class
          FROM platform.ticker_classifications
        """)
        out["ticker_classifications_global"] = dict(r)

        # 2b. Same coverage scoped to ACTIVE only
        r = await c.fetchrow("""
          SELECT
            COUNT(*) AS active,
            COUNT(*) FILTER (WHERE cik IS NOT NULL AND cik <> '') AS w_cik,
            COUNT(*) FILTER (WHERE figi IS NOT NULL) AS w_figi,
            COUNT(*) FILTER (WHERE cusip IS NOT NULL) AS w_cusip,
            COUNT(*) FILTER (WHERE isin IS NOT NULL) AS w_isin,
            COUNT(*) FILTER (WHERE first_public_filing_date IS NOT NULL) AS w_fpfd,
            COUNT(*) FILTER (WHERE lifetime_start = DATE '1900-01-01') AS sentinel_ls
          FROM platform.ticker_classifications
          WHERE lifetime_end IS NULL
        """)
        out["ticker_classifications_active"] = dict(r)

        # 2c. Same-ticker, multiple classification_ids (ticker reuse evidence)
        rows = await c.fetch("""
          SELECT ticker, COUNT(*) AS n
          FROM platform.ticker_classifications
          GROUP BY ticker
          HAVING COUNT(*) > 1
          ORDER BY n DESC, ticker
          LIMIT 25
        """)
        n_tickers_multi_cls = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT ticker FROM platform.ticker_classifications
            GROUP BY ticker HAVING COUNT(*) > 1
          ) AS m
        """)
        out["tickers_with_multiple_classifications"] = n_tickers_multi_cls
        out["top_25_tickers_with_multiple_cls"] = [{"ticker": r["ticker"], "n_cls": r["n"]} for r in rows]

        # 2d. Same-CIK, multiple classification_ids (entity history)
        rows = await c.fetch("""
          SELECT cik, COUNT(*) AS n
          FROM platform.ticker_classifications
          WHERE cik IS NOT NULL AND cik <> ''
          GROUP BY cik HAVING COUNT(*) > 1
          ORDER BY n DESC
          LIMIT 25
        """)
        n_ciks_multi = await c.fetchval("""
          SELECT COUNT(*) FROM (
            SELECT cik FROM platform.ticker_classifications
            WHERE cik IS NOT NULL AND cik <> ''
            GROUP BY cik HAVING COUNT(*) > 1
          ) AS m
        """)
        out["ciks_with_multiple_classifications"] = n_ciks_multi
        out["top_25_ciks_with_multiple_cls"] = [{"cik": r["cik"], "n_cls": r["n"]} for r in rows]

        # 2e. Source / discovery_source / cik_source distributions
        rows = await c.fetch("""
          SELECT source, COUNT(*) FROM platform.ticker_classifications
          GROUP BY source ORDER BY COUNT(*) DESC LIMIT 20
        """)
        out["ticker_classifications_by_source"] = [{"source": r["source"], "n": r["count"]} for r in rows]
        rows = await c.fetch("""
          SELECT discovery_source, COUNT(*) FROM platform.ticker_classifications
          GROUP BY discovery_source ORDER BY COUNT(*) DESC
        """)
        out["ticker_classifications_by_discovery_source"] = [{"discovery_source": r["discovery_source"], "n": r["count"]} for r in rows]

        # 2f. ticker_history coverage + multi-row distribution
        r = await c.fetchrow("""
          SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT ticker) AS distinct_tickers,
            COUNT(DISTINCT classification_id) AS distinct_cls,
            COUNT(*) FILTER (WHERE valid_to IS NULL) AS open_ended,
            COUNT(*) FILTER (WHERE valid_to IS NOT NULL) AS closed
          FROM platform.ticker_history
        """)
        out["ticker_history_global"] = dict(r)

        # 2g. ticker_history rows-per-ticker distribution
        rows = await c.fetch("""
          SELECT n_rows, COUNT(*) AS n_tickers FROM (
            SELECT ticker, COUNT(*) AS n_rows
            FROM platform.ticker_history
            GROUP BY ticker
          ) AS x
          GROUP BY n_rows ORDER BY n_rows
        """)
        out["ticker_history_rows_per_ticker_distribution"] = [
            {"rows_per_ticker": r["n_rows"], "tickers_with_this_count": r["n_tickers"]}
            for r in rows
        ]

        # 2h. ticker_history multi-row tickers (top examples)
        rows = await c.fetch("""
          SELECT ticker, COUNT(*) AS n_rows
          FROM platform.ticker_history
          GROUP BY ticker HAVING COUNT(*) > 1
          ORDER BY n_rows DESC, ticker LIMIT 20
        """)
        out["top_20_tickers_with_multi_history"] = [{"ticker": r["ticker"], "n_rows": r["n_rows"]} for r in rows]

        # 2i. issuer_securities coverage
        r = await c.fetchrow("""
          SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT issuer_id) AS distinct_issuers,
            COUNT(DISTINCT classification_id) AS distinct_cls
          FROM platform.issuer_securities
        """)
        out["issuer_securities_global"] = dict(r)

        # 2j. issuers coverage
        r = await c.fetchrow("""
          SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE cik IS NOT NULL) AS w_cik,
            COUNT(*) FILTER (WHERE legal_name IS NOT NULL) AS w_legal_name
          FROM platform.issuers
        """)
        out["issuers_global"] = dict(r)

        # 2k. Cross-table: how many active ticker_classifications have NO matching
        # issuer_securities row, NO matching ticker_history row?
        r = await c.fetchrow("""
          SELECT
            COUNT(*) FILTER (WHERE NOT EXISTS (
              SELECT 1 FROM platform.ticker_history th WHERE th.classification_id = tc.id
            )) AS no_th,
            COUNT(*) FILTER (WHERE NOT EXISTS (
              SELECT 1 FROM platform.issuer_securities iss WHERE iss.classification_id = tc.id
            )) AS no_iss
          FROM platform.ticker_classifications tc
          WHERE tc.lifetime_end IS NULL
        """)
        out["active_cls_without_substrate_links"] = dict(r)

        # 2l. Coverage of FK relations that should exist but don't (no FK constraint)
        # ticker_history.classification_id -> ticker_classifications.id
        no_match = await c.fetchval("""
          SELECT COUNT(*) FROM platform.ticker_history th
          WHERE NOT EXISTS (SELECT 1 FROM platform.ticker_classifications tc WHERE tc.id = th.classification_id)
        """)
        out["ticker_history_orphan_classification_id"] = no_match

        # issuer_securities.issuer_id -> issuers.issuer_id
        no_match2 = await c.fetchval("""
          SELECT COUNT(*) FROM platform.issuer_securities iss
          WHERE NOT EXISTS (SELECT 1 FROM platform.issuers i WHERE i.issuer_id = iss.issuer_id)
        """)
        out["issuer_securities_orphan_issuer_id"] = no_match2

    await pool.close()

    (OUT / "step2_identity_master.json").write_text(json.dumps(out, indent=2, default=str))

    # Print summary
    print("=== Step 2 identity master audit ===")
    g = out["ticker_classifications_global"]
    a = out["ticker_classifications_active"]
    print(f"ticker_classifications total: {g['total_rows']:,}  active: {a['active']:,}  retired: {g['retired']:,}")
    print(f"  active identifier coverage:")
    for k in ("w_cik", "w_figi", "w_cusip", "w_isin", "w_fpfd"):
        v = a[k]
        pct = 100 * v / a["active"]
        print(f"    {k:10s} {v:>6,} / {a['active']:,} ({pct:5.1f}%)")
    print(f"    sentinel_lifetime_start (1900-01-01) on active: {a['sentinel_ls']:,} / {a['active']:,} ({100*a['sentinel_ls']/a['active']:.1f}%)")
    print()
    print(f"tickers with >1 classification (reuse evidence): {out['tickers_with_multiple_classifications']:,}")
    print(f"CIKs with >1 classification: {out['ciks_with_multiple_classifications']:,}")
    print()
    print(f"ticker_history: {out['ticker_history_global']['total_rows']:,} rows, "
          f"{out['ticker_history_global']['distinct_tickers']:,} distinct tickers, "
          f"open-ended: {out['ticker_history_global']['open_ended']:,}")
    print(f"  rows-per-ticker distribution:")
    for d in out["ticker_history_rows_per_ticker_distribution"]:
        print(f"    {d['rows_per_ticker']} rows: {d['tickers_with_this_count']:,} tickers")
    print()
    print(f"issuer_securities: {out['issuer_securities_global']['total_rows']:,} rows, "
          f"{out['issuer_securities_global']['distinct_issuers']:,} distinct issuers")
    print(f"issuers: {out['issuers_global']['total_rows']:,} rows")
    print()
    print(f"active classifications WITHOUT ticker_history link: {out['active_cls_without_substrate_links']['no_th']:,}")
    print(f"active classifications WITHOUT issuer_securities link: {out['active_cls_without_substrate_links']['no_iss']:,}")
    print(f"ticker_history rows orphan (no matching classification): {out['ticker_history_orphan_classification_id']:,}")
    print(f"issuer_securities rows orphan (no matching issuer): {out['issuer_securities_orphan_issuer_id']:,}")
    print(f"output: {OUT / 'step2_identity_master.json'}")


asyncio.run(go())
