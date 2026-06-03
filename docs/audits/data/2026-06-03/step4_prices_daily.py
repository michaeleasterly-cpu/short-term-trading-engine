"""Step 4: prices_daily attribution integrity audit."""
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

        # 4a. total rows + NULL classification_id
        total = await c.fetchval("SELECT COUNT(*) FROM platform.prices_daily")
        null_cls = await c.fetchval("SELECT COUNT(*) FROM platform.prices_daily WHERE classification_id IS NULL")
        out["total_bars"] = total
        out["null_classification_id"] = null_cls

        # 4b. classification_id not present in ticker_classifications
        orphan = await c.fetchval("""
          SELECT COUNT(*) FROM platform.prices_daily pd
          WHERE pd.classification_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM platform.ticker_classifications tc WHERE tc.id = pd.classification_id)
        """)
        out["orphan_classification_id"] = orphan

        # 4c. bars before the assigned classification's FPFD (mis-attribution candidate #1)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS bars_before_fpfd,
                 COUNT(DISTINCT pd.ticker) AS tickers_affected,
                 COUNT(DISTINCT pd.classification_id) AS classifications_affected
          FROM platform.prices_daily pd
          JOIN platform.ticker_classifications tc ON tc.id = pd.classification_id
          WHERE tc.first_public_filing_date IS NOT NULL
            AND pd.date < tc.first_public_filing_date
        """)
        out["bars_before_fpfd"] = dict(r)

        # 4d. bars after the assigned classification's lifetime_end (mis-attribution candidate #2)
        r = await c.fetchrow("""
          SELECT COUNT(*) AS bars_after_lifetime_end,
                 COUNT(DISTINCT pd.ticker) AS tickers_affected,
                 COUNT(DISTINCT pd.classification_id) AS classifications_affected
          FROM platform.prices_daily pd
          JOIN platform.ticker_classifications tc ON tc.id = pd.classification_id
          WHERE tc.lifetime_end IS NOT NULL
            AND pd.date > tc.lifetime_end
        """)
        out["bars_after_lifetime_end"] = dict(r)

        # 4e. bars whose date is outside the assigned classification's ticker_history valid window
        r = await c.fetchrow("""
          SELECT COUNT(*) AS bars_outside_th_window
          FROM platform.prices_daily pd
          WHERE NOT EXISTS (
            SELECT 1 FROM platform.ticker_history th
            WHERE th.classification_id = pd.classification_id
              AND th.ticker = pd.ticker
              AND th.valid_from <= pd.date
              AND (th.valid_to IS NULL OR pd.date < th.valid_to)
          )
        """)
        out["bars_outside_ticker_history_window"] = r["bars_outside_th_window"]

        # 4f. Cases where (ticker, date) could resolve to a different classification_id via ticker_history
        # if substrate were complete (i.e., a non-active classification owns this ticker at this date)
        r = await c.fetchrow("""
          WITH alt AS (
            SELECT pd.ticker, pd.date, pd.classification_id AS current_cls,
                   th.classification_id AS th_cls
            FROM platform.prices_daily pd
            JOIN platform.ticker_history th ON th.ticker = pd.ticker
              AND th.valid_from <= pd.date
              AND (th.valid_to IS NULL OR pd.date < th.valid_to)
            WHERE th.classification_id <> pd.classification_id
          )
          SELECT COUNT(*) AS rows, COUNT(DISTINCT ticker) AS tickers
          FROM alt
        """)
        out["bars_attributed_to_wrong_cls_per_ticker_history"] = dict(r)

        # 4g. Top 50 tickers by suspected mis-attributed bar count (pre-FPFD)
        rows = await c.fetch("""
          SELECT pd.ticker, COUNT(*) AS bars_before_fpfd
          FROM platform.prices_daily pd
          JOIN platform.ticker_classifications tc ON tc.id = pd.classification_id
          WHERE tc.first_public_filing_date IS NOT NULL
            AND pd.date < tc.first_public_filing_date
          GROUP BY pd.ticker
          ORDER BY bars_before_fpfd DESC, pd.ticker
          LIMIT 50
        """)
        out["top_50_pre_fpfd_pollution_tickers"] = [{"ticker": r["ticker"], "bars": r["bars_before_fpfd"]} for r in rows]

        # 4h. Recent-window completeness — bars in last 30 sessions for tier1 + active stocks
        # First find what NYSE sessions exist recently
        sess = await c.fetch("""
          SELECT DISTINCT date FROM platform.prices_daily
          WHERE date > CURRENT_DATE - 60
          ORDER BY date DESC LIMIT 30
        """)
        recent_sessions = [r["date"] for r in sess]
        out["recent_session_window_size"] = len(recent_sessions)
        out["recent_session_range"] = {"oldest": recent_sessions[-1] if recent_sessions else None,
                                        "newest": recent_sessions[0] if recent_sessions else None}

        # 4i. source distribution
        rows = await c.fetch("""
          SELECT source, COUNT(*) FROM platform.prices_daily
          GROUP BY source ORDER BY COUNT(*) DESC LIMIT 10
        """)
        out["prices_daily_by_source"] = [{"source": r["source"], "n": r["count"]} for r in rows]

        # 4j. session_date date range
        r = await c.fetchrow("SELECT MIN(date) AS lo, MAX(date) AS hi FROM platform.prices_daily")
        out["date_range"] = dict(r)

    await pool.close()
    (OUT / "step4_prices_daily.json").write_text(json.dumps(out, indent=2, default=str))

    print("=== Step 4 prices_daily attribution ===")
    print(f"  total bars: {out['total_bars']:,}")
    print(f"  null classification_id: {out['null_classification_id']:,}")
    print(f"  orphan classification_id (not in ticker_classifications): {out['orphan_classification_id']:,}")
    print(f"  date range: {out['date_range']['lo']} → {out['date_range']['hi']}")
    print()
    bf = out["bars_before_fpfd"]
    print(f"  bars BEFORE assigned cls's first_public_filing_date: {bf['bars_before_fpfd']:,}")
    print(f"    (across {bf['tickers_affected']:,} tickers, {bf['classifications_affected']:,} classifications)")
    print(f"    = {100*bf['bars_before_fpfd']/out['total_bars']:.2f}% of all bars")
    al = out["bars_after_lifetime_end"]
    print(f"  bars AFTER assigned cls's lifetime_end: {al['bars_after_lifetime_end']:,}")
    print(f"    (across {al['tickers_affected']:,} tickers)")
    print()
    print(f"  bars OUTSIDE assigned cls's ticker_history valid window: "
          f"{out['bars_outside_ticker_history_window']:,}")
    alt = out["bars_attributed_to_wrong_cls_per_ticker_history"]
    print(f"  bars whose date+ticker resolves to a DIFFERENT cls via ticker_history: "
          f"{alt['rows']:,} (across {alt['tickers']:,} tickers)")
    print()
    print(f"  top 10 pre-FPFD pollution tickers:")
    for d in out["top_50_pre_fpfd_pollution_tickers"][:10]:
        print(f"    {d['ticker']:8s} {d['bars']:>5,} bars")
    print(f"\noutput: {OUT / 'step4_prices_daily.json'}")


asyncio.run(go())
