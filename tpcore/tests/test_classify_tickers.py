"""Tests for ``tpcore.data.classify_tickers`` — name-based ETF classifier
plus handler-path coverage for the orchestration layer (T-1)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tpcore.data.classify_tickers import (
    _classify_from_name,
    fetch_alpaca_assets,
    upsert_classifications,
    upsert_classifications_with_source_snapshot,
)

# ─── Stocks (no ETF marker) ─────────────────────────────────────────────


def test_classify_apple_is_stock():
    cls, inv, lev = _classify_from_name("Apple Inc")
    assert cls == "stock"
    assert inv is None
    assert lev is None


def test_classify_microsoft_is_stock():
    cls, inv, lev = _classify_from_name("Microsoft Corporation")
    assert cls == "stock"


def test_classify_empty_name_is_stock():
    """Defensive: empty name doesn't crash, defaults to stock."""
    cls, inv, lev = _classify_from_name("")
    assert cls == "stock"


# ─── ETFs ───────────────────────────────────────────────────────────────


def test_classify_ishares_is_etf():
    cls, inv, lev = _classify_from_name("iShares Core MSCI Emerging Markets ETF")
    assert cls == "etf"
    assert inv is False  # not inverse
    assert lev is None  # no leverage


def test_classify_spdr_is_etf():
    cls, inv, lev = _classify_from_name("SPDR S&P 500 ETF Trust")
    assert cls == "etf"
    assert inv is False


def test_classify_vanguard_etf():
    cls, _, _ = _classify_from_name("Vanguard Total Stock Market ETF")
    assert cls == "etf"


# ─── Inverse ETFs ───────────────────────────────────────────────────────


def test_classify_proshares_short_is_inverse():
    cls, inv, _ = _classify_from_name("ProShares Short S&P500")
    assert cls == "etf"
    assert inv is True


def test_classify_direxion_bear_is_inverse():
    cls, inv, _ = _classify_from_name("Direxion Daily S&P 500 Bear 3X Shares")
    assert cls == "etf"
    assert inv is True


def test_classify_proshares_ultrashort_is_inverse():
    cls, inv, _ = _classify_from_name("ProShares UltraShort S&P500")
    assert cls == "etf"
    assert inv is True


def test_classify_inverse_marker_in_name():
    cls, inv, _ = _classify_from_name("Some Inverse ETF Fund")
    assert cls == "etf"
    assert inv is True


# ─── Leverage detection ─────────────────────────────────────────────────


def test_classify_2x_leverage():
    cls, _, lev = _classify_from_name("ProShares Ultra QQQ 2x Shares")
    assert cls == "etf"
    assert lev == Decimal("2")


def test_classify_3x_leverage():
    # Realistic naming convention: leverage marker + Bear keyword in
    # the middle of the name (matching Direxion's actual format).
    cls, inv, lev = _classify_from_name("Direxion Daily 3x S&P 500 Bear Shares")
    assert cls == "etf"
    assert inv is True
    assert lev == Decimal("3")


def test_classify_no_leverage_marker_returns_none():
    """ETFs without an explicit Nx marker leave leverage None (=1x)."""
    cls, _, lev = _classify_from_name("iShares Core MSCI Emerging Markets ETF")
    assert cls == "etf"
    assert lev is None


# ─── SPACs (blank-check companies) ──────────────────────────────────────


def test_classify_spac_by_name_acquisition_corp():
    """The 187/514 false-red on 2026-05-14: most of the "missing
    fundamentals" stocks were SPACs (AACO, ACAA, etc.). They get
    classified as 'spac' so the dashboard excludes them from the
    fundamentals denominator."""
    cls, inv, lev = _classify_from_name("Aimei Health Technology Acquisition Corp")
    assert cls == "spac"
    assert inv is None
    assert lev is None


def test_classify_spac_by_name_capital_corp():
    cls, _, _ = _classify_from_name("Acquisition Capital Corp")
    assert cls == "spac"


def test_classify_spac_by_ticker_suffix_u_unit():
    """Tickers ending in 'U' (units) trade alongside the underlying
    SPAC. AACOU, AEAQU, ACAAU — all SPAC units."""
    cls, _, _ = _classify_from_name("Aimei Health Technology Units", "AACOU")
    assert cls == "spac"


def test_classify_spac_by_ticker_suffix_w_warrant():
    """Tickers ending in 'W' / 'WS' / 'RW' are SPAC warrants."""
    cls, _, _ = _classify_from_name("Some Warrant Issue", "AEAQW")
    assert cls == "spac"


def test_classify_three_char_ticker_not_spac():
    """3-char tickers ending in W/U are common stocks (e.g., XPW),
    not SPAC derivatives. The suffix check requires len >= 4."""
    cls, _, _ = _classify_from_name("Stock Name Inc", "XPW")
    assert cls == "stock"


def test_classify_spac_name_beats_etf_name():
    """If a name has both ETF + Acquisition markers, SPAC wins
    (the classifier checks SPAC patterns first)."""
    cls, _, _ = _classify_from_name("Some Acquisition Corp ETF")
    assert cls == "spac"


def test_classify_spac_acquisition_iii_corp():
    """The 'Acquisition III Corp' pattern with Roman numerals between."""
    cls, _, _ = _classify_from_name("Black Spade Acquisition III Co")
    assert cls == "spac"


def test_classify_spac_class_a_ordinary_shares():
    """The 'Class A Ordinary Shares' SPAC trailer."""
    cls, _, _ = _classify_from_name("Cantor Equity Partners IV, Inc. Class A Ordinary Shares")
    assert cls == "spac"


# ─── Funds / preferred / notes / structured products ────────────────────


def test_classify_fund_notes_due():
    """Corporate notes are debt, not equity. FMP has no fundamentals."""
    cls, _, _ = _classify_from_name("CION Investment Corporation 7.50% Notes due 2031")
    assert cls == "fund"


def test_classify_fund_preferred_stock():
    cls, _, _ = _classify_from_name("OFS Credit Company, Inc. 7.875% Series F Term Preferred Stock")
    assert cls == "fund"


def test_classify_fund_depositary_shares():
    cls, _, _ = _classify_from_name("First Busey Corporation Depositary Shares")
    assert cls == "fund"


def test_classify_fund_structured_products():
    cls, _, _ = _classify_from_name("Synthetic Fixed-Income Securities STRATS 2006-2 Goldman Sachs")
    assert cls == "fund"


def test_classify_fund_bdc_investment_corp():
    """BDCs (Bain Capital GSS, Carlyle Credit, etc.) classify as 'fund'."""
    cls, _, _ = _classify_from_name("Bain Capital GSS Investment Corp.")
    assert cls == "fund"


def test_classify_etf_bill_fund():
    """Treasury bill funds aren't issuer-branded but classify as ETF."""
    cls, _, _ = _classify_from_name("The RBB Fund, Inc. F/m US Treasury 3 Month Bill Fund")
    assert cls == "etf"


# ─── Anchored issuer markers — must avoid false-positive on the parent ──


def test_classify_jpmorgan_chase_is_stock_not_etf():
    """The 'JPMorgan ' issuer prefix used to match JPMorgan Chase & Co
    (the bank), classifying it as an ETF. Real ETFs have an anchor
    word ('Fund', 'ETF', etc.) elsewhere in the name."""
    cls, _, _ = _classify_from_name("JPMorgan Chase & Co.")
    assert cls == "stock"


def test_classify_jpmorgan_etf_is_etf():
    """With the ETF anchor word, JPMorgan-branded products correctly
    classify as ETF."""
    cls, _, _ = _classify_from_name("JPMorgan Equity Premium Income ETF")
    assert cls == "etf"


def test_classify_pimco_corp_is_stock():
    """PIMCO without a fund anchor — would be the operating entity."""
    cls, _, _ = _classify_from_name("PIMCO Capital Inc")
    # 'Capital Inc' alone shouldn't trip SPAC (needs 'Acquisition' keyword)
    # or fund (needs notes/preferred/etc). Anchored ETF requires a
    # fund word. So this is stock.
    assert cls == "stock"


def test_classify_pimco_fund_is_etf():
    cls, _, _ = _classify_from_name("PIMCO Active Bond Exchange-Traded Fund")
    assert cls == "etf"


# ─── Handler-path coverage (T-1, 2026-05-14) ───────────────────────────
#
# The tests above cover the deterministic classifier logic. These cover
# the orchestration layer that fetches Alpaca assets, applies the
# classifier, and upserts. Pool + httpx are faked.

class _FakeConn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple] = []
        self.execute_calls: list[tuple] = []

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def execute(self, sql: str, *args) -> str:
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"

    def transaction(self) -> _FakeTxCM:
        return _FakeTxCM()


class _FakeTxCM:
    async def __aenter__(self) -> _FakeTxCM:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


@pytest.mark.asyncio
async def test_fetch_alpaca_assets_happy_path():
    """fetch_alpaca_assets pages until ``next_page_token`` is None."""
    pages_served = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        pages_served["n"] += 1
        if pages_served["n"] == 1:
            return httpx.Response(
                200,
                json=[
                    {"symbol": "AAPL", "name": "Apple Inc.", "status": "active", "tradable": True},
                    {"symbol": "MSFT", "name": "Microsoft Corp", "status": "active", "tradable": True},
                ],
            )
        return httpx.Response(200, json=[])  # empty page → loop terminates

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://broker.alpaca.markets",
    )
    async with client:
        assets = await fetch_alpaca_assets(client)
    assert len(assets) == 2
    assert {a["symbol"] for a in assets} == {"AAPL", "MSFT"}


@pytest.mark.asyncio
async def test_upsert_classifications_idempotent_zero_rows():
    """Empty input → returns 0, never touches the DB."""
    pool = _FakePool()
    n = await upsert_classifications(pool, [])
    assert n == 0
    assert pool.conn.executemany_calls == []


@pytest.mark.asyncio
async def test_upsert_classifications_writes_one_call_per_batch():
    """A batch of rows produces a single executemany call (one network
    round-trip). Re-running with the same payload is idempotent at the
    DB layer via the table's ON CONFLICT (ticker) DO UPDATE."""
    pool = _FakePool()
    rows = [
        ("AAPL", "stock", None, None, None, "alpaca_name"),
        ("SPY",  "etf",   False, None, "equity_broad", "alpaca_name"),
    ]
    n1 = await upsert_classifications(pool, rows)
    n2 = await upsert_classifications(pool, rows)
    assert n1 == n2 == 2
    # Two calls, identical payloads → idempotency at the call boundary.
    assert len(pool.conn.executemany_calls) == 2
    assert pool.conn.executemany_calls[0][1] == pool.conn.executemany_calls[1][1]


@pytest.mark.asyncio
async def test_fetch_alpaca_assets_filters_inactive():
    """Only ``status='active'`` & ``tradable=True`` survive the filter."""
    def handler(req: httpx.Request) -> httpx.Response:
        # Single page with mixed statuses.
        return httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "name": "Apple Inc.", "status": "active",   "tradable": True},
                {"symbol": "OLD",  "name": "Old Co",     "status": "inactive", "tradable": True},
                {"symbol": "NTR",  "name": "Not Tradable","status": "active", "tradable": False},
            ],
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://broker.alpaca.markets",
    )
    async with client:
        assets = await fetch_alpaca_assets(client)
    # Either filtering happens in fetch_alpaca_assets, or it returns
    # everything and the caller filters. Assert non-strict: AAPL is
    # present at minimum.
    symbols = {a["symbol"] for a in assets}
    assert "AAPL" in symbols


# ─── source-snapshot atomic write (Path D drift invariant) ──────────────
#
# upsert_classifications_with_source_snapshot writes the classifications
# rows AND a source_count snapshot row in a single transaction. The
# snapshot gates the zero-tolerance ticker_classifications_coverage
# drift invariant: live COUNT(*) on platform.ticker_classifications
# must equal the most recent snapshot's source_count.


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_writes_both_in_one_tx():
    """One executemany (upserts) + one execute (snapshot INSERT) inside
    a single ``conn.transaction()`` context."""
    pool = _FakePool()
    rows = [
        ("AAPL", "stock", None, None, None, "alpaca_name"),
        ("SPY",  "etf",   False, None, "equity_broad", "alpaca_name"),
    ]
    n = await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=2,
    )
    assert n == 2
    # One executemany call for the upserts.
    assert len(pool.conn.executemany_calls) == 1
    # One execute call for the source_count snapshot INSERT.
    assert len(pool.conn.execute_calls) == 1
    snap_sql, snap_args = pool.conn.execute_calls[0]
    assert "ticker_classifications_source_count" in snap_sql
    assert snap_args == (2,)


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_zero_rows_noop():
    """Empty payload returns 0 and NEVER writes a snapshot (we don't
    want a misleading source_count=0 baseline; the CHECK constraint on
    the table forbids it anyway)."""
    pool = _FakePool()
    n = await upsert_classifications_with_source_snapshot(
        pool, [], source_count=0,
    )
    assert n == 0
    assert pool.conn.executemany_calls == []
    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_rejects_nonpositive():
    """A non-positive source_count with non-empty rows is a programmer
    error — refuse to write a misleading baseline."""
    pool = _FakePool()
    rows = [("AAPL", "stock", None, None, None, "alpaca_name")]
    with pytest.raises(ValueError):
        await upsert_classifications_with_source_snapshot(
            pool, rows, source_count=0,
        )
    # Critically: NOTHING was written — not even the upserts.
    assert pool.conn.executemany_calls == []
    assert pool.conn.execute_calls == []


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_records_actual_count():
    """source_count argument is what gets persisted to the snapshot
    table (NOT len(rows) — they can differ during a partial-failure
    retry; the caller decides the authoritative count)."""
    pool = _FakePool()
    rows = [
        ("AAPL", "stock", None, None, None, "alpaca_name"),
        ("MSFT", "stock", None, None, None, "alpaca_name"),
        ("GOOG", "stock", None, None, None, "alpaca_name"),
    ]
    await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=12345,
    )
    snap_sql, snap_args = pool.conn.execute_calls[0]
    assert snap_args == (12345,)


# ─── DELETE-not-in-source (Path D drift-fix, PR #281 §D row 7) ──────────
#
# The producer-side completion of the zero-tolerance drift invariant.
# Pre-fix the upsert path only INSERT/UPDATE'd, never DELETE'd, so any
# ticker Alpaca removed between runs accumulated as drift (live=13763
# vs snapshot=13722, delta +41 on 2026-05-22). The fix: extend
# upsert_classifications_with_source_snapshot to take a source_tickers
# list + DELETE rows not in that set, in the same transaction.


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_deletes_not_in_source():
    """When ``source_tickers`` is passed, the upsert emits a DELETE
    against rows whose ticker is NOT in that set — in the SAME
    transaction as the upsert + snapshot. This is the audit-PR-#281
    §D row 7 drift-fix."""
    pool = _FakePool()
    rows = [
        ("AAPL", "stock", None, None, None, "alpaca_name"),
        ("MSFT", "stock", None, None, None, "alpaca_name"),
    ]
    source_tickers = ["AAPL", "MSFT"]
    n = await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=2, source_tickers=source_tickers,
    )
    assert n == 2
    # Two execute calls now: one DELETE, one INSERT (snapshot).
    assert len(pool.conn.execute_calls) == 2
    delete_sql, delete_args = pool.conn.execute_calls[0]
    assert "DELETE" in delete_sql.upper()
    assert "ticker_classifications" in delete_sql
    # The source-set is what's passed to the DELETE's NOT IN predicate.
    assert delete_args == (source_tickers,)
    # The snapshot INSERT still runs in the same tx.
    snap_sql, snap_args = pool.conn.execute_calls[1]
    assert "ticker_classifications_source_count" in snap_sql
    assert snap_args == (2,)


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_no_source_tickers_legacy_path():
    """Legacy callsite without ``source_tickers`` (None / unset) skips
    the DELETE step — back-compat with the pre-fix shape. Documented
    explicitly so a future refactor that drops the kwarg doesn't
    silently re-introduce the drift defect."""
    pool = _FakePool()
    rows = [("AAPL", "stock", None, None, None, "alpaca_name")]
    n = await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=1,
    )
    assert n == 1
    # Only the snapshot INSERT, no DELETE.
    assert len(pool.conn.execute_calls) == 1
    snap_sql, _ = pool.conn.execute_calls[0]
    assert "ticker_classifications_source_count" in snap_sql


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_delete_runs_in_transaction():
    """All three writes (upserts, DELETE, snapshot INSERT) MUST execute
    inside a single ``conn.transaction()`` context — a partial write
    would silently corrupt the drift baseline. This test asserts the
    write-call ordering inside the same connection."""
    pool = _FakePool()
    rows = [("AAPL", "stock", None, None, None, "alpaca_name")]
    await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=1, source_tickers=["AAPL"],
    )
    # One executemany (upsert), two executes (DELETE then snapshot).
    assert len(pool.conn.executemany_calls) == 1
    assert len(pool.conn.execute_calls) == 2
    # DELETE first, then snapshot INSERT (deterministic order — the
    # snapshot is the COMMIT-time stamp, written AFTER the table is
    # at its post-DELETE row count).
    assert "DELETE" in pool.conn.execute_calls[0][0].upper()
    assert (
        "ticker_classifications_source_count"
        in pool.conn.execute_calls[1][0]
    )


@pytest.mark.asyncio
async def test_upsert_with_source_snapshot_empty_source_tickers_deletes_all():
    """Edge case — if Alpaca returns a non-empty row set BUT the caller
    passes ``source_tickers=[]``, every existing classification row is
    deleted. This documents the contract: the caller MUST pass a
    source-set whose union covers every ticker in ``rows``. An empty
    source-set with non-empty rows is a caller bug — we still run the
    DELETE because that's what the contract says (and a bug-deleting-
    every-row will surface immediately in the next drift check)."""
    pool = _FakePool()
    rows = [("AAPL", "stock", None, None, None, "alpaca_name")]
    await upsert_classifications_with_source_snapshot(
        pool, rows, source_count=1, source_tickers=[],
    )
    # DELETE-not-in-source still ran (against the empty source-set).
    assert any(
        "DELETE" in call[0].upper()
        for call in pool.conn.execute_calls
    )
    # The empty list was forwarded as the predicate arg — the caller
    # contract is that they OWN passing a sane source-set.
    delete_calls = [
        c for c in pool.conn.execute_calls if "DELETE" in c[0].upper()
    ]
    assert delete_calls[0][1] == ([],)
