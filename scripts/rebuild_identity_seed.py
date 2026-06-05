"""Rebuild Phase-1 identity seed — CSV-first from the pre-wipe snapshot.

Loads the survivorship-free identity graph from the preserved snapshot CSVs
(``data/rebuild_2026-06-04/ticker_graph_snapshot/``) into the LIVE clean
schema, per ``docs/superpowers/specs/2026-06-05-identity-entity-model-delta.md``:

  1. ``ticker_classifications`` — the universal securities spine. TKR-14 ``id``
     (= classification_id) loaded VERBATIM (the 21M prices_daily bars FK to it;
     never re-minted).
  2. ``ticker_history`` — SCD-2 ticker→classification windows, verbatim.
  3. ``lifetime_start`` RESOLVED in-DB: ``COALESCE(first_public_filing_date,
     min(ticker_history.valid_from))`` — kills the 73% ``1900-01-01`` sentinel
     (gate probe 1) and is never < FPFD (gate probe 2).
  4. Issuer graph (``issuers`` / ``issuer_securities`` / ``issuer_history``)
     REBUILT from cik-bearing **stock/reit** classifications only — the issuer
     satellite is an operating-equity model (ETFs/funds/SPACs/ADRs keep a CIK
     attribute but get no issuer row; gate probes 5/6 are asset_class-guarded).
  5. Runs the 10-probe ``evaluate_identity_gate`` and reports.

Idempotent: TRUNCATEs the five identity tables (FK-safe order) before load.
``--dry-run`` parses the CSVs + reports counts WITHOUT touching the DB.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tpcore.db import build_asyncpg_pool
from tpcore.identity.identity_gate import evaluate_identity_gate

logger = logging.getLogger("scripts.rebuild_identity_seed")

SNAPSHOT_DIR = (
    Path(__file__).resolve().parent.parent
    / "data" / "rebuild_2026-06-04" / "ticker_graph_snapshot"
)

# ── ticker_classifications: CSV header order == COPY column order ──────────
# (verified against the live platform.ticker_classifications schema 2026-06-05)
_TC_COLUMNS: tuple[str, ...] = (
    "ticker", "asset_class", "etf_inverse", "etf_leverage", "etf_category",
    "source", "last_updated", "country", "id", "figi", "cusip", "isin",
    "current_ticker", "current_exchange", "current_legal_name", "gics_sector",
    "ipo_venue", "discovery_source", "cik", "status", "updated_at",
    "lifetime_start", "lifetime_end", "instrument_subtype",
    "sec_document_type_primary", "sec_document_type_history",
    "first_public_filing_date", "fiscal_year_end_month", "last_filing_date",
    "metadata_source", "metadata_updated_at", "cik_source",
    "issuer_lifecycle_state", "issuer_lifecycle_state_source",
    "issuer_lifecycle_event_date", "issuer_lifecycle_evidence_url",
    "issuer_lifecycle_updated_at",
)
_TC_DATE_COLS = frozenset({
    "lifetime_start", "lifetime_end", "first_public_filing_date",
    "last_filing_date", "issuer_lifecycle_event_date",
})
_TC_TS_COLS = frozenset({
    "last_updated", "updated_at", "metadata_updated_at",
    "issuer_lifecycle_updated_at",
})
_TC_BOOL_COLS = frozenset({"etf_inverse"})
_TC_NUM_COLS = frozenset({"etf_leverage"})
_TC_INT_COLS = frozenset({"fiscal_year_end_month"})
_TC_JSONB_COLS = frozenset({"sec_document_type_history"})


def _d(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_tc(col: str, raw: str) -> Any:
    v = raw if raw != "" else None
    if v is None:
        return None
    if col in _TC_DATE_COLS:
        return _d(v)
    if col in _TC_TS_COLS:
        return _ts(v)
    if col in _TC_BOOL_COLS:
        return v.strip().lower() in ("t", "true", "1", "yes")
    if col in _TC_NUM_COLS:
        try:
            return Decimal(v)
        except InvalidOperation:
            return None
    if col in _TC_INT_COLS:
        try:
            return int(float(v))
        except ValueError:
            return None
    if col in _TC_JSONB_COLS:
        return v  # JSON text; cast ::jsonb in the INSERT
    return v


def _read_tc_rows() -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    with open(SNAPSHOT_DIR / "ticker_classifications.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            out.append(tuple(_coerce_tc(c, r.get(c, "")) for c in _TC_COLUMNS))
    return out


def _read_ticker_history() -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    with open(SNAPSHOT_DIR / "ticker_history.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            out.append((
                r["classification_id"], r["ticker"],
                _d(r["valid_from"]), _d(r["valid_to"]),
            ))
    return out


_TC_INSERT_SQL = (
    "INSERT INTO platform.ticker_classifications ("
    + ", ".join(_TC_COLUMNS)
    + ") VALUES ("
    + ", ".join(
        f"${i + 1}::jsonb" if c in _TC_JSONB_COLS else f"${i + 1}"
        for i, c in enumerate(_TC_COLUMNS)
    )
    + ")"
)
_TH_INSERT_SQL = (
    "INSERT INTO platform.ticker_history "
    "(classification_id, ticker, valid_from, valid_to) "
    "VALUES ($1, $2, $3, $4)"
)

# lifetime_start := FPFD if known else earliest ticker_history window start.
# Kills the 1900-01-01 sentinel (probe 1); never < FPFD (probe 2).
_RESOLVE_LIFETIME_START_SQL = """
    UPDATE platform.ticker_classifications tc
    SET lifetime_start = COALESCE(
        tc.first_public_filing_date,
        (SELECT min(th.valid_from) FROM platform.ticker_history th
          WHERE th.classification_id = tc.id),
        tc.lifetime_start)
"""

# Issuer graph — operating-equity (stock/reit) cik rows only. cik normalized
# to zfill-10; issuer_id = 'CIK'+zfill10 (the live mint convention).
_CIK_NORM = "lpad(regexp_replace(tc.cik, '[^0-9]', '', 'g'), 10, '0')"
_BUILD_ISSUERS_SQL = f"""
    INSERT INTO platform.issuers
        (issuer_id, cik, legal_name, country_of_incorp, status)
    SELECT DISTINCT ON ({_CIK_NORM})
        'CIK' || {_CIK_NORM},
        {_CIK_NORM},
        COALESCE(NULLIF(tc.current_legal_name, ''), tc.ticker),
        NULLIF(tc.country, ''),
        'active'
    FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL AND tc.cik <> ''
      AND tc.asset_class IN ('stock', 'reit')
      AND regexp_replace(tc.cik, '[^0-9]', '', 'g') <> ''
    ORDER BY {_CIK_NORM}, tc.current_legal_name NULLS LAST
"""
_BUILD_ISSUER_SECURITIES_SQL = f"""
    INSERT INTO platform.issuer_securities
        (issuer_id, classification_id, valid_from, valid_to)
    SELECT 'CIK' || {_CIK_NORM}, tc.id, tc.lifetime_start, tc.lifetime_end
    FROM platform.ticker_classifications tc
    WHERE tc.cik IS NOT NULL AND tc.cik <> ''
      AND tc.asset_class IN ('stock', 'reit')
      AND regexp_replace(tc.cik, '[^0-9]', '', 'g') <> ''
"""
_BUILD_ISSUER_HISTORY_SQL = """
    INSERT INTO platform.issuer_history
        (issuer_id, cik, legal_name, valid_from, valid_to, source)
    SELECT i.issuer_id, i.cik, i.legal_name,
        COALESCE(
            (SELECT min(es.valid_from) FROM platform.issuer_securities es
              WHERE es.issuer_id = i.issuer_id),
            DATE '2000-01-01'),
        NULL, 'snapshot_seed'
    FROM platform.issuers i
"""

_IDENTITY_TABLES_FK_SAFE = (
    "issuer_securities", "issuer_history", "ticker_history",
    "issuers", "ticker_classifications",
)


async def _insert_chunked(conn: Any, sql: str, rows: list[tuple], chunk: int = 2000) -> int:
    n = 0
    for i in range(0, len(rows), chunk):
        await conn.executemany(sql, rows[i:i + chunk])
        n += len(rows[i:i + chunk])
        logger.info("inserted %d / %d", n, len(rows))
    return n


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not SNAPSHOT_DIR.exists():
        print(f"FAILED — snapshot dir not found: {SNAPSHOT_DIR}", file=sys.stderr)
        return 1

    logger.info("reading snapshot CSVs from %s", SNAPSHOT_DIR)
    tc_rows = _read_tc_rows()
    th_rows = _read_ticker_history()
    logger.info("parsed: ticker_classifications=%d ticker_history=%d",
                len(tc_rows), len(th_rows))

    if args.dry_run:
        # report would-be issuer-graph cardinality from the parsed rows
        cik_i = _TC_COLUMNS.index("cik")
        ac_i = _TC_COLUMNS.index("asset_class")
        stock_cik = {
            r[cik_i] for r in tc_rows
            if r[cik_i] and r[ac_i] in ("stock", "reit")
        }
        print(f"DRY RUN — would seed: classifications={len(tc_rows)} "
              f"ticker_history={len(th_rows)} "
              f"distinct stock/reit issuers (by raw cik)~={len(stock_cik)}; "
              "no DB writes.")
        return 0

    db_url = os.getenv("DATABASE_URL_IPV4") or os.getenv("DATABASE_URL")
    if not db_url:
        print("FAILED — DATABASE_URL[_IPV4] not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=3)
    try:
        async with pool.acquire() as conn:
            # CASCADE: the identity tables are FK-referenced by 17 children,
            # all empty in the rebuild EXCEPT small pre-rebuild leftovers
            # (prices_daily ~42, corporate_actions ~236) that are reloaded
            # verbatim from the snapshot CSVs in Phase 2 — so cascading them
            # here is correct, not data loss. macro_data has no classification_id
            # and is NOT referenced, so SACRED hy_spread is untouched.
            logger.info("TRUNCATE identity tables CASCADE")
            await conn.execute(
                "TRUNCATE "
                + ", ".join(f"platform.{t}" for t in _IDENTITY_TABLES_FK_SAFE)
                + " CASCADE"
            )
            logger.info("loading ticker_classifications (spine, ids verbatim)")
            n_tc = await _insert_chunked(conn, _TC_INSERT_SQL, tc_rows)
            logger.info("loading ticker_history")
            n_th = await _insert_chunked(conn, _TH_INSERT_SQL, th_rows)
            logger.info("resolving lifetime_start (kill sentinel)")
            await conn.execute(_RESOLVE_LIFETIME_START_SQL)
            sentinel_left = await conn.fetchval(
                "SELECT count(*) FROM platform.ticker_classifications "
                "WHERE lifetime_start = DATE '1900-01-01'")
            logger.info("sentinel lifetime_start remaining: %d", sentinel_left)
            logger.info("building issuer graph (stock/reit cik rows)")
            await conn.execute(_BUILD_ISSUERS_SQL)
            await conn.execute(_BUILD_ISSUER_SECURITIES_SQL)
            await conn.execute(_BUILD_ISSUER_HISTORY_SQL)
            counts = {}
            for t in ("ticker_classifications", "ticker_history", "issuers",
                      "issuer_securities", "issuer_history"):
                counts[t] = await conn.fetchval(f"SELECT count(*) FROM platform.{t}")
            logger.info("seeded counts: %s", json.dumps(counts))

            logger.info("running identity gate (10 probes)")
            gate = await evaluate_identity_gate(conn)
        print("=" * 60)
        print(f"SEED COMPLETE — tc={n_tc} th={n_th}")
        print(f"counts: {json.dumps(counts)}")
        print(f"GATE passed={gate.passed}")
        for k, v in gate.violations.items():
            flag = "" if v == 0 else "  <<< VIOLATION"
            print(f"  {k:<48} {v}{flag}")
        return 0 if gate.passed else 2
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="parse CSVs + report counts, no DB writes")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
