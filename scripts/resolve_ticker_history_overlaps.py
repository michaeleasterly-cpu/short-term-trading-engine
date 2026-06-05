"""Resolve cross-classification ticker_history overlaps (gate probe 3 → 0).

The snapshot's ticker_history carries 592 same-ticker / different-classification
window overlaps across 285 tickers (the delisted-then-reused cohort + old
FMP annual-snapshot artifacts). The DB EXCLUDE only guards SAME-classification
overlap, so these must be resolved here.

Resolution (deterministic, FK-safe — only mutates ``ticker_history`` rows;
never drops a classification, never orphans a price bar):

  Per ticker, order its classifications by (price-bar count DESC, valid_from
  ASC, id) so the REAL (bar-bearing) entity wins, then greedily assign each a
  DISJOINT sub-window — clipping a lower-priority window to the gaps left by
  higher-priority ones, deleting its history row if fully subsumed. Bar counts
  come from the snapshot ``prices_daily.csv`` (the authority for "real").

Half-open '[)' semantics throughout: a contiguous handoff
(predecessor.valid_to == successor.valid_from) is NOT an overlap.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from tpcore.db import build_asyncpg_pool
from tpcore.identity.identity_gate import evaluate_identity_gate

logger = logging.getLogger("scripts.resolve_ticker_history_overlaps")

SNAP_PRICES = (
    Path(__file__).resolve().parent.parent
    / "data" / "rebuild_2026-06-04" / "ticker_graph_snapshot" / "prices_daily.csv"
)
_INF = date.max


def _subtract(base: tuple[date, date], claimed: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """[base) minus the union of [claimed) intervals → list of disjoint gaps."""
    segs = [base]
    for cs, ce in claimed:
        nxt: list[tuple[date, date]] = []
        for s, e in segs:
            if ce <= s or cs >= e:  # no overlap
                nxt.append((s, e))
                continue
            if cs > s:
                nxt.append((s, cs))
            if ce < e:
                nxt.append((ce, e))
        segs = nxt
    return [(s, e) for s, e in segs if s < e]


def _resolve_ticker(
    rows: list[tuple[str, date, date | None]],
    barcount: dict[str, int],
) -> list[tuple[str, date, date, date | None, bool]]:
    """rows: (classification_id, valid_from, valid_to). Returns plan:
    (classification_id, orig_valid_from, new_valid_from, new_valid_to, delete)."""
    order = sorted(rows, key=lambda r: (-barcount.get(r[0], 0), r[1], r[0]))
    claimed: list[tuple[date, date]] = []
    plan: list[tuple[str, date, date, date | None, bool]] = []
    for cid, vf, vt in order:
        end = vt or _INF
        gaps = _subtract((vf, end), claimed)
        if not gaps:
            plan.append((cid, vf, vf, None, True))  # fully subsumed → delete
            continue
        seg = max(gaps, key=lambda s: (s[1] - s[0]))  # keep the largest gap
        s0, s1 = seg
        plan.append((cid, vf, s0, (None if s1 == _INF else s1), False))
        claimed.append(seg)
        claimed.sort()
    return plan


async def _overlap_tickers(conn: Any) -> dict[str, list[tuple[str, date, date | None]]]:
    rows = await conn.fetch("""
        SELECT DISTINCT th.ticker, th.classification_id, th.valid_from, th.valid_to
        FROM platform.ticker_history th
        WHERE th.ticker IN (
            SELECT th1.ticker FROM platform.ticker_history th1
            JOIN platform.ticker_history th2
              ON th1.ticker = th2.ticker
             AND th1.classification_id <> th2.classification_id
             AND daterange(th1.valid_from, COALESCE(th1.valid_to, 'infinity'::date), '[)')
               && daterange(th2.valid_from, COALESCE(th2.valid_to, 'infinity'::date), '[)')
        )
        ORDER BY th.ticker, th.valid_from
    """)
    out: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    for r in rows:
        out[r["ticker"]].append((r["classification_id"], r["valid_from"], r["valid_to"]))
    return out


def _bar_counts(ids: set[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    with open(SNAP_PRICES, newline="") as fh:
        rd = csv.reader(fh)
        hdr = next(rd)
        ci = hdr.index("classification_id")
        for row in rd:
            cid = row[ci]
            if cid in ids:
                counts[cid] += 1
    return counts


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL_IPV4") or os.getenv("DATABASE_URL")
    if not db_url:
        print("FAILED — DATABASE_URL[_IPV4] not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=3)
    try:
        async with pool.acquire() as conn:
            by_ticker = await _overlap_tickers(conn)
            all_ids = {cid for rows in by_ticker.values() for cid, _, _ in rows}
            logger.info("overlap tickers=%d classifications=%d; scanning price CSV for bar counts",
                        len(by_ticker), len(all_ids))
            barcount = _bar_counts(all_ids)
            logger.info("bar counts loaded (ids with bars=%d)",
                        sum(1 for i in all_ids if barcount.get(i, 0) > 0))

            updates: list[tuple[date | None, date, str, date]] = []  # (new_vt, new_vf, cid, orig_vf) for UPDATE
            deletes: list[tuple[str, date]] = []
            n_clip = n_del = 0
            for _ticker, rows in by_ticker.items():
                for cid, orig_vf, new_vf, new_vt, delete in _resolve_ticker(rows, barcount):
                    if delete:
                        deletes.append((cid, orig_vf))
                        n_del += 1
                    else:
                        # always re-write the row (idempotent) — the value may
                        # be unchanged or clipped to a disjoint sub-window.
                        updates.append((new_vt, new_vf, cid, orig_vf))
                        if new_vf != orig_vf:
                            n_clip += 1
            logger.info("plan: %d clipped, %d deleted, %d total upserts",
                        n_clip, n_del, len(updates))

            if args.dry_run:
                print(f"DRY RUN — {len(by_ticker)} tickers: "
                      f"{n_clip} window clips, {n_del} row deletes; no DB writes.")
                return 0

            async with conn.transaction():
                for cid, orig_vf in deletes:
                    await conn.execute(
                        "DELETE FROM platform.ticker_history "
                        "WHERE classification_id=$1 AND valid_from=$2", cid, orig_vf)
                for new_vt, new_vf, cid, orig_vf in updates:
                    await conn.execute(
                        "UPDATE platform.ticker_history SET valid_from=$1, valid_to=$2 "
                        "WHERE classification_id=$3 AND valid_from=$4",
                        new_vf, new_vt, cid, orig_vf)

            gate = await evaluate_identity_gate(conn)
        print(f"RESOLVED — clipped={n_clip} deleted={n_del}")
        print(f"IDENTITY GATE passed={gate.passed}")
        for k, v in gate.violations.items():
            print(f"  {k:<48} {v}" + ("" if v == 0 else "  <<< VIOLATION"))
        return 0 if gate.passed else 2
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
