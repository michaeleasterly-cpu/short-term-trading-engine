"""Clean-slate identity-spine STAGING build + P1-P5 gate (Phase A orchestrator).

Plan: ``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md``
(REVISED 2026-06-08). Spec §0 / §7-B. NON-destructive to the live
``platform.*`` spine: this mints a clean-slate spine into a STAGING schema
(default ``platform_stage_spine``) and runs the staging gate against it. The
destructive cut (wipe children, swap in the clean spine) is a SEPARATE later
step gated on operator + a green staging gate.

This is the I/O seam for ``tpcore.identity.staging_spine_build`` (the pure
mint) + ``tpcore.identity.staging_gate`` (the P1-P5 probes). It:

  1. Reads the per-symbol price-bar span from the LIVE ``platform.prices_daily``
     (the binding P3 constraint — the EXACT bars the re-ingest will write).
  2. Builds the SEC ``tickers[]`` → CIK index + per-CIK metadata (FPFD,
     formerNames, SIC, sec_document_type_primary, Form-25/15 delisting) from
     the cached ``/tmp/sec_submissions.zip`` (bulk-first; zero per-CIK HTTP).
  3. Pulls FMP/known-delisting corroboration from the LIVE spine's existing
     evidence columns (the FMP listing/delisting already captured there) +
     ``KNOWN_DELISTINGS``.
  4. Resolves identity SEC-first per priced symbol (+ the non-priced SEC
     universe for completeness), collapsing SPAC unit/warrant/share-class
     variants of one CIK, and assembles ``SpineBuildInput`` rows.
  5. Mints the clean ``SpineSecurity`` set, derives ``ticker_history`` (reuse
     of ``derive_ticker_history``), creates the staging schema/tables, and
     writes them.
  6. Runs the P1-P5 staging gate and prints the verdict.

Usage::

    set -a; source .env; set +a
    .venv/bin/python -m scripts.stage_spine_build            # build + gate
    .venv/bin/python -m scripts.stage_spine_build --gate-only  # re-run gate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import asyncpg
import structlog

from tpcore.data.survivorship_backfill import KNOWN_DELISTINGS
from tpcore.identity.staging_gate import evaluate_staging_gate
from tpcore.identity.staging_spine_build import (
    SpineBuildInput,
    assemble_spine,
)
from tpcore.identity.ticker_history_reuse_build import (
    ClassificationLifetime,
    derive_ticker_history,
)

logger = structlog.get_logger("stage_spine_build")

STAGE_SCHEMA = "platform_stage_spine"
SEC_ZIP = Path("/tmp/sec_submissions.zip")  # noqa: S108
SEC_EXTRACT_CACHE = Path("/tmp/sec_spine_extract.json")  # noqa: S108

# Active-recency window: a symbol whose last bar is within this many days of
# the run date is treated as still-trading (open window). 30 NYSE sessions ~
# 45 calendar days; we use 60 to be conservative against holiday gaps.
_ACTIVE_RECENCY_DAYS = 60

# SEC document-type → asset_class authority (spec A3 / build rule §asset_class).
_DOCTYPE_ASSET_CLASS: dict[str, str] = {
    "10-Q": "stock", "10-K": "stock",
    "20-F": "adr", "40-F": "adr", "6-K": "adr",
}
_REIT_SIC = "6798"
_BLANK_CHECK_SIC = "6770"  # SPAC


# ──────────────────────────────────────────────────────────────────────
# DB connect (retry — the IPv4 endpoint occasionally cold-starts)
# ──────────────────────────────────────────────────────────────────────
async def _connect() -> asyncpg.Connection:
    url = os.environ["DATABASE_URL_IPV4"]
    last: Exception | None = None
    for _ in range(6):
        try:
            conn = await asyncpg.connect(url, timeout=40)
            await conn.execute("SET statement_timeout=0")
            return conn
        except Exception as exc:  # noqa: BLE001 — transient cold-start
            last = exc
            await asyncio.sleep(3)
    raise RuntimeError(f"could not connect to live DB: {last}")


# ──────────────────────────────────────────────────────────────────────
# SEC extract — one zip scan, cached to JSON
# ──────────────────────────────────────────────────────────────────────
def _padded(cik_int: int) -> str:
    return str(cik_int).zfill(10)


def _parse_sec_date(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def build_sec_extract() -> dict[str, dict]:
    """Scan the cached submissions.zip ONCE → per-CIK metadata dict.

    Returns ``{cik_padded: {tickers, legal_name, sic, fpfd, doctype,
    delisting_date, exchanges}}``. Cached to ``SEC_EXTRACT_CACHE`` so re-runs
    skip the ~30s scan.
    """
    if SEC_EXTRACT_CACHE.exists():
        logger.info("sec_extract.cache_hit", path=str(SEC_EXTRACT_CACHE))
        return json.loads(SEC_EXTRACT_CACHE.read_text())

    from tpcore.sec.companyfacts_adapter import SECCompanyFactsAdapter

    if not SEC_ZIP.exists():
        raise RuntimeError(f"SEC submissions zip not cached at {SEC_ZIP}")
    zf = zipfile.ZipFile(SEC_ZIP, "r")
    bases = [
        n for n in zf.namelist()
        if n.startswith("CIK") and n.endswith(".json") and "-submissions-" not in n
    ]
    logger.info("sec_extract.scanning", n_base=len(bases))
    from tpcore.sec.submissions_bulk_reader import SECSubmissionsBulkReader

    reader = SECSubmissionsBulkReader(zip_path=SEC_ZIP, local_dir=Path("/nonexistent"))
    out: dict[str, dict] = {}
    n = 0
    for nm in bases:
        try:
            base = json.loads(zf.read(nm))
        except Exception:  # noqa: BLE001 — skip a corrupt shard, surface count
            continue
        cik_int = int(nm[3:-5])
        cik = _padded(cik_int)
        tickers = [
            t.strip().upper()
            for t in (base.get("tickers") or [])
            if t and t.strip()
        ]
        if not tickers:
            continue  # no live symbol → not a tradeable identity anchor
        # FPFD + doctype need the MERGED (full-history) payload.
        merged = reader.get_merged_submissions(cik)
        fpfd = None
        doctype = None
        if merged is not None and not merged.get("_shard_errors"):
            meta = SECCompanyFactsAdapter.extract_filing_metadata(merged)
            fpfd = meta.get("first_public_filing_date")
            fpfd = fpfd.isoformat() if isinstance(fpfd, date) else None
            doctype = meta.get("sec_document_type_primary")
            life = SECCompanyFactsAdapter.extract_lifecycle_events(
                merged, cik=cik
            )
        else:
            life = {"derived_event_date": None}
        delisting = life.get("derived_event_date")
        delisting = (
            delisting.isoformat() if isinstance(delisting, date) else None
        )
        out[cik] = {
            "tickers": tickers,
            "legal_name": base.get("name"),
            "sic": str(base.get("sic") or ""),
            "exchanges": base.get("exchanges") or [],
            "fpfd": fpfd,
            "doctype": doctype,
            "delisting_date": delisting,
        }
        n += 1
        if n % 2000 == 0:
            logger.info("sec_extract.progress", processed=n)
    zf.close()
    SEC_EXTRACT_CACHE.write_text(json.dumps(out))
    logger.info("sec_extract.done", n_ciks=len(out), cache=str(SEC_EXTRACT_CACHE))
    return out


def _sec_asset_class(rec: dict, prior: str | None) -> tuple[str, bool]:
    """SEC-authoritative asset_class (spec A3). Returns (asset_class, verified).

    Precedence: sec_document_type_primary (10-Q/10-K⇒stock, 20-F/40-F⇒adr) →
    SIC 6798⇒reit, 6770⇒spac → else keep prior (unverified)."""
    doctype = (rec.get("doctype") or "").upper()
    if doctype in _DOCTYPE_ASSET_CLASS:
        return _DOCTYPE_ASSET_CLASS[doctype], True
    sic = rec.get("sic") or ""
    if sic == _REIT_SIC:
        return "reit", True
    if sic == _BLANK_CHECK_SIC:
        return "spac", True
    return (prior or "stock"), False


# ──────────────────────────────────────────────────────────────────────
# Live-DB reads
# ──────────────────────────────────────────────────────────────────────
async def load_price_spans(conn: asyncpg.Connection) -> dict[str, dict]:
    """Per-symbol min/max bar date + n_bars from live prices_daily.

    Materializes a helper table (reused across runs) to avoid the 21M-row
    full scan each call."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS platform.tmp_price_bar_span AS
        SELECT ticker, min(date) AS min_date, max(date) AS max_date,
               count(*) AS n_bars
        FROM platform.prices_daily GROUP BY ticker
    """)
    rows = await conn.fetch("SELECT ticker, min_date, max_date, n_bars FROM platform.tmp_price_bar_span")
    return {
        r["ticker"]: {
            "min_date": r["min_date"],
            "max_date": r["max_date"],
            "n_bars": int(r["n_bars"]),
        }
        for r in rows
    }


async def load_live_evidence(conn: asyncpg.Connection) -> dict[str, dict]:
    """Per-symbol FMP/listing evidence carried in the LIVE spine.

    The live ``ticker_classifications`` already captured FMP listing/delisting
    evidence (``lifetime_start`` from FMP-earliest for FMP-only rows,
    ``lifetime_end`` from FMP delisting). We harvest it as corroboration for
    the clean mint WITHOUT a fresh FMP network call (the evidence is already on
    disk). Keyed by ticker; for a reused ticker we take the EARLIEST start +
    the LATEST end seen (the widest FMP evidence)."""
    rows = await conn.fetch("""
        SELECT ticker, current_ticker, cik, asset_class, country,
               lifetime_start, lifetime_end, source,
               current_legal_name, first_public_filing_date
        FROM platform.ticker_classifications
    """)
    out: dict[str, dict] = {}
    for r in rows:
        for key in {r["ticker"], r["current_ticker"]}:
            if not key:
                continue
            cur = out.get(key)
            ls = r["lifetime_start"]
            le = r["lifetime_end"]
            country = (r["country"] or "US").strip() or "US"
            name = r["current_legal_name"]
            if cur is None:
                out[key] = {
                    "fmp_earliest": ls,
                    "fmp_delisting": le,
                    "cik": r["cik"],
                    "asset_class": r["asset_class"],
                    "country": country,
                    "legal_name": name,
                }
            else:
                if ls is not None and (
                    cur["fmp_earliest"] is None or ls < cur["fmp_earliest"]
                ):
                    cur["fmp_earliest"] = ls
                if le is not None and (
                    cur["fmp_delisting"] is None or le > cur["fmp_delisting"]
                ):
                    cur["fmp_delisting"] = le
                if cur["cik"] is None and r["cik"]:
                    cur["cik"] = r["cik"]
                if cur["legal_name"] is None and name:
                    cur["legal_name"] = name
    return out


# ──────────────────────────────────────────────────────────────────────
# Build inputs
# ──────────────────────────────────────────────────────────────────────
def build_inputs(
    *,
    price_spans: dict[str, dict],
    sec_extract: dict[str, dict],
    live_evidence: dict[str, dict],
    now: datetime,
) -> tuple[list[SpineBuildInput], dict[str, int]]:
    """Assemble the clean-slate build inputs — one per real (entity, symbol).

    SEC-first identity: build a ticker→CIK index from SEC ``tickers[]``. A
    priced symbol in that index is SEC-backed; otherwise FMP/price-evidence
    fallback. SPAC unit/warrant/share-class variants of one CIK each appear in
    ``tickers[]`` and each priced one gets its own input (collapsed onto the
    shared CIK / issuer, NOT 7 Jan-1 dups). Non-priced SEC symbols are included
    for completeness (the spine is the universe, not just the priced subset),
    but P3 only constrains priced symbols.
    """
    # SEC ticker → cik (first CIK wins; cross-CIK current reuse is ~3 symbols).
    sec_ticker_to_cik: dict[str, str] = {}
    for cik, rec in sec_extract.items():
        for t in rec["tickers"]:
            sec_ticker_to_cik.setdefault(t, cik)

    recency_floor = date.fromordinal(now.date().toordinal() - _ACTIVE_RECENCY_DAYS)
    known_delist: dict[str, date] = {}
    for tk, dt, _note in KNOWN_DELISTINGS:
        try:
            known_delist[tk.upper()] = date.fromisoformat(dt)
        except ValueError:
            continue

    inputs: list[SpineBuildInput] = []
    stats = {"sec_priced": 0, "fmp_priced": 0, "sec_nonpriced": 0}
    seen_symbols: set[str] = set()

    # 1. Every priced symbol gets an input (the binding P3 set).
    for ticker, span in sorted(price_spans.items()):
        seen_symbols.add(ticker)
        cik = sec_ticker_to_cik.get(ticker)
        ev = live_evidence.get(ticker, {})
        first_bar = span["min_date"]
        last_bar = span["max_date"]
        still = last_bar >= recency_floor
        if cik is not None:
            rec = sec_extract[cik]
            ac, verified = _sec_asset_class(rec, ev.get("asset_class"))
            fpfd = date.fromisoformat(rec["fpfd"]) if rec.get("fpfd") else None
            sec_del = (
                date.fromisoformat(rec["delisting_date"])
                if rec.get("delisting_date")
                else None
            )
            inputs.append(SpineBuildInput(
                ticker=ticker,
                asset_class=ac,
                cik=cik,
                legal_name=rec.get("legal_name") or ev.get("legal_name"),
                country="US",
                discovery_source="S",
                asset_class_verified=verified,
                fpfd=fpfd,
                first_bar=first_bar,
                last_bar=last_bar,
                fmp_earliest=ev.get("fmp_earliest"),
                sec_delisting_date=sec_del,
                fmp_delisting_date=ev.get("fmp_delisting"),
                known_delisting_date=known_delist.get(ticker),
                still_trading=still,
            ))
            stats["sec_priced"] += 1
        else:
            inputs.append(SpineBuildInput(
                ticker=ticker,
                asset_class=ev.get("asset_class") or "stock",
                cik=ev.get("cik"),
                legal_name=ev.get("legal_name"),
                country=ev.get("country") or "US",
                discovery_source="F",
                asset_class_verified=False,
                fpfd=None,
                first_bar=first_bar,
                last_bar=last_bar,
                fmp_earliest=ev.get("fmp_earliest"),
                fmp_delisting_date=ev.get("fmp_delisting"),
                known_delisting_date=known_delist.get(ticker),
                still_trading=still,
            ))
            stats["fmp_priced"] += 1

    # 2. Non-priced SEC symbols (completeness — the universe includes entities
    #    with no bars in our snapshot). One input per (cik, symbol) NOT already
    #    minted as a priced symbol. These have no price-bar constraint, so the
    #    window is anchored at FPFD (open if a current filer, else SEC/known
    #    delisting). Skipped when FPFD is unknown (no real-day anchor → would
    #    need a synthetic; we DROP rather than fabricate).
    for cik, rec in sorted(sec_extract.items()):
        fpfd = date.fromisoformat(rec["fpfd"]) if rec.get("fpfd") else None
        if fpfd is None:
            continue
        sec_del = (
            date.fromisoformat(rec["delisting_date"])
            if rec.get("delisting_date")
            else None
        )
        for ticker in rec["tickers"]:
            if ticker in seen_symbols:
                continue
            seen_symbols.add(ticker)
            ac, verified = _sec_asset_class(rec, None)
            inputs.append(SpineBuildInput(
                ticker=ticker,
                asset_class=ac,
                cik=cik,
                legal_name=rec.get("legal_name"),
                country="US",
                discovery_source="S",
                asset_class_verified=verified,
                fpfd=fpfd,
                sec_delisting_date=sec_del,
                still_trading=sec_del is None,
            ))
            stats["sec_nonpriced"] += 1

    return inputs, stats


# ──────────────────────────────────────────────────────────────────────
# Staging schema + write
# ──────────────────────────────────────────────────────────────────────
async def create_staging(conn: asyncpg.Connection) -> None:
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {STAGE_SCHEMA}")
    # A 30s lock_timeout surfaces a stale lock (a prior killed run holding the
    # table) loudly instead of hanging forever; CASCADE drops the dependent
    # index/PK in one shot.
    await conn.execute("SET lock_timeout = '30s'")
    await conn.execute(
        f"DROP TABLE IF EXISTS {STAGE_SCHEMA}.ticker_history CASCADE"
    )
    await conn.execute(
        f"DROP TABLE IF EXISTS {STAGE_SCHEMA}.ticker_classifications CASCADE"
    )
    await conn.execute("SET lock_timeout = 0")
    await conn.execute(f"""
        CREATE TABLE {STAGE_SCHEMA}.ticker_classifications (
            id text PRIMARY KEY,
            ticker text NOT NULL,
            current_ticker text,
            asset_class text NOT NULL,
            source text NOT NULL,
            cik text,
            legal_name text,
            lifetime_start date NOT NULL,
            lifetime_end date,
            discovery_source text,
            metadata_source text,
            asset_class_verified boolean NOT NULL DEFAULT false,
            first_public_filing_date date,
            CONSTRAINT tc_stage_lifetime_order
                CHECK (lifetime_end IS NULL OR lifetime_end > lifetime_start)
        )
    """)
    await conn.execute(f"""
        CREATE TABLE {STAGE_SCHEMA}.ticker_history (
            classification_id text NOT NULL,
            ticker text NOT NULL,
            valid_from date NOT NULL,
            valid_to date,
            PRIMARY KEY (classification_id, valid_from)
        )
    """)
    await conn.execute(
        f"CREATE INDEX ON {STAGE_SCHEMA}.ticker_history (ticker)"
    )


async def write_spine(
    conn: asyncpg.Connection, securities: list, history: list
) -> None:
    """Bulk-write via COPY (asyncpg ``copy_records_to_table``) — orders of
    magnitude faster than per-row ``executemany`` for ~12k+24k rows. The
    staging tables are freshly created (no conflict to handle), so COPY is
    safe + the fastest path."""
    await conn.copy_records_to_table(
        "ticker_classifications",
        schema_name=STAGE_SCHEMA,
        columns=[
            "id", "ticker", "current_ticker", "asset_class", "source", "cik",
            "legal_name", "lifetime_start", "lifetime_end", "discovery_source",
            "metadata_source", "asset_class_verified", "first_public_filing_date",
        ],
        records=[
            (
                s.id, s.ticker, s.current_ticker, s.asset_class, s.source,
                s.cik, s.legal_name, s.lifetime_start, s.lifetime_end,
                s.discovery_source, s.metadata_source, s.asset_class_verified,
                s.first_public_filing_date,
            )
            for s in securities
        ],
    )
    await conn.copy_records_to_table(
        "ticker_history",
        schema_name=STAGE_SCHEMA,
        columns=["classification_id", "ticker", "valid_from", "valid_to"],
        records=[
            (h.classification_id, h.ticker, h.valid_from, h.valid_to)
            for h in history
        ],
    )


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
async def run(*, gate_only: bool) -> int:
    conn = await _connect()
    try:
        if not gate_only:
            now = datetime.now(UTC)
            logger.info("stage_spine.start")
            price_spans = await load_price_spans(conn)
            logger.info("stage_spine.price_spans", n=len(price_spans))
            sec_extract = build_sec_extract()
            logger.info("stage_spine.sec_extract", n_ciks=len(sec_extract))
            live_evidence = await load_live_evidence(conn)
            logger.info("stage_spine.live_evidence", n=len(live_evidence))

            inputs, stats = build_inputs(
                price_spans=price_spans,
                sec_extract=sec_extract,
                live_evidence=live_evidence,
                now=now,
            )
            logger.info("stage_spine.inputs", n=len(inputs), **stats)

            securities = assemble_spine(inputs, now=now)
            lifetimes = [
                ClassificationLifetime(
                    classification_id=s.id,
                    ticker=s.ticker,
                    lifetime_start=s.lifetime_start,
                    lifetime_end=s.lifetime_end,
                )
                for s in securities
            ]
            history = derive_ticker_history(lifetimes)
            logger.info(
                "stage_spine.derived",
                n_classifications=len(securities),
                n_windows=len(history),
            )

            await create_staging(conn)
            await write_spine(conn, securities, history)
            logger.info("stage_spine.written", schema=STAGE_SCHEMA)

        # The orchestrator materialized platform.tmp_price_bar_span in
        # load_price_spans (build path); the gate reuses it so P3 is instant
        # against the 21M-row live prices table. On --gate-only it may not
        # exist yet — refresh it (idempotent CREATE) before gating.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS platform.tmp_price_bar_span AS
            SELECT ticker, min(date) AS min_date, max(date) AS max_date,
                   count(*) AS n_bars
            FROM platform.prices_daily GROUP BY ticker
        """)
        result = await evaluate_staging_gate(
            conn,
            spine_schema=STAGE_SCHEMA,
            span_table="platform.tmp_price_bar_span",
            raise_on_fail=False,
        )
        n_cls = await conn.fetchval(
            f"SELECT count(*) FROM {STAGE_SCHEMA}.ticker_classifications"
        )
        n_win = await conn.fetchval(
            f"SELECT count(*) FROM {STAGE_SCHEMA}.ticker_history"
        )
        print("=" * 64)
        print(f"STAGED SPINE: {n_cls} classifications, {n_win} windows")
        print(f"GATE PASSED: {result.passed}")
        if result.violations:
            print("VIOLATIONS:")
            for k, v in result.violations.items():
                print(f"  {k}: {v}")
        if result.p3_violator_sample:
            print(f"P3 violator sample ({len(result.p3_violator_sample)} shown):")
            for v in result.p3_violator_sample[:40]:
                print(f"  {v['ticker']:10} {v['min_date']} .. {v['max_date']} "
                      f"({v['n_bars']} bars)")
        print("=" * 64)
        return 0 if result.passed else 1
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate-only", action="store_true", help="re-run gate only")
    args = ap.parse_args()
    return asyncio.run(run(gate_only=args.gate_only))


if __name__ == "__main__":
    raise SystemExit(main())
