"""Built-in ingestion-job handlers.

Each handler is an async callable ``(pool, config: dict) -> int | None``.
The return value is ``rows_ingested`` and lands in the
``INGESTION_COMPLETE`` event payload; ``None`` means "rows" doesn't
apply to this job (validation, e.g.). Any raised exception is captured
by the engine and recorded as ``last_error``. Handlers reuse the
existing single-purpose modules — this file is mostly glue.

Registry: :data:`HANDLERS` maps ``job_name`` → handler. The engine
looks up by name; jobs without a registered handler land in
``last_status = 'failed'`` with a clear error.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

HandlerFn = Callable[["asyncpg.Pool", dict[str, Any]], Awaitable["int | None"]]
"""Handlers return ``rows_ingested`` (or ``None`` if the metric doesn't apply,
e.g. validation). The engine threads the value into the application_log
``INGESTION_COMPLETE`` event so daily ops checks can see throughput."""


FUNDAMENTALS_ARCHIVE_FIELDS: tuple[str, ...] = (
    "ticker", "filing_date", "period_end_date", "period_label",
    "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
    "total_assets", "total_liabilities", "current_assets",
    "current_liabilities", "receivables", "cash_and_equivalents",
    "shares_outstanding", "pb", "de", "recorded_at",
)
"""Canonical CSV-archive column order for ``fmp_fundamentals``.

Must stay in lockstep with the data columns of
``platform.fundamentals_quarterly`` (minus the surrogate ``id``).
Exposed at module level so the schema-drift tests
(``test_handle_fundamentals_archive_e2e`` for CSV-header parity,
``test_handle_fundamentals_archive_db_schema`` for live-DB parity)
can import and assert against it — adding a column to the table
requires updating this tuple and the assertion will surface the gap."""


async def handle_data_validation(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
    """Run the Data Validation Suite. Raises if the suite fails.

    Returns ``None`` — "rows ingested" doesn't map to a validation pass."""
    from tpcore.quality.validation.suite import run_suite

    result = await run_suite(pool)
    if not result.passed:
        # The suite already wrote per-check rows to platform.data_quality_log;
        # the exception is so the engine records last_status='failed'.
        raise RuntimeError(f"validation suite failed: {result}")
    return None


_FUNDAMENTALS_FETCH_INTER_SYMBOL_SLEEP_SEC = 1.0


def _payload_to_archive_rows(symbol: str, payload: dict) -> list[dict]:
    """Flatten one FMP payload to one CSV-archive row per period.

    The payload's shape is ``{latest_period_fields..., "history": [...]}``.
    Each period dict (latest + every history entry) becomes one row.
    Periods missing ``filing_date`` are dropped (matches the legacy
    ``_upsert_payload`` gate). Numeric fields are str-encoded; the
    ETL side parses back as needed by ``_upsert_payload``."""
    periods: list[dict] = [{k: v for k, v in payload.items() if k != "history"}]
    for h in payload.get("history") or []:
        periods.append(h)
    out: list[dict] = []
    for p in periods:
        if p.get("filing_date") is None:
            continue
        out.append({
            "ticker": symbol,
            "filing_date": str(p.get("filing_date") or ""),
            "period_end_date": str(p.get("period_end_date") or ""),
            "period_label": str(p.get("period") or ""),
            "net_income": "" if p.get("net_income") is None else str(p.get("net_income")),
            "fcf": "" if p.get("fcf") is None else str(p.get("fcf")),
            "operating_cash_flow": "" if p.get("operating_cash_flow") is None else str(p.get("operating_cash_flow")),
            "capex": "" if p.get("capex") is None else str(p.get("capex")),
            "revenue": "" if p.get("revenue") is None else str(p.get("revenue")),
            "total_assets": "" if p.get("total_assets") is None else str(p.get("total_assets")),
            "total_liabilities": "" if p.get("total_liabilities") is None else str(p.get("total_liabilities")),
            "current_assets": "" if p.get("current_assets") is None else str(p.get("current_assets")),
            "current_liabilities": "" if p.get("current_liabilities") is None else str(p.get("current_liabilities")),
            "receivables": "" if p.get("receivables") is None else str(p.get("receivables")),
            "cash_and_equivalents": "" if p.get("cash_and_equivalents") is None else str(p.get("cash_and_equivalents")),
            "shares_outstanding": "" if p.get("shares_outstanding") is None else str(p.get("shares_outstanding")),
            # pb + de are derived columns on the DB side, not in the
            # FMP payload — they default to empty in the archive so the
            # CSV-header parity test stays green; the upsert path
            # doesn't read them either.
            "pb": "",
            "de": "",
            "recorded_at": "",
        })
    return out


def _archive_rows_to_payload(rows: list[dict]) -> dict:
    """Reconstruct a payload-shaped dict for one symbol from its CSV
    rows. The first row becomes the latest period; remaining rows
    become ``history``. Inverse of :func:`_payload_to_archive_rows`
    (lossy on the derived pb/de columns which the upsert doesn't read).
    """
    from datetime import date as _date_t
    from decimal import Decimal as _Decimal

    def _to_period(r: dict) -> dict:
        def _opt(k: str) -> object:
            v = r.get(k)
            return None if v in (None, "") else v

        def _opt_decimal(k: str) -> object:
            v = r.get(k)
            return None if v in (None, "") else _Decimal(v)

        def _opt_date(k: str) -> object:
            v = r.get(k)
            return None if v in (None, "") else _date_t.fromisoformat(v)

        return {
            "filing_date": _opt_date("filing_date"),
            "period_end_date": _opt_date("period_end_date"),
            "period": _opt("period_label"),
            "net_income": _opt_decimal("net_income"),
            "fcf": _opt_decimal("fcf"),
            "operating_cash_flow": _opt_decimal("operating_cash_flow"),
            "capex": _opt_decimal("capex"),
            "revenue": _opt_decimal("revenue"),
            "total_assets": _opt_decimal("total_assets"),
            "total_liabilities": _opt_decimal("total_liabilities"),
            "current_assets": _opt_decimal("current_assets"),
            "current_liabilities": _opt_decimal("current_liabilities"),
            "receivables": _opt_decimal("receivables"),
            "cash_and_equivalents": _opt_decimal("cash_and_equivalents"),
            "shares_outstanding": _opt_decimal("shares_outstanding"),
        }

    if not rows:
        return {}
    latest = _to_period(rows[0])
    history = [_to_period(r) for r in rows[1:]]
    return {**latest, "history": history}


async def handle_fundamentals_refresh(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Refresh FMP fundamentals for the active universe.

    Archive-first contract (P1-sibling trust-audit 2026-05-25): the
    legacy flow called ``cache.backfill_all()`` (per-ticker fetch +
    DB upsert interleaved), then post-hoc dumped the DB rows to the
    archive — violating the archive-as-substrate invariant. New
    flow: Phase 0 pre-fetches every symbol's payload into memory,
    Phase 1 writes archive + manifest='archived', Phase 2 reads the
    on-disk archive and per-symbol upserts via ``cache.upsert_payload``,
    Phase 3 marks the manifest loaded/failed.

    ``config`` is currently unused — the cache reads the active
    universe from ``platform.prices_daily`` directly. The seed
    payload's ``{"universe": "active"}`` documents intent.
    """
    import asyncio

    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fmp.fundamentals_adapter import DataProviderOutage
    from tpcore.fundamentals.cache import FundamentalsCache
    from tpcore.ingestion.archive_etl import manifest_lifecycle, read_archive_csv

    today = datetime.now(UTC).date()
    archive_rows: list[dict] = []
    no_data: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    skipped = 0

    async with FMPFundamentalsAdapter() as adapter:
        cache = FundamentalsCache(pool, adapter=adapter)

        # Phase 0: pre-fetch every active-universe ticker's payload
        # into memory. No DB writes. Respects the same skip-fresh +
        # rate-limit pacing the legacy backfill_all did.
        tickers = await cache.list_active_tickers()
        already_fresh = await cache.tickers_refreshed_within(tickers, hours=24.0)
        for i, symbol in enumerate(tickers, start=1):
            if symbol.upper() in already_fresh:
                skipped += 1
                continue
            try:
                payload = await cache.fetch_payload(symbol)
            except DataProviderOutage as exc:
                msg = str(exc)
                bucket = (
                    no_data
                    if ("no usable fundamentals" in msg or "returned 402" in msg)
                    else failures
                )
                bucket.append((symbol, msg[:160]))
                await asyncio.sleep(_FUNDAMENTALS_FETCH_INTER_SYMBOL_SLEEP_SEC)
                continue
            archive_rows.extend(_payload_to_archive_rows(symbol, payload))
            logger.debug(
                "fundamentals.fetch_payload",
                symbol=symbol, done=i, total=len(tickers),
            )
            await asyncio.sleep(_FUNDAMENTALS_FETCH_INTER_SYMBOL_SLEEP_SEC)

    # Phases 1–3 under the canonical lifecycle.
    total_rows = 0
    archive_path_str: str | None = None
    async with manifest_lifecycle(
        pool,
        source="fmp_fundamentals",
        provider="fmp",
        archive_rows=archive_rows,
        fieldnames=list(FUNDAMENTALS_ARCHIVE_FIELDS),
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("period_end_date")),
        date_range_end=today,
    ) as ctx:
        archive_path_str = str(ctx.archive_path)
        csv_rows = read_archive_csv(ctx)
        by_ticker: dict[str, list[dict]] = {}
        for r in csv_rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        # Reuse the existing _upsert_payload contract: reconstruct
        # payload-shaped dicts per symbol from the archive rows.
        async with FMPFundamentalsAdapter() as upsert_adapter:
            upsert_cache = FundamentalsCache(pool, adapter=upsert_adapter)
            for symbol, rows in by_ticker.items():
                payload = _archive_rows_to_payload(rows)
                if not payload:
                    continue
                total_rows += await upsert_cache.upsert_payload(symbol, payload)
        ctx.actual_rows = total_rows

    logger.info(
        "ingestion.handler.fundamentals_done",
        rows=total_rows,
        no_data=len(no_data),
        failures=len(failures),
        skipped_fresh=skipped,
        csv_archive=archive_path_str,
    )
    if failures:
        # ETF skips (no_data) are expected and silent. Real FMP outages
        # bubble up so the engine records the run as failed.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} real failure(s); "
            f"first={failures[0][0]}: {failures[0][1]}"
        )
    return total_rows


SEC_FUNDAMENTALS_ARCHIVE_FIELDS: tuple[str, ...] = (
    "ticker", "cik", "filing_date", "period_end_date",
    "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
    "total_assets", "total_liabilities", "current_assets",
    "current_liabilities", "receivables", "cash_and_equivalents",
    "shares_outstanding", "recorded_at",
)
"""CSV-archive column order for the ``sec_edgar_fundamentals_fallback``
source. Distinct from ``FUNDAMENTALS_ARCHIVE_FIELDS`` because SEC
companyfacts has no ``period_label`` (Q1/Q2/...) and ``cik`` is
informational. Same financial columns + ``recorded_at`` so a row replays
cleanly through ``platform.fundamentals_quarterly``'s upsert."""

_SEC_FUNDAMENTALS_INTER_TICKER_SLEEP_SEC = 0.12
"""SEC fair-use guidance: ≤10 req/sec unauthenticated. 0.12s sleep =
8 req/sec, comfortable margin. Per ``feedback_bulk_before_api_crawl_REINFORCED``:
SEC has a bulk companyfacts.zip (~3 GB) — for the typical FMP-gap
follow-up the per-ticker call count is small (one HTTP call per CIK
returns ALL XBRL facts) so bulk is overkill. Revisit if scope widens."""


async def handle_sec_fundamentals_fallback(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """SEC EDGAR companyfacts → fundamentals_quarterly fallback.

    Cascade fallback for ``fundamentals_quarterly_completeness`` when
    FMP's 3-endpoint merge leaves period gaps (pre-IPO predecessors,
    recent IPOs with sparse balance-sheet history, etc.).

    Universe: tier-≤2 active stocks with a non-NULL CIK in
    ``platform.ticker_classifications``. Per-ticker: enumerate the
    inferred-missing period_ends (same gap-inference the completeness
    check uses), fetch ``data.sec.gov/api/xbrl/companyfacts/CIK<n>.json``
    once per CIK, extract the canonical financial fields for each
    missing period, archive to R2 + upsert into platform.fundamentals_quarterly.

    Config keys (all optional):
      * ``tickers``: comma-separated list to scope to a specific
        subset. Default: all tier-≤2 active stocks with CIK.
      * ``include_no_gap_tickers`` (bool, default False): when True,
        also fetch SEC facts for tickers without inferred gaps (useful
        for a deep-history first-time backfill). Default skips them to
        keep the daily cascade cheap.

    Per memory ``feedback_sec_authoritative_fmp_fallback_non_us``: SEC
    is the US-filer authoritative source. Non-US tickers without CIKs
    are out-of-scope here (the universe filter excludes them).

    Per ``.claude/rules/data-adapter.md``: backfills run through this
    canonical stage (``python scripts/ops.py --stage sec_fundamentals_fallback``)
    — NEVER as a one-off script.
    """
    import asyncio
    from datetime import date as _date_t

    from tpcore.fundamentals.cache import FundamentalsCache
    from tpcore.ingestion.archive_etl import manifest_lifecycle, read_archive_csv
    from tpcore.outage import DataProviderOutage
    from tpcore.sec.companyfacts_adapter import SECCompanyFactsAdapter

    today = datetime.now(UTC).date()
    include_no_gap = bool(config.get("include_no_gap_tickers", False))
    ticker_filter = config.get("tickers")
    ticker_filter_list: list[str] | None = (
        [t.strip().upper() for t in str(ticker_filter).split(",") if t.strip()]
        if ticker_filter else None
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT tc.ticker, tc.cik
            FROM platform.ticker_classifications tc
            JOIN platform.liquidity_tiers lt ON lt.ticker = tc.ticker
            WHERE tc.asset_class = 'stock'
              AND (tc.lifetime_end IS NULL OR tc.lifetime_end > CURRENT_DATE)
              AND lt.tier <= 2
              AND tc.cik IS NOT NULL AND tc.cik <> ''
            ORDER BY tc.ticker
            """
        )
    candidates = [(r["ticker"], r["cik"]) for r in rows]
    if ticker_filter_list:
        wanted = set(ticker_filter_list)
        candidates = [(t, c) for t, c in candidates if t in wanted]
    logger.info(
        "ingestion.handler.sec_fundamentals_fallback.universe",
        tier2_with_cik=len(candidates),
        ticker_filter=len(ticker_filter_list or []),
    )

    import calendar as _cal

    async def _missing_periods_for(t: str) -> list[_date_t]:
        async with pool.acquire() as cx:
            r = await cx.fetch(
                "SELECT period_end_date FROM platform.fundamentals_quarterly "
                "WHERE ticker = $1 ORDER BY period_end_date",
                t,
            )
        have = sorted({row["period_end_date"] for row in r})
        if len(have) < 2:
            return []
        out: list[_date_t] = []
        for i in range(1, len(have)):
            a, b = have[i - 1], have[i]
            if (b - a).days <= 100:
                continue
            cur_y, cur_m = a.year, a.month
            for _ in range(40):  # safety bound
                next_m = cur_m + 3
                next_y = cur_y + (next_m - 1) // 12
                next_m = ((next_m - 1) % 12) + 1
                # Calendar-correct last-day-of-month (was a buggy fixed
                # dict {3,6,9,12}: 31/30/30/31 with 30 fallback — crashed
                # on Feb fiscal-year-end filers with "day is out of range
                # for month" on date(y, 2, 30)). Handles leap years too.
                last_day = _cal.monthrange(next_y, next_m)[1]
                candidate = _date_t(next_y, next_m, last_day)
                if candidate >= b:
                    break
                if candidate > a:
                    out.append(candidate)
                cur_y, cur_m = candidate.year, candidate.month
        return out

    archive_rows: list[dict] = []
    no_data: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    nothing_to_fill: list[str] = []

    async with SECCompanyFactsAdapter() as sec:
        for i, (ticker, cik) in enumerate(candidates, start=1):
            missing = await _missing_periods_for(ticker)
            if not missing and not include_no_gap:
                nothing_to_fill.append(ticker)
                continue
            try:
                facts = await sec.get_companyfacts(cik)
            except DataProviderOutage as exc:
                failures.append((ticker, str(exc)[:160]))
                await asyncio.sleep(_SEC_FUNDAMENTALS_INTER_TICKER_SLEEP_SEC)
                continue
            if facts is None:
                no_data.append((ticker, f"CIK {cik} returned 404 (no XBRL on file)"))
                await asyncio.sleep(_SEC_FUNDAMENTALS_INTER_TICKER_SLEEP_SEC)
                continue

            filled = 0
            for pe in missing:
                extracted = sec.extract_period(facts, pe)
                if extracted is None:
                    continue
                # SEC companyfacts doesn't expose a single canonical
                # filing_date per fact (every fact carries its own
                # ``filed`` per submission). For the upsert we use
                # period_end as filing_date so the ON CONFLICT
                # (ticker, filing_date) key is stable + idempotent.
                # The platform's `fundamentals_integrity` check uses
                # period_end <= filing_date which is satisfied trivially.
                filing = pe
                if filing > today:
                    continue
                shares = extracted["shares_outstanding"]
                if shares is not None and shares <= 0:
                    continue
                archive_rows.append({
                    "ticker": ticker,
                    "cik": cik,
                    "filing_date": filing.isoformat(),
                    "period_end_date": pe.isoformat(),
                    "net_income": _str_or_blank(extracted["net_income"]),
                    "fcf": _str_or_blank(extracted["fcf"]),
                    "operating_cash_flow": _str_or_blank(extracted["operating_cash_flow"]),
                    "capex": _str_or_blank(extracted["capex"]),
                    "revenue": _str_or_blank(extracted["revenue"]),
                    "total_assets": _str_or_blank(extracted["total_assets"]),
                    "total_liabilities": _str_or_blank(extracted["total_liabilities"]),
                    "current_assets": _str_or_blank(extracted["current_assets"]),
                    "current_liabilities": _str_or_blank(extracted["current_liabilities"]),
                    "receivables": _str_or_blank(extracted["receivables"]),
                    "cash_and_equivalents": _str_or_blank(extracted["cash_and_equivalents"]),
                    "shares_outstanding": _str_or_blank(shares),
                    "recorded_at": "",
                })
                filled += 1
            if i % 25 == 0:
                logger.info(
                    "ingestion.handler.sec_fundamentals_fallback.progress",
                    done=i, total=len(candidates),
                    ticker=ticker, filled_this_ticker=filled,
                    archive_rows=len(archive_rows),
                )
            await asyncio.sleep(_SEC_FUNDAMENTALS_INTER_TICKER_SLEEP_SEC)

    logger.info(
        "ingestion.handler.sec_fundamentals_fallback.phase0_done",
        archive_rows=len(archive_rows),
        no_data=len(no_data),
        failures=len(failures),
        nothing_to_fill=len(nothing_to_fill),
    )

    if not archive_rows:
        return 0

    # Phases 1-3 under the canonical lifecycle. Same pattern as
    # handle_fundamentals_refresh; archive lands in R2 (or local FS
    # depending on CSV_ARCHIVE_BACKEND), Phase 2 reads back + per-symbol
    # upserts via the cache's contract.
    total_rows = 0
    archive_path_str: str | None = None
    async with manifest_lifecycle(
        pool,
        source="sec_edgar_fundamentals_fallback",
        provider="sec_edgar",
        archive_rows=archive_rows,
        fieldnames=list(SEC_FUNDAMENTALS_ARCHIVE_FIELDS),
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("period_end_date")),
        date_range_end=today,
    ) as ctx:
        archive_path_str = str(ctx.archive_path)
        csv_rows = read_archive_csv(ctx)
        # Group archive rows by ticker so we can reuse the existing
        # _upsert_payload contract — same shape as the FMP handler.
        by_ticker: dict[str, list[dict]] = {}
        for r in csv_rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        cache = FundamentalsCache(pool, adapter=None)
        for symbol, ticker_rows in by_ticker.items():
            payload = _sec_archive_rows_to_payload(ticker_rows)
            if not payload:
                continue
            total_rows += await cache.upsert_payload(symbol, payload)
        ctx.actual_rows = total_rows

    logger.info(
        "ingestion.handler.sec_fundamentals_fallback.done",
        rows=total_rows,
        no_data=len(no_data),
        failures=len(failures),
        csv_archive=archive_path_str,
    )
    if failures:
        raise RuntimeError(
            f"sec_fundamentals_fallback: {len(failures)} real failure(s); "
            f"first={failures[0][0]}: {failures[0][1]}"
        )
    return total_rows


def _str_or_blank(v: object) -> str:
    return "" if v is None else str(v)


def _sec_archive_rows_to_payload(rows: list[dict]) -> dict:
    """Reconstruct a FundamentalsCache-style payload from SEC archive
    rows. First row → latest period; rest → history. Numeric fields
    parse as Decimal; date fields parse as date. Mirrors
    ``_archive_rows_to_payload`` (FMP) — SEC just has a different
    archive schema."""
    from datetime import date as _date_t
    from decimal import Decimal as _Decimal

    def _opt_decimal(r: dict, k: str) -> object:
        v = r.get(k)
        return None if v in (None, "") else _Decimal(v)

    def _opt_date(r: dict, k: str) -> object:
        v = r.get(k)
        return None if v in (None, "") else _date_t.fromisoformat(v)

    def _to_period(r: dict) -> dict:
        return {
            "filing_date": _opt_date(r, "filing_date"),
            "period_end_date": _opt_date(r, "period_end_date"),
            "period": None,  # SEC doesn't expose Q1/Q2/... label here
            "net_income": _opt_decimal(r, "net_income"),
            "fcf": _opt_decimal(r, "fcf"),
            "operating_cash_flow": _opt_decimal(r, "operating_cash_flow"),
            "capex": _opt_decimal(r, "capex"),
            "revenue": _opt_decimal(r, "revenue"),
            "total_assets": _opt_decimal(r, "total_assets"),
            "total_liabilities": _opt_decimal(r, "total_liabilities"),
            "current_assets": _opt_decimal(r, "current_assets"),
            "current_liabilities": _opt_decimal(r, "current_liabilities"),
            "receivables": _opt_decimal(r, "receivables"),
            "cash_and_equivalents": _opt_decimal(r, "cash_and_equivalents"),
            "shares_outstanding": _opt_decimal(r, "shares_outstanding"),
        }

    if not rows:
        return {}
    periods = [_to_period(r) for r in rows]
    head = periods[0]
    out = dict(head)
    out["history"] = periods[1:]
    out["symbol"] = rows[0].get("ticker", "")
    return out


_CORPORATE_ACTIONS_50_NAME_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)
"""Original 50-name backtest universe — kept in sync with
``ops/cron_corporate_actions.py:UNIVERSE``. Default for back-compat;
``config.universe = "all_active"`` overrides to the full prices_daily set."""


_CORPORATE_ACTIONS_FIELDS: list[str] = [
    "ticker", "action_date", "action_type", "ratio", "raw_data",
]


def _action_to_archive_row(action: dict) -> dict:
    """Serialize a normalized corporate-action dict for the CSV archive.

    The normalized shape (from ``fetch_corporate_actions._normalize_*``)
    is ``{ticker, action_date(date), action_type, ratio(Decimal),
    raw_data(dict)}``. We round-trip it through CSV by encoding
    ``action_date`` as ISO, ``ratio`` as a stringified Decimal, and
    ``raw_data`` as a full JSON blob (NO truncation — the pre-P1
    handler clipped to 500 chars, which broke archive-as-substrate
    round-tripping for actions whose raw payload exceeded that)."""
    return {
        "ticker": action["ticker"],
        "action_date": action["action_date"].isoformat(),
        "action_type": action["action_type"],
        "ratio": str(action["ratio"]),
        "raw_data": json.dumps(action["raw_data"], default=str),
    }


def _archive_row_to_action(row: dict[str, str]) -> dict:
    """Parse a CSV-archive row back into the upsert-shaped action
    dict. Inverse of :func:`_action_to_archive_row`."""
    from datetime import date as _date_t
    from decimal import Decimal
    return {
        "ticker": row["ticker"],
        "action_date": _date_t.fromisoformat(row["action_date"]),
        "action_type": row["action_type"],
        "ratio": Decimal(row["ratio"]),
        "raw_data": json.loads(row.get("raw_data") or "{}"),
    }


async def handle_corporate_actions(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Pull Alpaca corporate actions and re-apply splits to ``platform.prices_daily``.

    Archive-first contract (P1 trust-audit 2026-05-25): all action
    fetches accumulate into an in-memory list. The archive lands on
    disk + a ``platform.ingest_manifest`` row is INSERTed with
    status='archived' BEFORE any production write. ETL re-parses
    the on-disk archive and calls ``upsert_corporate_actions``;
    manifest moves to 'loaded' on success / 'failed' on exception.
    The legacy 500-char truncation on ``raw_data`` is dropped —
    archive-as-substrate requires lossless round-tripping.

    ``config`` keys:
        * ``universe``:
            - default (omitted): the 50-name backtest universe.
            - ``"all_active"``: every distinct ticker in ``prices_daily``.
              Apply-splits then runs against the full table (no ticker
              filter), so Tradier-sourced bars get adjusted too.
            - ``list[str]``: explicit ticker override.
        * ``ingest_start``: ISO date, default ``"2018-01-01"``.
    """
    import httpx

    from tpcore.data.apply_splits import apply_all_splits
    from tpcore.data.ingest_alpaca_bars import _alpaca_headers
    from tpcore.data.ingest_corporate_actions import (
        DEFAULT_TYPES,
        fetch_corporate_actions,
        upsert_corporate_actions,
    )
    from tpcore.ingestion.archive_etl import manifest_lifecycle, read_archive_csv

    universe_cfg = config.get("universe", "default")
    if universe_cfg == "all_active":
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.prices_daily ORDER BY ticker"
            )
        universe: tuple[str, ...] = tuple(r["ticker"] for r in rows)
        apply_filter: list[str] | None = None
    elif isinstance(universe_cfg, list):
        universe = tuple(str(s).upper() for s in universe_cfg)
        apply_filter = list(universe)
    else:
        universe = _CORPORATE_ACTIONS_50_NAME_UNIVERSE
        apply_filter = list(universe)

    chunk_size = 20
    today = datetime.now(UTC).date()
    ingest_start = config.get("ingest_start", "2018-01-01")
    if isinstance(ingest_start, str):
        from datetime import date as date_t

        ingest_start = date_t.fromisoformat(ingest_start)

    headers = _alpaca_headers()
    archive_rows: list[dict] = []
    async with httpx.AsyncClient(
        headers=headers,
        base_url="https://data.alpaca.markets",
        timeout=60.0,
    ) as client:
        for i in range(0, len(universe), chunk_size):
            chunk = list(universe[i : i + chunk_size])
            actions = await fetch_corporate_actions(
                client,
                symbols=chunk,
                start=ingest_start,
                end=today,
                types=list(DEFAULT_TYPES),
            )
            for a in actions:
                archive_rows.append(_action_to_archive_row(a))

    # Archive-first lifecycle: Phase 1 (archive + manifest='archived')
    # → Phase 2 (read archive + upsert) → Phase 3 (mark loaded/failed).
    # A failed Phase 1 (archive write or manifest INSERT) blocks the
    # production write entirely; a failed Phase 2 leaves manifest at
    # 'failed' with the archive preserved on disk.
    total_actions = 0
    archive_path_str: str | None = None
    async with manifest_lifecycle(
        pool,
        source="alpaca_corporate_actions",
        provider="alpaca",
        archive_rows=archive_rows,
        fieldnames=_CORPORATE_ACTIONS_FIELDS,
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("action_type")),
        date_range_start=ingest_start,
        date_range_end=today,
    ) as ctx:
        archive_path_str = str(ctx.archive_path)
        # Phase 2: read archive FROM DISK, reconstruct action dicts,
        # call the canonical upsert. ETL must read the file (not the
        # in-memory archive_rows the manifest was built from) — that's
        # the archive-as-substrate invariant.
        parsed_actions = [
            _archive_row_to_action(r) for r in read_archive_csv(ctx)
        ]
        total_actions = await upsert_corporate_actions(pool, parsed_actions)
        ctx.actual_rows = total_actions

    # Post-load: shrinkage detection + D2 metrics. These read the
    # already-written archive — they're observational, not gating.
    # Kept here (not inside manifest_lifecycle) so a shrinkage signal
    # doesn't roll back a successful load; the gating remains the
    # 100%-green-or-don't-trade selfheal contract.
    from tpcore.ingestion.csv_archive import (
        assert_not_shrunk,
        detect_shrinkage,
        log_shrinkage_warning,
    )
    archive_path_p = Path(archive_path_str) if archive_path_str else None
    shrinkage = (
        detect_shrinkage(
            "alpaca_corporate_actions", total_actions, exclude_path=archive_path_p,
        ) if archive_path_p else None
    )
    if shrinkage is not None:
        log_shrinkage_warning(shrinkage)
        assert_not_shrunk(shrinkage)  # producer hard-stop on a short snapshot

    # D2 substrate: durable per-source metrics + rolling-median check
    # in PARALLEL with the v1 single-prior detector.
    from tpcore.ingestion.d2_metrics import (
        check_shrinkage_vs_rolling_median,
        detectors_disagree,
        record_ingestion_metrics,
    )
    v2_verdict = await check_shrinkage_vs_rolling_median(
        pool, "alpaca_corporate_actions", total_actions,
    )
    if shrinkage is not None and detectors_disagree(
        shrinkage.over_threshold, v2_verdict,
    ):
        logger.warning(
            "SHRINKAGE_DETECTORS_DISAGREE",
            source="alpaca_corporate_actions",
            v1_over_threshold=shrinkage.over_threshold,
            v2_shrunk=v2_verdict.shrunk,
            v1_shrinkage_pct=round(shrinkage.shrinkage_pct, 4),
            v2_shrinkage_pct=round(v2_verdict.shrinkage_pct, 4),
            v2_median_rows=v2_verdict.median_rows,
            v2_samples_used=v2_verdict.samples_used,
        )
    await record_ingestion_metrics(
        pool, "alpaca_corporate_actions", total_actions,
    )

    split_summary = await apply_all_splits(pool, only_tickers=apply_filter)
    logger.info(
        "ingestion.handler.corporate_actions_done",
        universe_mode=universe_cfg if isinstance(universe_cfg, str) else "list",
        universe_size=len(universe),
        actions_ingested=total_actions,
        splits_applied=len(split_summary["applied"]),
        splits_skipped=len(split_summary["skipped"]),
        csv_archive=archive_path_str,
        shrinkage_over_threshold=shrinkage.over_threshold if shrinkage else False,
    )
    return total_actions


async def handle_daily_bars(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
    """Incremental daily-bar refresh for the active universe.

    Pulls the last ``lookback_days`` (default 7) of bars from Alpaca for
    every ticker that has a recent bar in ``platform.prices_daily`` and
    is not flagged delisted. The 7-day window is overlap insurance —
    Alpaca's IEX free tier occasionally restates a bar a day or two
    later.

    ``config`` keys:
        * ``universe``:
            - ``"active"`` (default): reads tickers from prices_daily.
            - ``"all_active"``: discovery-mode sweep — enumerate every
              tradable Alpaca asset on NYSE/NASDAQ and apply a coarse
              price/volume filter. New tickers that pass the filter
              enter ``platform.prices_daily``.
            - ``list[str]``: explicit ticker override.
        * ``lookback_days``: int, default 7.
        * ``end_offset_days``: int, default 0. Shifts the request window's
          end date back by N days relative to "today". Use ``1`` for
          mid-session backfills — Alpaca's SIP free tier returns 403
          when ``end=today`` during regular hours (intraday data
          subscription required), but historical ``end=yesterday`` is
          fine. The after-hours scheduled run leaves this at 0 since
          21:30 UTC ≈ 17:30 ET is post-close.
        * ``min_price`` / ``min_volume``: coarse filter floors (only
          consulted on the ``all_active`` path). Defaults: $5, 250k.
        * ``batch_size`` / ``inter_batch_sleep_sec``: ``all_active``
          batching. Defaults: 50, 0.3.
    """
    universe_cfg = config.get("universe", "active")
    if universe_cfg == "all_active":
        return await _handle_daily_bars_all_active(pool, config)
    return await _handle_daily_bars_explicit(pool, config, universe_cfg)


async def _handle_daily_bars_explicit(
    pool: asyncpg.Pool,
    config: dict[str, Any],
    universe_cfg: Any,
) -> int:
    """The 'active' / list-of-tickers code path. Multi-symbol fetches.

    Lifted out of ``handle_daily_bars`` so the discovery sweep
    (``all_active``) can live as its own helper without making the entry
    point unreadable.

    Two adapter paths gated on ``config["feed"]`` (default ``"fmp"``):

    * ``feed="fmp"`` (DEFAULT 2026-05-22+): per-ticker calls to FMP's
      ``/stable/historical-price-eod/full`` endpoint. Full CTA
      consolidated tape on the operator's $200/year Starter tier (no
      batch endpoint at that tier — per-symbol only). ~25 min wall
      time for the ~7,600-ticker universe at 200ms/call.
    * ``feed="iex" | "sip"`` (legacy / diagnostic): Alpaca's
      ``/v2/stocks/bars?symbols=…`` multi endpoint in 100-symbol chunks
      (2026-05-15). Kept available so the operator can A/B against the
      Alpaca path without redeploying.

    Each path uses the same downstream ``_upsert_bars`` + CSV-archive
    collector — the only difference is which transport fans out the
    universe and at what granularity. Per-chunk (Alpaca) or per-symbol
    (FMP) failure is logged + the run continues; the aggregate
    ``RuntimeError`` only fires when ≥1 chunk/symbol permanently fails.
    """
    from datetime import timedelta

    feed = str(config.get("feed", "fmp"))
    lookback_days = int(config.get("lookback_days", 7))
    end_offset_days = int(config.get("end_offset_days", 0))

    if universe_cfg == "active":
        sql = """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
              AND delisted = false
            ORDER BY ticker
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
        symbols = [r["ticker"] for r in rows]
    elif isinstance(universe_cfg, list):
        symbols = [str(s).upper() for s in universe_cfg]
    elif isinstance(universe_cfg, str) and "," in universe_cfg:
        # Explicit comma-separated set. The canonical --param channel
        # coerces to scalars only, so a targeted list arrives here as a
        # CSV string. This is the bounded coverage-collapse repair the
        # pipeline previously couldn't express (repair_gaps is blind to
        # a freshness coverage_collapse; a full force_refresh times out
        # at 3600s — proven 2026-05-17). Re-pulling only the missing
        # tail is fast and canonical.
        symbols = [s.strip().upper() for s in universe_cfg.split(",") if s.strip()]
    else:
        raise ValueError(
            f"daily_bars: unsupported universe config {universe_cfg!r} — "
            "expected 'active', 'all_active', a CSV string, or a list"
        )

    today = datetime.now(UTC).date()
    end = today - timedelta(days=end_offset_days)
    start = end - timedelta(days=lookback_days)

    # Archive-first contract (P1 trust-audit 2026-05-25): collectors
    # _fetch_via_fmp / _fetch_via_alpaca no longer touch the production
    # DB. They return the accumulated bars list; the archive_first_load
    # orchestrator writes the CSV+manifest BEFORE the upsert, and reads
    # production rows BACK FROM the on-disk archive (not from the
    # in-memory list). A failed archive write blocks the upsert; a
    # failed ETL leaves the manifest row at status=FAILED with the
    # archive preserved on disk.
    from tpcore.ingestion.archive_etl import archive_first_load_bars

    if feed == "fmp":
        archive_rows, failures = await _fetch_via_fmp(symbols, start, end)
        source_label = "fmp_daily_bars"
        provider_label = "fmp"
    else:
        archive_rows, failures = await _fetch_via_alpaca(symbols, start, end, feed=feed)
        source_label = "alpaca_daily_bars"
        provider_label = "alpaca"

    total_rows, archive_path = await archive_first_load_bars(
        pool,
        archive_rows=archive_rows,
        source=source_label,
        provider=provider_label,
        date_range_start=start,
        date_range_end=end,
    )

    logger.info(
        "ingestion.handler.daily_bars_done",
        feed=feed,
        symbols=len(symbols),
        rows_upserted=total_rows,
        failures=len(failures),
        csv_archive=str(archive_path),
    )
    if failures:
        raise RuntimeError(
            f"daily_bars[{feed}]: {len(failures)} fetch failure(s); first: {failures[0]}"
        )
    return total_rows


async def _fetch_via_fmp(
    symbols: list[str],
    start,  # noqa: ANN001 — date; avoiding circular forward-decl friction
    end,  # noqa: ANN001
) -> tuple[list[dict], list[str]]:
    """Per-symbol FMP daily-bars fan-out — archive-first collector.

    P1 trust-audit (2026-05-25): this function no longer writes to
    production. It pulls bars from FMP and accumulates them into
    ``archive_rows``. The caller writes the archive CSV + manifest
    BEFORE any production write happens.

    FMP's /stable tier has no multi-symbol EOD batch — we issue one
    call per ticker. The adapter's internal 200ms inter-call sleep
    keeps us under the 300 req/min Starter cap. A single ticker's
    permanent failure (auth, malformed key) is recorded and the run
    continues; the aggregate failure list reaches the caller for
    final disposition.
    """
    import httpx

    from tpcore.data.ingest_fmp_bars import fetch_daily_bars_multi as fmp_fetch

    failures: list[str] = []
    archive_rows: list[dict] = []

    # FMP per-symbol calls are not chunked by transport — we hand
    # the whole symbol list to the adapter which paces itself.
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            by_symbol = await fmp_fetch(client, symbols, start, end)
        except Exception as exc:  # noqa: BLE001 — adapter raised a permanent failure
            failures.append(f"fmp_fetch_aborted({type(exc).__name__}:{str(exc)[:120]})")
            return archive_rows, failures

    for symbol, bars in by_symbol.items():
        if not bars:
            continue
        for b in bars:
            archive_rows.append({
                "ticker": symbol, "date": b.get("t", ""),
                "open": b.get("o", ""), "high": b.get("h", ""),
                "low": b.get("l", ""), "close": b.get("c", ""),
                "volume": b.get("v", ""), "vwap": b.get("vw", ""),
            })

    return archive_rows, failures


async def _fetch_via_alpaca(
    symbols: list[str],
    start,  # noqa: ANN001
    end,  # noqa: ANN001
    *,
    feed: str,
) -> tuple[list[dict], list[str]]:
    """Legacy 100-symbol-chunked Alpaca path — archive-first collector.

    P1 trust-audit (2026-05-25): no longer writes to production.
    Returns ``(archive_rows, failures)``; the caller's
    ``archive_first_load_bars`` orchestrator writes the archive +
    manifest BEFORE any production write.

    Kept available behind ``--param feed=iex|sip`` so the operator can
    A/B against FMP without redeploying.
    """
    import asyncio

    import httpx

    from tpcore.data.ingest_alpaca_bars import (
        _RATE_LIMIT_SLEEP_SEC,
        _alpaca_headers,
        fetch_daily_bars_multi,
    )

    # Alpaca's /v2/stocks/bars multi endpoint accepts up to 100 symbols
    # per call. Chunking the universe collapses ~7,669 single-symbol
    # calls (a ~45-min rate-limit floor) into ~77 calls — minutes, not
    # hours. Same endpoint handle_corporate_actions + the all_active
    # sweep already use.
    _MULTI_CHUNK = 100

    headers = _alpaca_headers()
    failures: list[str] = []
    archive_rows: list[dict] = []

    async with httpx.AsyncClient(
        headers=headers,
        base_url="https://data.alpaca.markets",
        timeout=60.0,
    ) as client:
        for i in range(0, len(symbols), _MULTI_CHUNK):
            chunk = symbols[i : i + _MULTI_CHUNK]
            try:
                by_symbol = await fetch_daily_bars_multi(
                    client, chunk, start, end, feed=feed,
                )
            except httpx.HTTPStatusError as exc:
                # Whole chunk failed (e.g. SIP end=today 403 mid-session,
                # or 429 still failing after @with_retry backoff). Record
                # the chunk's symbols and continue — one bad chunk must
                # not abort the rest of the universe.
                failures.append(
                    f"chunk[{chunk[0]}..{chunk[-1]}]({exc.response.status_code})"
                )
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                continue
            for symbol, bars in by_symbol.items():
                if not bars:
                    continue
                for b in bars:
                    archive_rows.append({
                        "ticker": symbol, "date": b.get("t", ""),
                        "open": b.get("o", ""), "high": b.get("h", ""),
                        "low": b.get("l", ""), "close": b.get("c", ""),
                        "volume": b.get("v", ""), "vwap": b.get("vw", ""),
                    })
            await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)

    return archive_rows, failures


async def _handle_daily_bars_all_active(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int:
    """Discovery sweep: enumerate every active US equity, coarse-filter, upsert.

    Uses Alpaca's multi-symbol bars endpoint (``/v2/stocks/bars?symbols=…``)
    so 8k tickers fits in ~160 calls — under 4 minutes wall time at
    50 symbols / 0.3s between batches. Filters at the engine layer
    (close > min_price AND avg volume > min_volume over the lookback
    window) before upserting; symbols that don't pass never touch the
    database. New rows land with ``source = 'alpaca'`` (the upsert SQL
    sets it explicitly so a previously-Tradier-tagged row gets promoted
    back to alpaca-provenance).
    """
    import asyncio
    from datetime import timedelta

    import httpx

    from tpcore.data.ingest_alpaca_bars import (
        _ALPACA_DATA_BASE,
        _alpaca_broker_base,
        _alpaca_headers,
        _upsert_bars,
        fetch_active_us_equities,
        fetch_daily_bars_multi,
    )

    lookback_days = int(config.get("lookback_days", 7))
    min_price = float(config.get("min_price", 5.0))
    min_volume = int(config.get("min_volume", 250_000))
    batch_size = int(config.get("batch_size", 50))
    inter_batch_sleep = float(config.get("inter_batch_sleep_sec", 0.3))

    today = datetime.now(UTC).date()
    start = today - timedelta(days=lookback_days)

    headers = _alpaca_headers()
    rows_upserted = 0
    symbols_passed_coarse = 0
    failed_batches = 0
    async with (
        httpx.AsyncClient(headers=headers, base_url=_alpaca_broker_base(), timeout=60.0) as broker,
        httpx.AsyncClient(headers=headers, base_url=_ALPACA_DATA_BASE, timeout=60.0) as data,
    ):
        assets = await fetch_active_us_equities(broker)
        all_symbols = [a["symbol"] for a in assets]
        logger.info(
            "ingestion.handler.daily_bars.all_active.universe",
            count=len(all_symbols),
            min_price=min_price,
            min_volume=min_volume,
        )

        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i : i + batch_size]
            try:
                # Daily ingestion uses IEX, not the SIP default. Alpaca's
                # paper free tier returns 403 on SIP for live data; the
                # historical backfill uses SIP (where the free tier allows
                # >15-min-old data), but this is the end-of-day daily
                # path which must work without a paid SIP subscription.
                # Tradeoff per CLAUDE.md: IEX silently misses tickers that
                # trade off-IEX (e.g., some ADRs / OTC names); the daily
                # path accepts that miss rather than failing entirely.
                bars_by_symbol = await fetch_daily_bars_multi(
                    data, batch, start, today, feed="iex"
                )
            except httpx.HTTPStatusError as exc:
                failed_batches += 1
                logger.warning(
                    "ingestion.handler.daily_bars.all_active.batch_failed",
                    batch_start=i,
                    status=exc.response.status_code,
                )
                await asyncio.sleep(inter_batch_sleep)
                continue

            for symbol, bars in bars_by_symbol.items():
                if not bars:
                    continue
                last_close = float(bars[-1].get("c") or 0.0)
                avg_volume = sum(int(b.get("v") or 0) for b in bars) / len(bars)
                if last_close <= min_price or avg_volume <= min_volume:
                    continue
                symbols_passed_coarse += 1
                rows_upserted += await _upsert_bars(pool, symbol, bars, delisted=False)

            await asyncio.sleep(inter_batch_sleep)

    logger.info(
        "ingestion.handler.daily_bars.all_active.done",
        symbols_listed=len(all_symbols),
        symbols_passed_coarse=symbols_passed_coarse,
        rows_upserted=rows_upserted,
        failed_batches=failed_batches,
    )
    if failed_batches:
        raise RuntimeError(
            f"daily_bars all_active: {failed_batches} batch fetch failure(s)"
        )
    return rows_upserted


async def handle_sec_filings(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
    """SEC EDGAR Form 4 + 8-K ingest. CSV-first per the standard pipeline.

    Workflow (per docs/superpowers/pipelines/data_adapter_pipeline.md
    ingest sub-protocol):

    1. **download** — adapter pulls submissions index for each T1+T2
       stock; for each Form 4 also fetches and parses the XML body.
       Rows written to two CSVs under ``data/sec_backfill/``:
       ``sec_insider_<run-stamp>.csv`` and ``sec_material_<run-stamp>.csv``.
       Every row passes the physical-truth predicate at the CSV-write
       boundary.
    2. **validate** — at CSV-write: shares > 0, price >= 0, value >= 0,
       transaction_type ∈ {BUY, SELL}; event_type non-empty. Rejected
       rows logged with reason; never enter the loader.
    3. **load** — CSV → DB via ``INSERT ... ON CONFLICT DO NOTHING``.
       Idempotent: second run of the same CSV inserts zero new rows.
    4. **compress** — gzip both CSVs in-place on successful upsert.

    ``config`` keys:
        * ``lookback_days``: how far back to scan submission indexes
          (default 90). Wider windows pull more historical filings.
        * ``max_tickers``: hard cap per run (default 200) to keep the
          stage well under the SEC's 10 req/sec budget. Set to ``None``
          to ingest the entire stock universe.
        * ``skip_guard_days``: skip if last ingest landed within this
          many days (default 6). Set to 0 to force-rerun.

    Returns ``rows_loaded`` (sum across both tables). The structured
    success event includes per-table counts, ticker coverage, and the
    csv artifact paths so the operator can reconcile without opening
    the database.
    """
    from datetime import timedelta
    from pathlib import Path

    # ── 0. Skip guard ────────────────────────────────────────────────
    # Default tightened 6 → 3 days 2026-05-14 (audit cadence finding):
    # Form 4 has a 2-business-day filing deadline so 6-day staleness was
    # half-stale on average. 3 days keeps signal value while still
    # skipping back-to-back runs in the daily pipeline.
    skip_days = int(config.get("skip_guard_days", 3))
    if skip_days > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                """
                SELECT GREATEST(
                    COALESCE((SELECT MAX(recorded_at) FROM platform.insider_transactions), '-infinity'::timestamptz),
                    COALESCE((SELECT MAX(recorded_at) FROM platform.sec_material_events),     '-infinity'::timestamptz)
                )
                """
            )
        if newest is not None and newest != datetime.min.replace(tzinfo=UTC):
            age = datetime.now(UTC) - newest
            if age.days < skip_days:
                logger.info(
                    "ingestion.handler.sec_filings.skipped_fresh",
                    last_refresh_age_days=age.days,
                )
                return 0

    # ── 1. Universe: T1+T2 stocks (not ETFs/funds/SPACs) ─────────────
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
            ORDER BY lt.ticker
            """
        )
    universe = [r["ticker"] for r in rows]
    max_tickers = config.get("max_tickers", 200)
    if max_tickers is not None and len(universe) > int(max_tickers):
        universe = universe[: int(max_tickers)]

    if not universe:
        logger.info(
            "ingestion.handler.sec_filings.empty_universe",
            reason="no T1+T2 stocks in liquidity_tiers",
        )
        return 0

    # ── Bulk insider backfill (Form 345 quarterly datasets) ──────────
    # The per-ticker submissions+XML crawl is the WRONG tool for the
    # historical bootstrap (hundreds of thousands of tiny HTTP fetches
    # at SEC's 8 req/s ≈ ~30h). SEC publishes the entire market's
    # Form 3/4/5 history as ~33 quarterly zips (~336 MB total). For
    # the backfill we download those and parse locally; the per-ticker
    # adapter stays for the cheap daily/weekly incremental.
    if config.get("bulk_form345"):
        lb = int(config.get("lookback_days", 90))
        since_b = datetime.now(UTC).date() - timedelta(days=lb)
        return await _sec_bulk_form345_backfill(pool, set(universe), since_b)

    # ── 1b. Targeted backfill: skip already-covered tickers ──────────
    # The historical bootstrap re-running the whole universe is what
    # hung on the pooler. ``skip_covered`` makes the backfill BOUNDED
    # and TARGETED (same principle as daily_bars repair_gaps): only
    # tickers with zero SEC rows are pulled, so a resumed run converges
    # instead of re-walking the done set. Daily path leaves this off.
    if config.get("skip_covered"):
        async with pool.acquire() as conn:
            covered = await conn.fetch(
                """
                SELECT ticker FROM platform.insider_transactions
                UNION
                SELECT ticker FROM platform.sec_material_events
                """
            )
        covered_set = {r["ticker"] for r in covered}
        before = len(universe)
        universe = [t for t in universe if t not in covered_set]
        logger.info(
            "ingestion.handler.sec_filings.skip_covered",
            already_covered=len(covered_set),
            universe_before=before, universe_after=len(universe),
        )
        if not universe:
            logger.info("ingestion.handler.sec_filings.all_covered")
            return 0

    lookback_days = int(config.get("lookback_days", 90))
    since = datetime.now(UTC).date() - timedelta(days=lookback_days)

    # ── 2. CSV artifact paths ────────────────────────────────────────
    repo_root = Path(__file__).resolve().parent.parent.parent
    csv_dir = repo_root / "data" / "sec_backfill"
    csv_dir.mkdir(parents=True, exist_ok=True)

    # ── 3-5. Process in TICKER CHUNKS ────────────────────────────────
    # Root-cause fix for the pooler "connection was closed" on the
    # multi-hour bootstrap: each chunk does download → load → commit →
    # gzip with a fresh short-lived connection, then frees memory. A
    # crash mid-run keeps every committed chunk (ON CONFLICT DO NOTHING
    # is idempotent), so a re-run with skip_covered resumes cleanly.
    # ``ticker_chunk_size`` ≤ 0 (default) = one chunk → byte-identical
    # to the pre-chunking behaviour for the daily path + tests.
    chunk_size = int(config.get("ticker_chunk_size", 0))
    if chunk_size <= 0 or chunk_size >= len(universe):
        chunks = [universe]
    else:
        chunks = [universe[i:i + chunk_size]
                  for i in range(0, len(universe), chunk_size)]

    # ``eight_k_only`` runs the per-ticker path for 8-K material events
    # only (insider is covered by the bulk Form-345 ETL). 8-K item
    # codes come from the submissions index — one request per issuer,
    # no per-document XML — so a full-history pull is fast.
    # ``full_history`` additionally follows the older submissions
    # shards so a 2018→now backfill doesn't miss filings aged out of
    # ``recent`` for prolific filers.
    sec_forms = (("8-K",) if config.get("eight_k_only")
                 else ("4", "8-K"))
    full_history = bool(config.get("full_history", False))

    rows_loaded = downloaded = rejected = ticker_hits = 0
    loaded_insider = loaded_material = 0
    end_date = datetime.now(UTC).date().isoformat()
    last_csv_insider = last_csv_material = ""
    multi = len(chunks) > 1
    chunks_failed = 0
    for ci, chunk in enumerate(chunks, start=1):
        run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"{run_stamp}_c{ci:04d}" if multi else run_stamp
        insider_csv = csv_dir / f"sec_insider_{suffix}.csv"
        material_csv = csv_dir / f"sec_material_{suffix}.csv"

        try:
            c_ins, c_mat, c_dl, c_rej, c_hits = await _sec_download_to_csv(
                chunk, since, insider_csv, material_csv,
                forms=sec_forms, full_history=full_history,
            )
            c_li, c_lm = await _sec_load_csvs_to_db(pool, c_ins, c_mat)
        except Exception as exc:
            # Multi-chunk bootstrap is resumable: a transient failure
            # (e.g. a pooler "connection was closed" if the daily
            # data-ops run overlaps) fails only THIS chunk. Prior
            # chunks are committed; a re-run with skip_covered picks
            # up the rest. A single-chunk (daily) run re-raises so the
            # stage still surfaces the error.
            if not multi:
                raise
            chunks_failed += 1
            logger.warning(
                "ingestion.handler.sec_filings.chunk_failed",
                chunk=ci, chunks_total=len(chunks),
                chunk_tickers=len(chunk), error=str(exc),
            )
            continue
        _gzip_in_place(insider_csv)
        _gzip_in_place(material_csv)

        downloaded += c_dl
        rejected += c_rej
        ticker_hits += c_hits
        loaded_insider += c_li
        loaded_material += c_lm
        rows_loaded += c_li + c_lm
        last_csv_insider = str(insider_csv) + ".gz"
        last_csv_material = str(material_csv) + ".gz"
        if multi:
            # Streamed progress: a crash here still leaves prior chunks
            # committed, and the operator sees forward motion.
            logger.info(
                "ingestion.handler.sec_filings.chunk_done",
                chunk=ci, chunks_total=len(chunks),
                chunk_tickers=len(chunk),
                cum_rows_loaded=rows_loaded,
                cum_tickers_with_filings=ticker_hits,
                chunks_failed=chunks_failed,
            )

    logger.info(
        "ingestion.handler.sec_filings.done",
        rows_downloaded=downloaded,
        rows_rejected_at_csv_layer=rejected,
        rows_loaded=rows_loaded,
        insider_loaded=loaded_insider,
        material_loaded=loaded_material,
        tickers_attempted=len(universe),
        tickers_with_filings=ticker_hits,
        chunks=len(chunks),
        chunks_failed=chunks_failed,
        date_range_start=since.isoformat(),
        date_range_end=end_date,
        csv_insider=last_csv_insider,
        csv_material=last_csv_material,
    )
    return rows_loaded


# ── SEC helpers ───────────────────────────────────────────────────────


async def _sec_download_to_csv(
    universe: list[str],
    since: Any,  # datetime.date
    insider_csv: Any,  # Path
    material_csv: Any,  # Path
    *,
    forms: tuple[str, ...] = ("4", "8-K"),
    full_history: bool = False,
) -> tuple[list[tuple], list[tuple], int, int, int]:
    """Adapter loop — pulls submission indexes + Form 4 XMLs, writes CSV.

    Returns ``(insider_rows, material_rows, downloaded, rejected, ticker_hits)``.
    Bad rows (physical-truth failures) are counted under ``rejected`` and
    NOT written to either CSV — the loader never sees them.
    """
    import csv as _csv

    from tpcore.sec.edgar_adapter import SECEdgarAdapter

    insider_rows: list[tuple] = []
    material_rows: list[tuple] = []
    downloaded = rejected = ticker_hits = 0

    insider_csv_h = open(insider_csv, "w", newline="", encoding="utf-8")
    material_csv_h = open(material_csv, "w", newline="", encoding="utf-8")
    try:
        ins_writer = _csv.writer(insider_csv_h)
        mat_writer = _csv.writer(material_csv_h)
        ins_writer.writerow([
            "ticker", "filing_date", "insider_name", "transaction_type",
            "shares", "price", "value",
        ])
        mat_writer.writerow(["ticker", "filing_date", "event_type", "summary"])

        async with SECEdgarAdapter() as sec:
            for ticker in universe:
                try:
                    filings = await sec.get_recent_filings(
                        ticker, forms=forms, since=since,
                        full_history=full_history,
                    )
                except Exception as exc:
                    logger.warning(
                        "ingestion.handler.sec_filings.ticker_failed",
                        ticker=ticker, error=str(exc),
                    )
                    continue
                if not filings:
                    continue
                ticker_hits += 1

                for f in filings:
                    downloaded += 1
                    form = f["form"]
                    if form == "4":
                        try:
                            xml_text = await sec.fetch_form4_xml(
                                f["cik"], f["accession_number"],
                                f["primary_document"],
                            )
                        except Exception as exc:
                            logger.debug(
                                "ingestion.handler.sec_filings.form4_fetch_failed",
                                ticker=ticker, accession=f["accession_number"],
                                error=str(exc),
                            )
                            rejected += 1
                            continue
                        tx_rows, skipped = sec.parse_form4_transactions(
                            xml_text, ticker, f["filing_date"],
                        )
                        rejected += skipped
                        for row in tx_rows:
                            # CSV-layer physical-truth gate.
                            if not _insider_row_ok(row):
                                rejected += 1
                                continue
                            ins_writer.writerow([
                                row["ticker"],
                                row["filing_date"].isoformat(),
                                row["insider_name"],
                                row["transaction_type"],
                                row["shares"],
                                f"{row['price']}",
                                f"{row['value']}",
                            ])
                            insider_rows.append((
                                row["ticker"],
                                row["filing_date"],
                                row["insider_name"],
                                row["transaction_type"],
                                row["shares"],
                                row["price"],
                                row["value"],
                            ))
                    elif form == "8-K":
                        items = sec.parse_8k_items(f["items"])
                        for item_code in items:
                            if not _material_row_ok(item_code):
                                rejected += 1
                                continue
                            mat_writer.writerow([
                                ticker,
                                f["filing_date"].isoformat(),
                                item_code,
                                "",
                            ])
                            material_rows.append((
                                ticker,
                                f["filing_date"],
                                item_code,
                                None,
                            ))
    finally:
        insider_csv_h.close()
        material_csv_h.close()
    return insider_rows, material_rows, downloaded, rejected, ticker_hits


def _insider_row_ok(row: dict[str, Any]) -> bool:
    """Physical-truth predicate for insider rows — mirrors the table's
    CHECK constraints so a CSV-layer rejection is exactly equivalent
    to what would be rejected at INSERT time. Defense-in-depth: the
    DB CHECK is the ultimate guard; this stops bad rows earlier.
    """
    if row["transaction_type"] not in ("BUY", "SELL"):
        return False
    if int(row["shares"]) <= 0:
        return False
    from decimal import Decimal as _Decimal
    if _Decimal(row["price"]) < 0:
        return False
    if _Decimal(row["value"]) < 0:
        return False
    return True


def _material_row_ok(event_type: str) -> bool:
    return bool(event_type and event_type.strip())


async def _sec_load_csvs_to_db(
    pool: asyncpg.Pool,
    insider_rows: list[tuple],
    material_rows: list[tuple],
) -> tuple[int, int]:
    """Idempotent upsert. ON CONFLICT DO NOTHING on both unique keys."""
    loaded_insider = loaded_material = 0
    if insider_rows:
        async with pool.acquire() as conn:
            res = await conn.executemany(
                """
                INSERT INTO platform.insider_transactions
                    (ticker, filing_date, insider_name, transaction_type,
                     shares, price, value, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'sec')
                ON CONFLICT (ticker, filing_date, insider_name,
                             transaction_type, shares) DO NOTHING
                """,
                insider_rows,
            )
        # asyncpg.executemany returns None; treat all input rows as
        # "loaded or already present". The accurate "new" count would
        # need RETURNING, which executemany doesn't surface.
        loaded_insider = len(insider_rows)
        del res
    if material_rows:
        # summary column was dropped 2026-05-25 (LLM-triage was the
        # intended writer; removed 2026-05-22). Strip the 4th tuple
        # element here so callers don't have to know about the
        # schema change.
        material_rows_trimmed = [(t, d, ev) for (t, d, ev, _summary) in material_rows]
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO platform.sec_material_events
                    (ticker, filing_date, event_type)
                VALUES ($1, $2, $3)
                ON CONFLICT (ticker, filing_date, event_type) DO NOTHING
                """,
                material_rows_trimmed,
            )
        loaded_material = len(material_rows)
    return loaded_insider, loaded_material


def _gzip_in_place(path: Any) -> None:
    """Compress ``path`` to ``path.gz`` and delete the original.

    Mirrors the pattern in ``scripts/compress_backfill_csvs.py``. No-op
    if the source doesn't exist (e.g., zero filings → empty file kept
    for audit, not gzipped twice).
    """
    import gzip
    import shutil
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        return
    gz_path = p.with_suffix(p.suffix + ".gz")
    with open(p, "rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    p.unlink()


_SEC_BULK_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{q}_form345.zip"
)


async def _sec_bulk_fetch_zip(url: str, ua: str) -> bytes:
    """Download one Form-345 quarterly zip (module-level so tests can
    monkeypatch it). 429/5xx → retry via the canonical decorator;
    404 (a not-yet-published quarter) → ``DataProviderOutage`` so the
    caller can skip it cleanly."""
    import httpx

    from tpcore.outage import DataProviderOutage, with_retry

    @with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _do() -> bytes:
        async with httpx.AsyncClient(timeout=180.0) as c:
            r = await c.get(url, headers={"User-Agent": ua})
        if r.status_code == 200:
            return r.content
        if r.status_code == 429 or 500 <= r.status_code < 600:
            raise httpx.HTTPStatusError(
                f"sec_bulk {url} → {r.status_code}",
                request=r.request, response=r,
            )
        raise DataProviderOutage(f"sec_bulk {url} returned {r.status_code}")

    return await _do()


def _sec_quarters(since: Any) -> list[str]:
    """Quarter labels (``YYYYqN``) from ``since`` (date) to today."""
    from datetime import date as _date

    today = datetime.now(UTC).date()
    y, q = since.year, (since.month - 1) // 3 + 1
    out: list[str] = []
    while (y, q) <= (today.year, (today.month - 1) // 3 + 1):
        out.append(f"{y}q{q}")
        q += 1
        if q > 4:
            q, y = 1, y + 1
        if y > today.year + 1:  # defensive
            break
    del _date
    return out


def _sec_bulk_parse_date(s: str) -> Any:
    """Parse the Form-345 ``DD-MON-YYYY`` date (e.g. ``31-MAR-2026``)."""
    from datetime import datetime as _dt

    s = (s or "").strip()
    if not s:
        return None
    try:
        return _dt.strptime(s.title(), "%d-%b-%Y").date()  # noqa: DTZ007
    except ValueError:
        return None


async def _sec_bulk_form345_backfill(
    pool: asyncpg.Pool, universe: set[str], since: Any,
    dest_dir: Any = None,
) -> int:
    """Two-phase ETL load of the SEC quarterly Form 3/4/5 datasets.

    **Phase 1 — Extract.** Download every quarter zip to
    ``data/sec_backfill/raw/<q>_form345.zip``. A zip already on disk
    (and valid) is NOT re-downloaded — the raw files are a durable,
    re-runnable extract cache, so a crashed/aborted run resumes
    without re-pulling 336 MB.

    **Phase 2 — Transform → validate-at-CSV → Load → compress.** For
    each on-disk zip: parse SUBMISSION/REPORTINGOWNER/NONDERIV_TRANS
    filtered to the T1+T2 ``universe`` and ``since``; every row passes
    the ``_insider_row_ok`` physical-truth gate **as it is written to
    the per-quarter CSV** (the validation boundary, per the platform
    CSV-first sub-protocol); load the validated set idempotently
    (``ON CONFLICT DO NOTHING``); gzip the CSV. Each quarter is its
    own short DB transaction → bounded + pooler-safe + resumable.
    """
    import csv as _csv
    import io
    import os
    import zipfile
    from decimal import Decimal
    from pathlib import Path

    from tpcore.outage import DataProviderOutage

    ua = os.getenv("SEC_EDGAR_USER_AGENT", "short-term-trading-engine ops@local")
    quarters = _sec_quarters(since)
    if dest_dir is not None:
        csv_dir = Path(dest_dir)
    else:
        repo_root = Path(__file__).resolve().parent.parent.parent
        csv_dir = repo_root / "data" / "sec_backfill"
    raw_dir = csv_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # ── PHASE 1: EXTRACT — durable download to disk (resumable) ──────
    extracted: list[tuple[str, Any]] = []
    dl_new = dl_cached = dl_skipped = 0
    for q in quarters:
        zpath = raw_dir / f"{q}_form345.zip"
        if zpath.exists() and zipfile.is_zipfile(zpath):
            extracted.append((q, zpath))
            dl_cached += 1
            continue
        try:
            raw = await _sec_bulk_fetch_zip(_SEC_BULK_URL.format(q=q), ua)
        except DataProviderOutage as exc:
            # Future / not-yet-published quarter (404) — not an error.
            dl_skipped += 1
            logger.info(
                "ingestion.handler.sec_filings.extract_skip",
                quarter=q, reason=str(exc),
            )
            continue
        tmp = zpath.with_suffix(".zip.part")
        tmp.write_bytes(raw)
        if not zipfile.is_zipfile(tmp):
            tmp.unlink(missing_ok=True)
            dl_skipped += 1
            logger.warning(
                "ingestion.handler.sec_filings.extract_bad_zip", quarter=q,
            )
            continue
        tmp.rename(zpath)  # atomic: a partial download never looks done
        extracted.append((q, zpath))
        dl_new += 1
        logger.info(
            "ingestion.handler.sec_filings.extract_done",
            quarter=q, bytes=len(raw),
        )
    logger.info(
        "ingestion.handler.sec_filings.extract_phase_done",
        quarters_available=len(extracted), downloaded=dl_new,
        cached=dl_cached, skipped=dl_skipped,
    )

    # ── PHASE 2: TRANSFORM → validate-at-CSV → LOAD → compress ───────
    total_loaded = 0
    quarters_failed = 0
    for qi, (q, zpath) in enumerate(extracted, start=1):
        try:
            zf = zipfile.ZipFile(zpath)

            def _rows(name: str):
                with zf.open(name) as fh:  # noqa: B023
                    yield from _csv.DictReader(
                        io.TextIOWrapper(fh, encoding="utf-8", errors="replace"),
                        delimiter="\t",
                    )

            # ACCESSION → (ticker, filing_date) for in-universe issuers.
            sub: dict[str, tuple[str, Any]] = {}
            for r in _rows("SUBMISSION.tsv"):
                tk = (r.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
                if not tk or tk not in universe:
                    continue
                if (r.get("DOCUMENT_TYPE") or "").strip() not in (
                    "4", "5", "4/A", "5/A"
                ):
                    continue
                fd = _sec_bulk_parse_date(r.get("FILING_DATE", ""))
                if fd is None or fd < since:
                    continue
                sub[r["ACCESSION_NUMBER"]] = (tk, fd)

            owner: dict[str, str] = {}
            for r in _rows("REPORTINGOWNER.tsv"):
                acc = r.get("ACCESSION_NUMBER")
                if acc in sub and acc not in owner:
                    owner[acc] = (r.get("RPTOWNERNAME") or "").strip() or "UNKNOWN"

            # Transform + validate-at-CSV-write (the CSV is the
            # validation boundary; only rows that pass the physical-
            # truth gate are written AND loaded).
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            cpath = csv_dir / f"sec_insider_bulk_{q}_{stamp}.csv"
            rows: list[tuple] = []
            rejected = 0
            with open(cpath, "w", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh)
                w.writerow(["ticker", "filing_date", "insider_name",
                            "transaction_type", "shares", "price", "value"])
                for r in _rows("NONDERIV_TRANS.tsv"):
                    acc = r.get("ACCESSION_NUMBER")
                    meta = sub.get(acc)
                    if meta is None:
                        continue
                    disp = (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper()
                    ttype = ("BUY" if disp == "A"
                             else "SELL" if disp == "D" else "")
                    try:
                        shares = int(float(r.get("TRANS_SHARES") or 0))
                        price = Decimal(str(r.get("TRANS_PRICEPERSHARE") or "0"))
                    except (ValueError, ArithmeticError):
                        rejected += 1
                        continue
                    tk, fd = meta
                    row = {
                        "ticker": tk, "filing_date": fd,
                        "insider_name": owner.get(acc, "UNKNOWN"),
                        "transaction_type": ttype, "shares": shares,
                        "price": price, "value": price * shares,
                    }
                    if not _insider_row_ok(row):
                        rejected += 1
                        continue
                    w.writerow([tk, fd.isoformat(), row["insider_name"],
                                ttype, shares, f"{price}",
                                f"{row['value']}"])
                    rows.append((tk, fd, row["insider_name"], ttype,
                                 shares, price, row["value"]))

            loaded_i, _ = await _sec_load_csvs_to_db(pool, rows, [])
            _gzip_in_place(cpath)
            total_loaded += loaded_i
            logger.info(
                "ingestion.handler.sec_filings.bulk_quarter_done",
                quarter=q, idx=qi, quarters_total=len(extracted),
                rows_loaded=loaded_i, rows_rejected_at_csv_layer=rejected,
                cum_rows_loaded=total_loaded,
            )
        except Exception as exc:
            quarters_failed += 1
            logger.warning(
                "ingestion.handler.sec_filings.bulk_quarter_failed",
                quarter=q, error=str(exc),
            )
            continue

    logger.info(
        "ingestion.handler.sec_filings.bulk_done",
        quarters_available=len(extracted), quarters_failed=quarters_failed,
        rows_loaded=total_loaded, universe=len(universe),
        since=since.isoformat(),
    )
    return total_loaded


async def _ingest_macro_hist_csv(
    pool: asyncpg.Pool, csv_path: str, indicator: str
) -> int | None:
    """One-time historical CSV → ``platform.macro_indicators`` for a
    single indicator. Idempotent (``ON CONFLICT (indicator, date) DO
    NOTHING``) — re-running inserts nothing and never overwrites or
    deletes other indicators (e.g. ``credit_spread``/BAA10Y) or the
    live truncated rows of this same indicator.

    Parsing reuses the FRED adapter's own date/value parsers so the
    ``"."`` missing-marker and value semantics are byte-identical to
    the live API path. Archived under the distinct ``fred_macro_hist``
    source so the one-off volume does not poison the recurring
    ``fred_macro`` shrinkage comparator.
    """
    import csv as _csv
    from pathlib import Path as _Path

    from tpcore.fred.adapter import _parse_observation_date, _parse_value
    from tpcore.ingestion.csv_archive import write_archive

    rows = list(_csv.reader(_Path(csv_path).open()))
    if not rows or len(rows) < 2:
        raise RuntimeError(f"macro hist csv {csv_path}: empty or header-only")
    body = rows[1:]  # skip header (DATE,<series>)

    upsert_rows: list[tuple] = []
    skipped_missing = 0
    for r in body:
        if len(r) < 2:
            continue
        d = _parse_observation_date(r[0])
        v = _parse_value(r[1])
        if d is None:
            continue
        if v is None:  # "." / blank — FRED missing marker, skip (not zero)
            skipped_missing += 1
            continue
        upsert_rows.append((indicator, d, v))

    if not upsert_rows:
        raise RuntimeError(
            f"macro hist csv {csv_path}: zero parseable rows for {indicator}"
        )

    archive = write_archive(
        "fred_macro_hist",
        [{"indicator": indicator, "date": d.isoformat(), "value": str(v)}
         for (_, d, v) in upsert_rows],
        fieldnames=["indicator", "date", "value"],
        validator=lambda x: bool(x.get("indicator")) and x.get("date") is not None,
    )

    # Task #18 P5: write to macro_data only (legacy table renamed to _legacy;
    # the canonical name is now a view over macro_data).
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal
    async with pool.acquire() as conn:
        await upsert_macro_data_bitemporal(
            conn,
            source="fred",
            rows=[(name, d, v, None) for (name, d, v) in upsert_rows],
        )

    dmin = min(d for _, d, _ in upsert_rows)
    dmax = max(d for _, d, _ in upsert_rows)
    logger.info(
        "ingestion.handler.macro_indicators.hist_csv_done",
        indicator=indicator,
        rows_parsed=len(upsert_rows),
        skipped_missing=skipped_missing,
        date_min=dmin.isoformat(),
        date_max=dmax.isoformat(),
        csv_archive=str(archive.path),
        source_csv=csv_path,
    )
    return len(upsert_rows)


async def handle_macro_indicators(
    pool: asyncpg.Pool, config: dict[str, Any],
) -> int | None:
    """FRED macro-indicators ingest. Weekly stage (Monday 08:00 UTC).

    Pulls every series declared in ``tpcore.fred.INDICATOR_SERIES`` via
    ``tpcore.fred.FREDAdapter``, derives ``sos_state_diffusion`` from
    the 50 ``phci_<state>`` series (Crone/Clayton-Matthews 2005), and
    upserts into ``platform.macro_indicators`` with ``ON CONFLICT
    (indicator, date) DO NOTHING``. Idempotent — second run within the
    7-day skip-guard window returns 0 new rows.

    ``config`` keys:
        * ``start_date``: ISO date, default ``"2018-01-01"`` (full
          backtest-overlap window).
        * ``skip_guard_days``: skip if MAX(recorded_at) is within this
          many days (default 7). Pass 0 to force-rerun.

    Returns ``rows_loaded`` across all indicators. Structured success
    event includes per-indicator counts + date range so the operator
    can reconcile without opening the DB.
    """
    from datetime import date as _date

    from tpcore.fred import INDICATOR_SERIES, FREDAdapter

    # ── One-time historical CSV backfill (canonical knob, not a one-off
    # script). FRED + ALFRED permanently truncated BAMLH0A0HYM2 to a
    # rolling 3yr window (verified 2026-05-16: retroactive across all
    # ALFRED vintages). The pre-truncation history survives only in an
    # external CSV archive. This branch ingests such a CSV for ONE named
    # indicator, idempotently, without disturbing any other series.
    #     ops.py --stage macro_indicators \
    #       --param hist_csv_path=<file> --param hist_indicator=hy_spread --force
    hist_csv = config.get("hist_csv_path")
    hist_ind = config.get("hist_indicator")
    if hist_csv and hist_ind:
        return await _ingest_macro_hist_csv(pool, str(hist_csv), str(hist_ind))

    # ── 0. Skip guard ────────────────────────────────────────────────
    skip_days = int(config.get("skip_guard_days", 7))
    if skip_days > 0:
        async with pool.acquire() as conn:
            # Task #18 P7: skip-guard reads macro_data directly (legacy dropped).
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.macro_data "
                "WHERE source = 'fred'"
            )
        if newest is not None:
            age = datetime.now(UTC) - newest
            if age.days < skip_days:
                logger.info(
                    "ingestion.handler.macro_indicators.skipped_fresh",
                    last_refresh_age_days=age.days,
                )
                return 0

    start_raw = config.get("start_date", "2018-01-01")
    start = _date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw

    # ── 1. Fetch all five series ─────────────────────────────────────
    per_indicator: dict[str, list[dict[str, Any]]] = {}
    async with FREDAdapter() as fred:
        per_indicator = await fred.get_all_indicators(start=start)

    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated(
        "fred_macro",
        [o for lst in per_indicator.values() for o in lst],
    )

    # ── 2. Derived: SOS state diffusion ──────────────────────────────
    # The 50 ``phci_<state>`` raw series above are the substrate for
    # the derived ``sos_state_diffusion`` series — Crone/Clayton-
    # Matthews 2005 sum-of-states diffusion (share of states with
    # PHCI(t) < PHCI(t-3mo)). Pure function; no I/O. Added 2026-05-21
    # for the Sentinel graduated Bear Score Lab candidate. Derived
    # rows ride the same idempotent ON CONFLICT path as raw rows.
    from decimal import Decimal as _Decimal

    from tpcore.fred.diffusion import DEFAULT_SPAN_MONTHS, compute_sos_diffusion

    phci_by_state: dict[str, list[dict[str, Any]]] = {
        name: per_indicator.get(name, [])
        for name, _ in INDICATOR_SERIES
        if name.startswith("phci_")
    }
    sos_rows = compute_sos_diffusion(
        phci_by_state, span_months=DEFAULT_SPAN_MONTHS,
    )
    # Inject the derived series alongside the raw fetched series so
    # the rest of the handler (CSV archive + upsert + summary log)
    # treats it uniformly.
    per_indicator["sos_state_diffusion"] = [
        {"date": r["date"], "value": _Decimal(repr(r["value"]))}
        for r in sos_rows
    ]

    # ── 3. Bulk upsert ───────────────────────────────────────────────
    upsert_rows: list[tuple] = []
    for name, _ in INDICATOR_SERIES:
        for obs in per_indicator.get(name, []):
            upsert_rows.append((name, obs["date"], obs["value"]))
    # Append the derived series rows (NOT in INDICATOR_SERIES — it has
    # no FRED series_id).
    for obs in per_indicator.get("sos_state_diffusion", []):
        upsert_rows.append(("sos_state_diffusion", obs["date"], obs["value"]))

    if not upsert_rows:
        logger.info(
            "ingestion.handler.macro_indicators.empty",
            reason="all series returned zero observations",
        )
        return 0

    # ── 4. CSV-first archive (BAMLH0A0HYM2 truncation defence) ───────
    # Write the full vendor response to a gzipped CSV BEFORE the upsert.
    # If FRED retroactively truncates a series again, this archive is
    # the only place the pre-truncation history survives. Shrinkage
    # detection on the next run compares this archive to its predecessor.
    from tpcore.ingestion.csv_archive import (
        assert_not_shrunk,
        detect_shrinkage,
        log_shrinkage_warning,
        write_archive,
    )

    csv_rows = [
        {"indicator": name, "date": d, "value": str(value)}
        for (name, d, value) in upsert_rows
    ]
    archive = write_archive(
        "fred_macro", csv_rows,
        fieldnames=["indicator", "date", "value"],
        validator=lambda r: bool(r.get("indicator")) and r.get("date") is not None,
    )
    shrinkage = detect_shrinkage(
        "fred_macro", archive.rows_written, exclude_path=archive.path,
    )
    if shrinkage is not None:
        log_shrinkage_warning(shrinkage)
        assert_not_shrunk(shrinkage)  # producer hard-stop (FRED-truncation class)

    # ── 4b. D2 substrate: durable per-source metrics + rolling-median ──
    # Record THIS run's metrics in platform.ingestion_metrics so the
    # next FRED ingest's shrinkage check can compare against the
    # rolling median rather than the single-prior CSV. Run the new
    # detector in PARALLEL with the v1 single-prior detector — when
    # they disagree emit ``SHRINKAGE_DETECTORS_DISAGREE`` for forensic
    # visibility. A v2 PR retires the v1 detector after a soak period.
    from tpcore.ingestion.d2_metrics import (
        check_shrinkage_vs_rolling_median,
        detectors_disagree,
        record_ingestion_metrics,
    )
    v2_verdict = await check_shrinkage_vs_rolling_median(
        pool, "fred_macro", archive.rows_written,
    )
    if shrinkage is not None and detectors_disagree(
        shrinkage.over_threshold, v2_verdict,
    ):
        logger.warning(
            "SHRINKAGE_DETECTORS_DISAGREE",
            source="fred_macro",
            v1_over_threshold=shrinkage.over_threshold,
            v2_shrunk=v2_verdict.shrunk,
            v1_shrinkage_pct=round(shrinkage.shrinkage_pct, 4),
            v2_shrinkage_pct=round(v2_verdict.shrinkage_pct, 4),
            v2_median_rows=v2_verdict.median_rows,
            v2_samples_used=v2_verdict.samples_used,
        )
    await record_ingestion_metrics(
        pool, "fred_macro", archive.rows_written,
    )

    # ── 5. Load CSV → DB (Task #18 P5: writes go ONLY to macro_data) ────────
    # Pre-P5 this also INSERTed into the legacy platform.macro_indicators
    # table; that table has been renamed to macro_indicators_legacy (now a
    # frozen audit snapshot). The original name now resolves to a VIEW over
    # macro_data, which has no INSTEAD OF trigger — INSERT INTO it would fail.
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal
    async with pool.acquire() as conn:
        await upsert_macro_data_bitemporal(
            conn,
            source="fred",
            rows=[(name, d, value, None) for (name, d, value) in upsert_rows],
        )

    summary: dict[str, dict[str, Any]] = {}
    for name, _ in INDICATOR_SERIES:
        obs = per_indicator.get(name, [])
        if obs:
            summary[name] = {
                "rows": len(obs),
                "date_min": obs[0]["date"].isoformat(),
                "date_max": obs[-1]["date"].isoformat(),
            }
        else:
            summary[name] = {"rows": 0, "reason": "no observations returned"}
    # Include the derived series in the per-indicator summary so the
    # operator-visible log mirrors what landed in the DB.
    derived_obs = per_indicator.get("sos_state_diffusion", [])
    if derived_obs:
        summary["sos_state_diffusion"] = {
            "rows": len(derived_obs),
            "date_min": derived_obs[0]["date"].isoformat(),
            "date_max": derived_obs[-1]["date"].isoformat(),
            "derived_from": "phci_<state> × 50 (Crone/Clayton-Matthews 2005)",
        }
    else:
        summary["sos_state_diffusion"] = {
            "rows": 0, "reason": "no aligned state PHCI observations",
        }

    logger.info(
        "ingestion.handler.macro_indicators.done",
        rows_upserted=len(upsert_rows),
        per_indicator=summary,
        csv_archive=str(archive.path),
        shrinkage_over_threshold=shrinkage.over_threshold if shrinkage else False,
    )
    return len(upsert_rows)


async def handle_greeks_max_pain(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """greeks.pro free-tier max-pain ingest (1 symbol, daily snapshot).

    CSV-first → idempotent upsert. Skip-guard: if today's
    ``observed_date`` already has rows for the symbol, no-op (saves a
    call against the 600/day free quota; the upsert is idempotent
    regardless). ``config`` keys: ``symbol`` (default ``"SPY"``),
    ``skip_guard`` (default True; pass False to force a re-pull).
    """
    from datetime import UTC, datetime

    from tpcore.greeks import GreeksProAdapter
    from tpcore.ingestion.csv_archive import write_archive

    symbol = str(config.get("symbol", "SPY")).upper()
    skip_guard = bool(config.get("skip_guard", True))
    today = datetime.now(UTC).date()

    if skip_guard:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT COUNT(*) FROM platform.options_max_pain
                WHERE symbol = $1 AND observed_date = $2
                """,
                symbol, today,
            )
        if existing and existing > 0:
            logger.info(
                "ingestion.handler.greeks_max_pain.skipped_fresh",
                symbol=symbol, observed_date=today.isoformat(),
            )
            return 0

    # NOTE — targeting wedge intentionally absent here. greeks.pro is
    # profiled CONSTRAINED_DEMAND_DRIVEN because the free-tier 600/day
    # call cap is finite, but the handler shape is a SINGLE-symbol
    # snapshot (default "SPY"). There is no universe to prioritise.
    # Demand-driven dynamic symbol selection (pick the most-demanded
    # ticker when ``config.symbol`` is unset) is conceivable but the
    # engines that consume options_max_pain read SPECIFIC symbols —
    # switching the daily target on transient demand would miss the
    # symbol the engine actually needs. Stay explicit.
    async with GreeksProAdapter() as adapter:
        snap = await adapter.get_max_pain(symbol)

    obs_date = snap.observed_at.date()
    rows = [
        (
            snap.symbol, r.expiration_date.date(), obs_date, r.dte,
            snap.spot_price, r.max_pain_strike, r.total_pain_at_max,
            r.spot_distance, r.spot_distance_pct, snap.observed_at,
        )
        for r in snap.results
    ]
    if not rows:
        logger.info(
            "ingestion.handler.greeks_max_pain.empty",
            symbol=symbol, reason="provider returned zero expirations",
        )
        return 0

    write_archive(
        "greeks_max_pain",
        [
            {
                "symbol": s, "expiration_date": ed.isoformat(),
                "observed_date": od.isoformat(), "dte": str(d),
                "spot_price": str(sp), "max_pain_strike": str(mp),
                "total_pain_at_max": str(tp), "spot_distance": str(sd),
                "spot_distance_pct": str(sdp), "observed_at": oa.isoformat(),
            }
            for (s, ed, od, d, sp, mp, tp, sd, sdp, oa) in rows
        ],
        fieldnames=[
            "symbol", "expiration_date", "observed_date", "dte",
            "spot_price", "max_pain_strike", "total_pain_at_max",
            "spot_distance", "spot_distance_pct", "observed_at",
        ],
        validator=lambda x: bool(x.get("symbol")) and bool(x.get("expiration_date")),
    )

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.options_max_pain
                (symbol, expiration_date, observed_date, dte, spot_price,
                 max_pain_strike, total_pain_at_max, spot_distance,
                 spot_distance_pct, observed_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (symbol, expiration_date, observed_date) DO NOTHING
            """,
            rows,
        )

    logger.info(
        "ingestion.handler.greeks_max_pain.done",
        symbol=symbol, observed_date=obs_date.isoformat(),
        expirations=len(rows), spot_price=str(snap.spot_price),
    )
    return len(rows)


async def handle_finnhub_insider_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Finnhub free-tier insider-sentiment (MSPR) for the T1/T2 stock
    universe. Monthly data → 25-day skip-guard. CSV-first → idempotent
    upsert. ``config``: ``symbols`` (list override; default = T1/T2
    stock universe), ``lookback_months`` (default 12),
    ``skip_guard_days`` (default 25; 0 forces re-pull — self-heal).
    """
    import asyncio as _asyncio
    from datetime import timedelta as _td

    from tpcore.finnhub import FinnhubAdapter
    from tpcore.ingestion.csv_archive import write_archive

    skip_days = int(config.get("skip_guard_days", 25))
    if skip_days > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.insider_sentiment"
            )
        if newest is not None and (datetime.now(UTC) - newest).days < skip_days:
            logger.info(
                "ingestion.handler.insider_sentiment.skipped_fresh",
                last_refresh_age_days=(datetime.now(UTC) - newest).days,
            )
            return 0

    symbols_cfg = config.get("symbols")
    if isinstance(symbols_cfg, str) and symbols_cfg.strip():
        # --param symbols=AAPL,MSFT,... arrives as a string; accept it.
        symbols_cfg = [s for s in symbols_cfg.replace(" ", "").split(",") if s]
    if isinstance(symbols_cfg, list) and symbols_cfg:
        universe = [str(s).upper() for s in symbols_cfg]
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT lt.ticker
                FROM platform.liquidity_tiers lt
                LEFT JOIN platform.ticker_classifications tc USING (ticker)
                WHERE lt.tier <= 2
                  AND COALESCE(tc.asset_class, 'stock') = 'stock'
                ORDER BY lt.ticker
                """
            )
        universe = [r["ticker"] for r in rows]
    if not universe:
        logger.info("ingestion.handler.insider_sentiment.empty_universe")
        return 0
    # Demand-driven targeting (#165 facet 3 — IBorrowDesk precedent).
    # Finnhub free-tier is rate-capped at ~60 calls/min (1.1s sleep × N
    # tickers = ~27 min wall-clock for the full T1/T2 universe) — a
    # mid-run interruption (Supabase pooler drop, OOM, kill) truncates
    # the pull. Demand-driven prioritisation puts tickers the engines
    # are actually acting on (open_orders ∪ recent AAR ∪ recent
    # candidates) at the FRONT of the loop, so a truncated run still
    # covers the demand set. Empty demand (paper/early) → universe
    # unchanged. Targeting must never break the pull.
    from tpcore.feeds.targeting import demand_targets, prioritise
    try:
        _demand = await demand_targets(pool, "finnhub_insider_sentiment")
        universe = prioritise(universe, _demand)
    except Exception:
        pass  # targeting is an optimisation, never load-bearing

    today = datetime.now(UTC).date()
    lookback_months = int(config.get("lookback_months", 12))
    from_date = today - _td(days=31 * lookback_months)

    upsert_rows: list[tuple] = []
    archive_rows: list[dict[str, Any]] = []
    failures = 0
    async with FinnhubAdapter() as adapter:
        for i, sym in enumerate(universe):
            try:
                res = await adapter.get_insider_sentiment(sym, from_date, today)
            except Exception as exc:  # noqa: BLE001 — per-ticker isolation
                failures += 1
                logger.debug("insider_sentiment.ticker_failed", ticker=sym, error=str(exc))
                continue
            for rec in res.records:
                upsert_rows.append(
                    (rec.symbol, rec.year, rec.month, rec.mspr, rec.net_change)
                )
                archive_rows.append({
                    "symbol": rec.symbol, "year": str(rec.year),
                    "month": str(rec.month), "mspr": str(rec.mspr),
                    "net_change": str(rec.net_change),
                })
            if i % 50 == 49:
                logger.info("insider_sentiment.progress", done=i + 1, total=len(universe))
            await _asyncio.sleep(1.1)  # ~60/min free-tier courtesy

    if not upsert_rows:
        logger.info(
            "ingestion.handler.insider_sentiment.empty",
            reason="no records returned across universe", failures=failures,
        )
        return 0

    write_archive(
        "finnhub_insider_sentiment", archive_rows,
        fieldnames=["symbol", "year", "month", "mspr", "net_change"],
        validator=lambda r: bool(r.get("symbol")) and bool(r.get("month")),
    )
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.insider_sentiment
                (symbol, year, month, mspr, net_change)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (symbol, year, month) DO NOTHING
            """,
            upsert_rows,
        )
    logger.info(
        "ingestion.handler.insider_sentiment.done",
        rows=len(upsert_rows), tickers=len(universe), failures=failures,
    )
    return len(upsert_rows)


async def handle_apewisdom_social_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """ApeWisdom Reddit social-sentiment for the T1/T2 stock universe.

    API refreshes every ~2h. 24h skip-guard. Pull all pages, filter to
    T1/T2 stocks locally (no per-ticker API filter), CSV-first →
    idempotent upsert. ``config``: ``skip_guard_hours`` (default 24;
    0 forces re-pull — self-heal).
    """
    from datetime import UTC
    from datetime import timedelta as _td

    from tpcore.apewisdom import ApeWisdomAdapter
    from tpcore.ingestion.csv_archive import write_archive

    skip_hours = int(config.get("skip_guard_hours", 24))
    if skip_hours > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.social_sentiment"
            )
        if newest is not None and (datetime.now(UTC) - newest) < _td(hours=skip_hours):
            logger.info(
                "ingestion.handler.social_sentiment.skipped_fresh",
                last_refresh=newest.isoformat(),
            )
            return 0

    async with pool.acquire() as conn:
        urows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
            """
        )
    universe = {r["ticker"].upper() for r in urows}
    if not universe:
        logger.info("ingestion.handler.social_sentiment.empty_universe")
        return 0

    # NOTE — targeting wedge intentionally absent here. ApeWisdom is
    # profiled CONSTRAINED_DEMAND_DRIVEN because the API has a global
    # mentions ceiling (~23% of T1/T2 reachable post-recalibration),
    # but its shape is a single bulk pull (``get_all_sentiment()``)
    # with a server-side ranking we can't influence per-ticker. There
    # is no per-ticker loop to prioritise; demand prioritisation here
    # would re-order the local INSERT only — no wedge against the
    # binding constraint. Augmentation belongs in the DFCR provider
    # roster (a second sentiment source), not in this handler.
    async with ApeWisdomAdapter() as adapter:
        records = await adapter.get_all_sentiment()

    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("apewisdom_social_sentiment", records)

    today = datetime.now(UTC).date()
    # Dedup by ticker (date is constant) — keep the best-ranked entry
    # if a ticker appears twice. Prevents rows_loaded over-counting vs
    # the (ticker,date) PK and makes the return value match DB truth.
    _by_ticker: dict[str, tuple] = {}
    for rec in records:
        if rec.ticker not in universe:
            continue
        prev = _by_ticker.get(rec.ticker)
        if prev is None or rec.rank < prev[4]:
            _by_ticker[rec.ticker] = (
                rec.ticker, today, rec.mentions, rec.upvotes, rec.rank,
                rec.rank_24h_ago, rec.mentions_24h_ago,
            )
    rows = list(_by_ticker.values())
    if not rows:
        logger.info(
            "ingestion.handler.social_sentiment.empty",
            reason="no T1/T2 overlap", total_records=len(records),
        )
        return 0

    write_archive(
        "apewisdom_social_sentiment",
        [
            {
                "ticker": t, "date": d.isoformat(), "mentions": str(m),
                "upvotes": str(u), "rank": str(rk),
                "rank_24h_ago": "" if r24 is None else str(r24),
                "mentions_24h_ago": "" if m24 is None else str(m24),
            }
            for (t, d, m, u, rk, r24, m24) in rows
        ],
        fieldnames=[
            "ticker", "date", "mentions", "upvotes", "rank",
            "rank_24h_ago", "mentions_24h_ago",
        ],
        validator=lambda x: bool(x.get("ticker")) and bool(x.get("date")),
    )
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.social_sentiment
                (ticker, date, mentions, upvotes, rank,
                 rank_24h_ago, mentions_24h_ago)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (ticker, date) DO NOTHING
            """,
            rows,
        )
    logger.info(
        "ingestion.handler.social_sentiment.done",
        rows=len(rows), total_records=len(records),
        universe=len(universe), date=today.isoformat(),
    )
    return len(rows)


async def handle_fear_greed(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Compute the 4-component Fear & Greed index from existing
    platform data and upsert into ``platform.fear_greed``.

    Pure logic lives in ``tpcore.indicators.fear_greed``; this handler
    is the I/O shell. Always computes over the full available history
    (rolling windows need it), then upserts rows from ``start_date``.
    ``config``: ``backfill`` (bool; True → start 2001-01-01),
    ``start_date`` (ISO; default: last 10 days for the daily stage).
    No external provider — VIX/hy_spread/yield_curve from
    ``macro_indicators``, SPY from ``prices_daily``.
    """
    from datetime import UTC
    from datetime import date as _date
    from datetime import timedelta as _td

    import pandas as pd

    from tpcore.indicators.fear_greed import compute_fear_greed

    backfill = bool(config.get("backfill", False))
    if backfill:
        start = _date(2001, 1, 1)
    else:
        sd = config.get("start_date")
        start = (_date.fromisoformat(sd) if isinstance(sd, str)
                 else datetime.now(UTC).date() - _td(days=10))

    async with pool.acquire() as conn:
        # Task #18 P7: reads platform.macro_data directly (legacy
        # macro_indicators dropped). Source='fred'; current rows only.
        # Alias output column names so the downstream compute_fear_greed
        # pure helper continues to receive (indicator, date, value).
        macro = await conn.fetch(
            """
            SELECT series_id AS indicator,
                   observed_date AS date,
                   value_num AS value
            FROM platform.macro_data
            WHERE source = 'fred' AND realtime_end = 'infinity'
              AND series_id IN ('vix','hy_spread','yield_curve')
            ORDER BY observed_date
            """
        )
        spy = await conn.fetch(
            """
            SELECT date, close FROM platform.prices_daily
            WHERE ticker = 'SPY' AND delisted = false
            ORDER BY date
            """
        )

    def _ser(rows, key):
        s = pd.Series(
            {r["date"]: float(r["value"]) for r in rows if r["indicator"] == key}
        )
        s.index = pd.to_datetime(s.index)
        return s.sort_index()

    vix = _ser(macro, "vix")
    hy = _ser(macro, "hy_spread")
    t10 = _ser(macro, "yield_curve")
    sp = pd.Series({r["date"]: float(r["close"]) for r in spy})
    sp.index = pd.to_datetime(sp.index)
    sp = sp.sort_index()

    if vix.empty or hy.empty or sp.empty or t10.empty:
        logger.warning(
            "ingestion.handler.fear_greed.missing_inputs",
            vix=len(vix), hy=len(hy), spy=len(sp), t10y2y=len(t10),
        )
        return 0

    fg = compute_fear_greed(vix, hy, sp, t10).dropna(subset=["score"])
    fg = fg[fg.index.date >= start]
    if fg.empty:
        logger.info("ingestion.handler.fear_greed.empty", start=start.isoformat())
        return 0

    rows = [
        (
            d.date(), float(r["score"]), str(r["label"]),
            (None if pd.isna(r["direction"]) else str(r["direction"])),
            (None if pd.isna(r["score_5d_ago"]) else float(r["score_5d_ago"])),
            float(r["volatility_component"]), float(r["credit_component"]),
            float(r["momentum_component"]), float(r["safe_haven_component"]),
        )
        for d, r in fg.iterrows()
    ]
    # Task #18 P5: writes go ONLY to macro_data; legacy table renamed to
    # fear_greed_legacy (frozen audit snapshot); canonical name is now a view.
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal
    async with pool.acquire() as conn:
        # 8 channels per date: 6 numeric (score, score_5d_ago, 4 components)
        # + 2 text (label, direction). value_xor CHECK requires the channel
        # not in use to be NULL.
        macro_rows: list[tuple[str, _date, float | None, str | None]] = []
        for (d, score, label, direction, score_5d_ago,
             vol_c, credit_c, momentum_c, safe_haven_c) in rows:
            macro_rows.append(("score",                d, score,          None))
            macro_rows.append(("score_5d_ago",         d, score_5d_ago,   None))
            macro_rows.append(("volatility_component", d, vol_c,          None))
            macro_rows.append(("credit_component",     d, credit_c,       None))
            macro_rows.append(("momentum_component",   d, momentum_c,     None))
            macro_rows.append(("safe_haven_component", d, safe_haven_c,   None))
            macro_rows.append(("label",                d, None,           label))
            macro_rows.append(("direction",            d, None,           direction))
        # Drop rows whose chosen channel is NULL (XOR CHECK rejects all-NULL).
        macro_rows = [
            r for r in macro_rows
            if not (r[2] is None and r[3] is None)
        ]
        await upsert_macro_data_bitemporal(
            conn, source="cnn_fear_greed", rows=macro_rows,
        )
    logger.info(
        "ingestion.handler.fear_greed.done",
        rows=len(rows), backfill=backfill, start=start.isoformat(),
        latest_score=rows[-1][1], latest_label=rows[-1][2],
    )
    return len(rows)


# FINRA disseminates consolidated short interest ~8 NYSE sessions after
# the settlement date. release_date = settlement + this many sessions is
# a conservative PIT-safe estimate (never lets a backtest see the data
# before it could have existed; erring late, not early).
_FINRA_DISSEM_SESSIONS = 9


async def handle_finra_short_interest(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """FINRA consolidated short interest → ``platform.short_interest``,
    filtered to the T1/T2 stock universe. Bi-monthly → 12-day
    skip-guard. ``short_interest_pct`` derived from the most-recent
    PIT-safe ``fundamentals_quarterly.shares_outstanding``
    (period_end_date ≤ settlement_date); NULL if unavailable. PIT
    ``release_date`` = settlement + conservative NYSE-session lag.
    ``config``: ``since`` (ISO; default last 90d), ``skip_guard_days``
    (default 12; 0 forces re-pull — self-heal).
    """
    from datetime import UTC
    from datetime import date as _date
    from datetime import timedelta as _td

    from tpcore import calendar as cal
    from tpcore.finra import FinraAdapter

    skip_days = int(config.get("skip_guard_days", 12))
    if skip_days > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.short_interest"
            )
        if newest is not None and (datetime.now(UTC) - newest).days < skip_days:
            logger.info(
                "ingestion.handler.short_interest.skipped_fresh",
                last_refresh_age_days=(datetime.now(UTC) - newest).days,
            )
            return 0

    # FINRA cadence-derived window (measured 2026-05-16: bi-monthly,
    # ~16d period; 10 settlement periods returned for a 180d span).
    # 60d covers the latest ~3 bi-monthly periods incl. the freshest —
    # bounded (~3×21k rows, well under the adapter page cap), idempotent
    # ON CONFLICT. Replaces the blanket 90d. (Becomes the declared
    # FINRA cadence-profile value in the #163 single-source-of-truth.)
    sc = config.get("since")
    since = (_date.fromisoformat(sc) if isinstance(sc, str)
             else datetime.now(UTC).date() - _td(days=60))

    async with pool.acquire() as conn:
        urows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2 AND COALESCE(tc.asset_class, 'stock') = 'stock'
            """
        )
    universe = {r["ticker"].upper() for r in urows}
    if not universe:
        logger.info("ingestion.handler.short_interest.empty_universe")
        return 0

    # NOTE — targeting wedge intentionally absent here. FINRA is
    # profiled CONSTRAINED_DEMAND_DRIVEN because the consolidated
    # short-interest feed has a finite bi-monthly cadence + offset-
    # pagination, but its shape is a single bulk pull
    # (``get_short_interest(since=...)``) returning ALL tickers for
    # the window — there is no per-ticker API call to prioritise.
    # The cap is the API's bi-monthly window, not our per-ticker
    # budget; demand prioritisation would re-order the local filter
    # only, no wedge against the binding constraint.
    async with FinraAdapter() as adapter:
        records = [r for r in await adapter.get_short_interest(since=since)
                   if r.ticker in universe]
    if not records:
        logger.info("ingestion.handler.short_interest.empty", since=since.isoformat())
        return 0

    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("finra_short_interest", records)

    # PIT shares_outstanding: latest filing with period_end_date ≤
    # settlement_date, per ticker. One query for the involved tickers.
    tickers = sorted({r.ticker for r in records})
    async with pool.acquire() as conn:
        fund = await conn.fetch(
            """
            SELECT ticker, period_end_date, shares_outstanding
            FROM platform.fundamentals_quarterly
            WHERE ticker = ANY($1::text[]) AND shares_outstanding IS NOT NULL
            ORDER BY ticker, period_end_date
            """,
            tickers,
        )
    by_ticker: dict[str, list[tuple]] = {}
    for f in fund:
        by_ticker.setdefault(f["ticker"], []).append(
            (f["period_end_date"], f["shares_outstanding"])
        )

    def _pit_shares(tk: str, on: _date):
        cand = [s for (pe, s) in by_ticker.get(tk, []) if pe <= on]
        return cand[-1] if cand else None  # list is period_end ascending

    rows = []
    for rec in records:
        # release_date = settlement + conservative dissemination lag.
        span = cal.sessions_in_range(
            rec.settlement_date, rec.settlement_date + _td(days=30)
        )
        after = [s for s in span if s > rec.settlement_date]
        release = (after[_FINRA_DISSEM_SESSIONS - 1]
                   if len(after) >= _FINRA_DISSEM_SESSIONS
                   else rec.settlement_date + _td(days=14))
        shares = _pit_shares(rec.ticker, rec.settlement_date)
        pct = (
            round(float(rec.short_position_qty) / float(shares) * 100.0, 4)
            if shares and float(shares) > 0 else None
        )
        rows.append((
            rec.ticker, rec.settlement_date, release, pct,
            float(rec.days_to_cover) if rec.days_to_cover is not None else None,
        ))

    from tpcore.ingestion.csv_archive import write_archive
    write_archive(
        "finra_short_interest",
        [{"ticker": t, "settlement_date": sd.isoformat(),
          "release_date": rd.isoformat(),
          "short_interest_pct": "" if p is None else str(p),
          "days_to_cover": "" if d is None else str(d)}
         for (t, sd, rd, p, d) in rows],
        fieldnames=["ticker", "settlement_date", "release_date",
                    "short_interest_pct", "days_to_cover"],
        validator=lambda x: bool(x.get("ticker")) and bool(x.get("settlement_date")),
    )
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.short_interest
                (ticker, settlement_date, release_date,
                 short_interest_pct, days_to_cover)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (ticker, settlement_date) DO UPDATE SET
                release_date=EXCLUDED.release_date,
                short_interest_pct=EXCLUDED.short_interest_pct,
                days_to_cover=EXCLUDED.days_to_cover,
                recorded_at=now()
            """,
            rows,
        )
    logger.info(
        "ingestion.handler.short_interest.done",
        rows=len(rows), universe=len(universe),
        with_pct=sum(1 for r in rows if r[3] is not None),
    )
    return len(rows)


async def handle_iborrowdesk_borrow_rates(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """IBorrowDesk latest borrow rate per T1/T2 stock →
    ``platform.borrow_rates``. Daily → 1-day skip-guard. Scrape-fragile:
    3 CONSECUTIVE failures → CRITICAL log + skip (never crash). Per-
    ticker failures are isolated and counted. ``config``:
    ``skip_guard_hours`` (default 24; 0 forces re-pull — self-heal);
    ``max_tickers`` (default None = full T1/T2 universe; a positive int
    caps the per-run loop for bounded e2e / targeted self-heal).
    """
    from datetime import UTC
    from datetime import timedelta as _td

    from tpcore.iborrowdesk import IBorrowDeskAdapter
    from tpcore.outage import DataProviderOutage

    skip_hours = int(config.get("skip_guard_hours", 24))
    if skip_hours > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.borrow_rates"
            )
        if newest is not None and (datetime.now(UTC) - newest) < _td(hours=skip_hours):
            logger.info(
                "ingestion.handler.borrow_rates.skipped_fresh",
                last_refresh=newest.isoformat(),
            )
            return 0

    async with pool.acquire() as conn:
        urows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2 AND COALESCE(tc.asset_class, 'stock') = 'stock'
            ORDER BY lt.ticker
            """
        )
    universe = [r["ticker"].upper() for r in urows]
    if not universe:
        logger.info("ingestion.handler.borrow_rates.empty_universe")
        return 0
    # Demand-driven targeting (#165 facet 3): IBorrowDesk is
    # scrape-fragile + ticker-limited (CONSTRAINED_DEMAND_DRIVEN) — so
    # spend the bounded budget on tickers the engines are acting on
    # FIRST (open orders / recent AAR / recent candidates), then the
    # rest. Empty demand (paper/early) → universe unchanged. Demand is
    # an optimisation, never load-bearing.
    from tpcore.feeds.targeting import demand_targets, prioritise
    try:
        _demand = await demand_targets(pool, "iborrowdesk_borrow_rates")
        universe = prioritise(universe, _demand)
    except Exception:
        pass  # targeting must never break the pull
    max_tickers = config.get("max_tickers")
    if max_tickers is not None and len(universe) > int(max_tickers):
        universe = universe[: int(max_tickers)]

    rows: list[tuple] = []
    recs: list = []
    consecutive_fail = 0
    failures = 0
    async with IBorrowDeskAdapter() as adapter:
        for tk in universe:
            try:
                rec = await adapter.get_latest_borrow_rate(tk)
                consecutive_fail = 0
            except DataProviderOutage as exc:
                failures += 1
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    logger.critical(
                        "ingestion.handler.borrow_rates.site_unreachable",
                        consecutive_failures=consecutive_fail,
                        last_error=str(exc), processed=len(rows),
                        reason="3 consecutive IBorrowDesk failures — "
                               "skipping rest, NOT crashing pipeline",
                    )
                    break
                continue
            if rec is not None:
                rows.append((rec.ticker, rec.date, float(rec.borrow_rate_pct)))
                recs.append(rec)

    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("iborrowdesk_borrow_rates", recs)

    if not rows:
        logger.warning(
            "ingestion.handler.borrow_rates.empty",
            reason="no reachable borrow data", failures=failures,
        )
        return 0

    from tpcore.ingestion.csv_archive import write_archive
    write_archive(
        "iborrowdesk_borrow_rates",
        [{"ticker": t, "date": d.isoformat(), "borrow_rate_pct": str(r)}
         for (t, d, r) in rows],
        fieldnames=["ticker", "date", "borrow_rate_pct"],
        validator=lambda x: bool(x.get("ticker")) and bool(x.get("date")),
    )
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.borrow_rates (ticker, date, borrow_rate_pct)
            VALUES ($1,$2,$3)
            ON CONFLICT (ticker, date) DO NOTHING
            """,
            rows,
        )
    logger.info(
        "ingestion.handler.borrow_rates.done",
        rows=len(rows), universe=len(universe), failures=failures,
    )
    return len(rows)


async def handle_aaii_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """AAII weekly Sentiment Survey → ``platform.aaii_sentiment``.
    Published Thursdays; weekly → 5-day skip-guard. The source is a
    single full-history workbook, so the upsert is idempotent and
    self-correcting (``ON CONFLICT (date) DO UPDATE``) — every run
    refreshes the whole series cheaply. ``config``: ``skip_guard_days``
    (default 5; 0 forces re-pull — self-heal).
    """
    from datetime import UTC

    from tpcore.aaii import AAIIAdapter

    skip_days = int(config.get("skip_guard_days", 5))
    if skip_days > 0:
        async with pool.acquire() as conn:
            # Task #18 P7: skip-guard reads macro_data directly (legacy dropped).
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.macro_data "
                "WHERE source = 'aaii'"
            )
        if newest is not None and (datetime.now(UTC) - newest).days < skip_days:
            logger.info(
                "ingestion.handler.aaii_sentiment.skipped_fresh",
                last_refresh_age_days=(datetime.now(UTC) - newest).days,
            )
            return 0

    async with AAIIAdapter() as adapter:
        records = await adapter.get_sentiment_history()
    if not records:
        logger.info("ingestion.handler.aaii_sentiment.empty")
        return 0

    rows = [
        (r.date, float(r.bullish_pct), float(r.bearish_pct), float(r.neutral_pct))
        for r in records
    ]

    from tpcore.ingestion.csv_archive import write_archive
    write_archive(
        "aaii_sentiment",
        [{"date": d.isoformat(), "bullish_pct": str(b),
          "bearish_pct": str(be), "neutral_pct": str(n)}
         for (d, b, be, n) in rows],
        fieldnames=["date", "bullish_pct", "bearish_pct", "neutral_pct"],
        validator=lambda x: bool(x.get("date")) and bool(x.get("bullish_pct")),
    )
    # Task #18 P5: writes go ONLY to macro_data; legacy table renamed to
    # aaii_sentiment_legacy (frozen audit snapshot); canonical name is now a view.
    from tpcore.ingestion.macro_data_emit import upsert_macro_data_bitemporal
    async with pool.acquire() as conn:
        # 3 channels per date: bullish_pct / bearish_pct / neutral_pct.
        macro_rows = [
            (channel, d, val, None)
            for (d, b, be, n) in rows
            for (channel, val) in (
                ("bullish_pct", b),
                ("bearish_pct", be),
                ("neutral_pct", n),
            )
            if val is not None
        ]
        await upsert_macro_data_bitemporal(
            conn, source="aaii", rows=macro_rows,
        )
    logger.info(
        "ingestion.handler.aaii_sentiment.done",
        rows=len(rows), date_range=f"{rows[0][0]}..{rows[-1][0]}",
    )
    return len(rows)


HANDLERS: dict[str, HandlerFn] = {
    "data_validation": handle_data_validation,
    "fundamentals_refresh": handle_fundamentals_refresh,
    "sec_fundamentals_fallback": handle_sec_fundamentals_fallback,
    "corporate_actions": handle_corporate_actions,
    "daily_bars": handle_daily_bars,
    "sec_filings": handle_sec_filings,
    "macro_indicators": handle_macro_indicators,
    "greeks_max_pain": handle_greeks_max_pain,
    "finnhub_insider_sentiment": handle_finnhub_insider_sentiment,
    "apewisdom_social_sentiment": handle_apewisdom_social_sentiment,
    "fear_greed": handle_fear_greed,
    "finra_short_interest": handle_finra_short_interest,
    "iborrowdesk_borrow_rates": handle_iborrowdesk_borrow_rates,
    "aaii_sentiment": handle_aaii_sentiment,
}


__all__ = [
    "HANDLERS",
    "HandlerFn",
    "handle_data_validation",
    "handle_fundamentals_refresh",
    "handle_sec_fundamentals_fallback",
    "handle_corporate_actions",
    "handle_daily_bars",
    "handle_sec_filings",
    "handle_macro_indicators",
    "handle_greeks_max_pain",
    "handle_finnhub_insider_sentiment",
    "handle_apewisdom_social_sentiment",
    "handle_fear_greed",
    "handle_finra_short_interest",
    "handle_iborrowdesk_borrow_rates",
    "handle_aaii_sentiment",
]
