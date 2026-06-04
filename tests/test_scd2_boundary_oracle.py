"""Boundary-oracle sentinel: at a delisted-then-reused ticker seam, the
IdentityDispatcher (ticker -> classification_id) and the corp_history resolver
(ticker -> issuer_id) must both equal the daterange('[)') oracle, NOT each
other (mutual-wrongness would pass a trigger-vs-dispatcher check).

Skips when no live/test DB is configured. Uses a transaction-isolated synthetic
reuse pair inserted into ticker_history / issuer_securities; rolls back so no
state persists.

Real-signature adaptations (vs the plan draft):
  - ``IdentityDispatcher(pool)`` takes an asyncpg *pool*, not a connection. The
    fixture wraps the open transaction's connection in a minimal pool-shim whose
    ``acquire()`` yields that same connection, so the dispatcher reads inside the
    rolled-back transaction. (Public ``acquire()`` contract only — no private
    attribute access.)
  - ``corp_history.resolve_issuer_at_date(conn, ticker, as_of)`` returns an
    *issuer_id*, not a classification_id. The reuse pair therefore also gets two
    ``issuer_securities`` rows (one per classification) so the resolver's first
    hop (ticker_history) is what the seam exercises; the assertion maps the
    oracle's classification verdict to the corresponding issuer_id.
"""
from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager

import pytest

from tpcore.identity.tkr14 import mint

pytestmark = pytest.mark.asyncio

B = dt.date(2022, 6, 1)              # the seam: predecessor.valid_to == successor.valid_from == B
_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
# Real TKR-14 ids — the live `ticker_classifications.id` CHECK enforces the TKR14
# regex, so synthetic strings are rejected. Distinct legal names -> distinct
# issuer hashes -> distinct ids. asset_class "S" here is the TKR14 single-letter
# code (the table column below uses the long form "stock").
PRED_CLS = mint(country="US", asset_class="S", ipo_venue="N", discovery_source="S",
                cik=None, legal_name="Test Predecessor Co", now=_NOW)
SUCC_CLS = mint(country="US", asset_class="S", ipo_venue="N", discovery_source="S",
                cik=None, legal_name="Test Successor Co", now=_NOW)
assert PRED_CLS != SUCC_CLS, "minted classification ids must differ"
PRED_ISSUER = "TESTPREDISS001"       # issuer_id is free text (no format CHECK)
SUCC_ISSUER = "TESTSUCCISS001"
TKR = "ZZTESTREUSE"
D0 = dt.date(2020, 1, 1)


def _oracle(as_of: dt.date) -> str | None:
    """daterange('[)') truth: predecessor covers [D0, B); successor covers [B, inf)."""
    if as_of < D0:
        return None
    if as_of < B:
        return PRED_CLS          # [2020-01-01, B)
    return SUCC_CLS               # [B, inf)


def _oracle_issuer(as_of: dt.date) -> str | None:
    cls = _oracle(as_of)
    if cls == PRED_CLS:
        return PRED_ISSUER
    if cls == SUCC_CLS:
        return SUCC_ISSUER
    return None


class _ConnPoolShim:
    """Minimal asyncpg.Pool stand-in that hands the dispatcher the SAME open
    (transaction-bound) connection on every ``acquire()``. Public surface only.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest.fixture
async def pool():
    url = os.environ.get("DATABASE_URL_IPV4")
    if not url:
        pytest.skip("no DATABASE_URL_IPV4 — boundary-oracle sentinel needs a DB")
    from tpcore.db import build_asyncpg_pool

    p = await build_asyncpg_pool(url, max_size=2, timeout=20.0)
    yield p
    await p.close()


@pytest.fixture
async def reuse_pair(pool):
    """Insert a synthetic reuse pair inside a transaction; roll back after.

    Yields the open connection so both resolvers read the uncommitted rows.
    """
    async with pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            # Minimal classifications + the SCD-2 reuse pair in ticker_history.
            # Two classifications for the reused ticker, shaped to satisfy the live
            # partial uniques (tc_ticker_active_uniq: one lifetime_end-NULL row per
            # ticker; current_ticker_active_uniq: one active current_ticker holder):
            # the predecessor is DELISTED (lifetime_end=B, current_ticker NULL); the
            # successor is the active current holder. asset_class must satisfy the
            # CHECK (stock/adr/etf/...). No ON CONFLICT — a constraint surprise should
            # fail loud, not silently drop a row.
            await c.execute(
                "INSERT INTO platform.ticker_classifications "
                "(id, ticker, current_ticker, lifetime_start, lifetime_end, asset_class, source) VALUES "
                "($1, $2, NULL, $3, $4,   'stock', 'test_boundary_oracle'),"
                "($5, $2, $2,   $4, NULL, 'stock', 'test_boundary_oracle')",
                PRED_CLS, TKR, D0, B, SUCC_CLS,
            )
            await c.execute(
                "INSERT INTO platform.ticker_history (ticker, classification_id, valid_from, valid_to) "
                "VALUES ($1,$2,$3,$4),($1,$5,$4,NULL)",
                TKR, PRED_CLS, D0, B, SUCC_CLS,
            )
            # One issuer per classification, SCD-2-aligned, for the resolver hop.
            await c.execute(
                "INSERT INTO platform.issuers (issuer_id, legal_name) "
                "VALUES ($1,'Test Issuer Predecessor'),($2,'Test Issuer Successor') "
                "ON CONFLICT DO NOTHING",
                PRED_ISSUER, SUCC_ISSUER,
            )
            await c.execute(
                "INSERT INTO platform.issuer_securities "
                "(classification_id, issuer_id, valid_from, valid_to) "
                "VALUES ($1,$2,$3,NULL),($4,$5,$6,NULL)",
                PRED_CLS, PRED_ISSUER, D0, SUCC_CLS, SUCC_ISSUER, B,
            )
            yield c
        finally:
            await tx.rollback()


@pytest.mark.parametrize("as_of", [dt.date(2021, 1, 1), dt.date(2022, 5, 31), B, dt.date(2023, 1, 1)])
async def test_dispatcher_matches_oracle(reuse_pair, as_of):
    from tpcore.identity.dispatcher import IdentityDispatcher

    IdentityDispatcher.reset_shared_caches()  # the shim conn id is reused across params
    d = IdentityDispatcher(_ConnPoolShim(reuse_pair))
    got = await d.ticker_to_classification_id(TKR, as_of=as_of)
    assert got == _oracle(as_of), f"as_of={as_of}: dispatcher={got} oracle={_oracle(as_of)}"


@pytest.mark.parametrize("as_of", [dt.date(2022, 5, 31), B])
async def test_resolver_matches_oracle(reuse_pair, as_of):
    """The seam case (as_of=B) is the one the closed predicate got wrong:
    at B the resolver must resolve via the SUCCESSOR class, not the predecessor.
    """
    from tpcore.corp_history import resolve_issuer_at_date

    got = await resolve_issuer_at_date(reuse_pair, TKR, as_of)
    assert got == _oracle_issuer(as_of), (
        f"as_of={as_of}: resolver={got} oracle_issuer={_oracle_issuer(as_of)}"
    )
