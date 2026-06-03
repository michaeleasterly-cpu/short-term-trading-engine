"""Step 3: deep-trace 10 known-bad tickers + spot-check 5 from the 702 multi-cls cohort."""
import asyncio
import json
import os
from pathlib import Path

import asyncpg

OUT = Path("/tmp/audit")
TIX = ['SBET','GLXY','COLAU','SUNC','ARDT','LZ','TRAW','FA','VIVK','SUNE']


async def trace(c, ticker: str) -> dict:
    o = {"ticker": ticker}
    # ALL ticker_classifications rows for this ticker
    rows = await c.fetch(
        "SELECT id, cik, asset_class, source, discovery_source, country, "
        "current_legal_name, lifetime_start, lifetime_end, first_public_filing_date, "
        "last_filing_date, ipo_venue "
        "FROM platform.ticker_classifications WHERE ticker = $1 "
        "ORDER BY lifetime_start NULLS LAST, lifetime_end NULLS LAST",
        ticker,
    )
    o["classifications"] = [dict(r) for r in rows]

    # ALL ticker_history rows
    rows = await c.fetch(
        "SELECT classification_id, valid_from, valid_to FROM platform.ticker_history "
        "WHERE ticker = $1 ORDER BY valid_from",
        ticker,
    )
    o["ticker_history"] = [dict(r) for r in rows]

    # ALL issuer_securities rows joined to classifications for this ticker
    rows = await c.fetch(
        "SELECT iss.issuer_id, iss.classification_id, iss.valid_from, iss.valid_to "
        "FROM platform.issuer_securities iss "
        "JOIN platform.ticker_classifications tc ON tc.id = iss.classification_id "
        "WHERE tc.ticker = $1 ORDER BY iss.valid_from",
        ticker,
    )
    o["issuer_securities"] = [dict(r) for r in rows]

    # prices_daily summary
    pd = await c.fetchrow(
        "SELECT COUNT(*) AS bars, COUNT(DISTINCT classification_id) AS distinct_cls, "
        "MIN(date) AS first_bar, MAX(date) AS last_bar "
        "FROM platform.prices_daily WHERE ticker = $1", ticker)
    o["prices_daily_summary"] = dict(pd)

    # Find the active classification for this ticker
    active = await c.fetchrow(
        "SELECT id, cik, first_public_filing_date FROM platform.ticker_classifications "
        "WHERE ticker = $1 AND lifetime_end IS NULL LIMIT 1", ticker)

    if active:
        # bars BEFORE current FPFD attributed to current entity
        if active["first_public_filing_date"]:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM platform.prices_daily "
                "WHERE ticker = $1 AND classification_id = $2 AND date < $3",
                ticker, active["id"], active["first_public_filing_date"])
            o["prices_daily_bars_before_FPFD_attributed_to_active"] = n
        # bars total before FPFD
        if active["first_public_filing_date"]:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM platform.prices_daily "
                "WHERE ticker = $1 AND date < $2",
                ticker, active["first_public_filing_date"])
            o["prices_daily_bars_before_FPFD_total"] = n

    # fundamentals_quarterly
    fq = await c.fetchrow(
        "SELECT COUNT(*) AS rows, MIN(period_end_date) AS earliest, MAX(period_end_date) AS latest "
        "FROM platform.fundamentals_quarterly WHERE ticker = $1", ticker)
    o["fundamentals_quarterly_summary"] = dict(fq)
    if active and active["first_public_filing_date"]:
        n = await c.fetchval(
            "SELECT COUNT(*) FROM platform.fundamentals_quarterly "
            "WHERE ticker = $1 AND period_end_date < $2",
            ticker, active["first_public_filing_date"])
        o["fundamentals_quarterly_rows_before_FPFD"] = n

    return o


async def go() -> None:
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL_IPV4") or os.environ["DATABASE_URL"],
        statement_cache_size=0,
    )
    async with pool.acquire() as c:
        out = []
        for t in TIX:
            out.append(await trace(c, t))
        # Also spot-check 5 tickers from the 702 multi-cls cohort
        multi = await c.fetch("""
          SELECT ticker FROM platform.ticker_classifications
          GROUP BY ticker HAVING COUNT(*) > 1
          ORDER BY COUNT(*) DESC, ticker LIMIT 5
        """)
        multi_traces = []
        for r in multi:
            multi_traces.append(await trace(c, r["ticker"]))
    await pool.close()

    (OUT / "step3_ticker_trace.json").write_text(
        json.dumps({"worst_offenders": out, "multi_cls_examples": multi_traces}, indent=2, default=str)
    )

    print("=== Step 3: 10 worst-offender tickers ===")
    for o in out:
        active_cls = [c for c in o["classifications"] if c["lifetime_end"] is None]
        retired_cls = [c for c in o["classifications"] if c["lifetime_end"] is not None]
        th = o["ticker_history"]
        pds = o["prices_daily_summary"]
        fqs = o["fundamentals_quarterly_summary"]
        print(f"  {o['ticker']:6s}: cls={len(active_cls)}A+{len(retired_cls)}R  th_rows={len(th)}  "
              f"pd_bars={pds['bars']:>5}  pd_distinct_cls={pds['distinct_cls']}  "
              f"fq_rows={fqs['rows']:>3}  "
              f"pd_bars<FPFD_to_active={o.get('prices_daily_bars_before_FPFD_attributed_to_active', '?')}  "
              f"fq_rows<FPFD={o.get('fundamentals_quarterly_rows_before_FPFD', '?')}")

    print()
    print("=== Step 3: 5 tickers with most classifications (multi-cls cohort) ===")
    for o in multi_traces:
        active_cls = [c for c in o["classifications"] if c["lifetime_end"] is None]
        retired_cls = [c for c in o["classifications"] if c["lifetime_end"] is not None]
        print(f"  {o['ticker']:6s}: cls={len(active_cls)}A+{len(retired_cls)}R  th_rows={len(o['ticker_history'])}")
        for c2 in o["classifications"]:
            print(f"    cls={c2['id']} cik={c2['cik']} legal={c2['current_legal_name']!r} "
                  f"life=[{c2['lifetime_start']}..{c2['lifetime_end']}] fpfd={c2['first_public_filing_date']} "
                  f"source={c2['source']!r}")

    print(f"\noutput: {OUT / 'step3_ticker_trace.json'}")


asyncio.run(go())
