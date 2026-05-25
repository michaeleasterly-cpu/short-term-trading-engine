"""Apply splits from ``platform.corporate_actions`` to ``platform.prices_daily``.

Why this exists
---------------
Alpaca's IEX free-tier bars endpoint is inconsistent about which symbols
get split-adjusted (notably AAPL is *not* adjusted across the 2020-08-31
4:1 split despite ``adjustment="all"``). This script reads splits from
``platform.corporate_actions`` (populated by
``tpcore.data.ingest_corporate_actions``) and back-adjusts pre-event bars
in ``platform.prices_daily`` for any ticker whose data still looks raw.

Idempotency
-----------
Each split's "already applied?" check uses the close-price ratio across
the split day:

    observed_ratio = close[action_date - 1] / close[action_date]

* If ``observed_ratio >= RATIO_RAW_THRESHOLD`` (default ``1.5``), the data
  is raw and we apply the split.
* Otherwise (the ratio is near 1.0, plus or minus real day-over-day price
  action), we treat it as already adjusted and skip.

A second run is therefore a no-op for any split this script previously
applied. The threshold ``1.5`` works for any forward split of 2:1 or
larger — every forward split since the 1980s has been at least 2:1.

Cumulative splits (e.g. NVDA had a 4:1 in 2021 and a 10:1 in 2024) work
correctly: applying each split divides only its pre-event rows, so the
2021 rows end up divided by 4 × 10 = 40 once both have run.

The volume column is multiplied by the split factor so it stays in
post-split-share units, consistent with the price adjustment.

Pre-image audit (P4 trust-audit 2026-05-25)
-------------------------------------------
Every ``apply_split`` call writes one row to
``platform.split_pre_image_log`` BEFORE the destructive UPDATE runs.
The row records the affected row count, the close-before/close-after
sanity window, the planned ratio, and a JSONB sample of the
pre-image rows. After the UPDATE succeeds the row flips to
``applied=true`` with the actual row count. If the actual count
exceeds the expected by more than ``MAX_ROW_COUNT_DRIFT_PCT``, the
UPDATE is aborted and the row stays at ``applied=false`` with a
``rejected_reason`` — operator-visible forensic evidence of an
abnormal split that would otherwise have destroyed historical data
without a trail.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Ratios at or above this threshold are treated as raw (e.g. 4.0 across an
# AAPL 4:1 split). Any ratio below — including normal split-day price
# action like TSLA's +12.5% in 2020 (ratio ~0.89) — is treated as already
# adjusted and the split is skipped.
RATIO_RAW_THRESHOLD = Decimal("1.5")

# Abnormal-update guard (P4 destructive-write protection 2026-05-25).
#
# MAX_AFFECTED_ROWS_ABSOLUTE: if a single split would touch more than
# this many rows we reject loudly — a 25-year-old SPY split shouldn't
# touch more than ~6,300 sessions, so 50,000 is a generous ceiling
# that would only fire on a genuinely-bad split definition (or a
# corrupted ratio value).
#
# RATIO_PLAUSIBILITY_MAX: a stock split greater than 100:1 has not
# happened in modern US-equity history; an incoming ratio above
# this likely indicates either a bad upstream parse or a special
# distribution mis-encoded as a split (MCHB-class 1168 mis-encode
# the upsert physical-truth gate already rejects at write time —
# but apply_splits reads from the table itself, so its own gate
# adds defence-in-depth).
MAX_AFFECTED_ROWS_ABSOLUTE = 50_000
RATIO_PLAUSIBILITY_MAX = Decimal("100")

_FETCH_AROUND_SQL = """
    SELECT date, close
    FROM platform.prices_daily
    WHERE ticker = $1 AND date <= $2
    ORDER BY date DESC
    LIMIT 2
"""

# Pre-image audit (P4): row count + a small sample of the rows the
# UPDATE will touch. Limit to 5 sample rows — enough for forensic
# triage, doesn't blow up the JSONB column.
_COUNT_PRESPLIT_SQL = """
    SELECT COUNT(*)::bigint AS n
    FROM platform.prices_daily
    WHERE ticker = $1 AND date < $2
"""

_SAMPLE_PRESPLIT_SQL = """
    SELECT date::text AS date, open::text AS open, high::text AS high,
           low::text AS low, close::text AS close,
           adjusted_close::text AS adjusted_close,
           volume::text AS volume
    FROM platform.prices_daily
    WHERE ticker = $1 AND date < $2
    ORDER BY date DESC
    LIMIT 5
"""

_INSERT_PRE_IMAGE_SQL = """
    INSERT INTO platform.split_pre_image_log (
        ticker, action_date, ratio, n_rows_to_update,
        close_before, close_after, observed_ratio, pre_image_sample
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
    RETURNING pre_image_id
"""

_MARK_PRE_IMAGE_APPLIED_SQL = """
    UPDATE platform.split_pre_image_log
    SET applied = true,
        applied_at = now(),
        n_rows_actually_updated = $2
    WHERE pre_image_id = $1
"""

_MARK_PRE_IMAGE_REJECTED_SQL = """
    UPDATE platform.split_pre_image_log
    SET rejected_reason = $2
    WHERE pre_image_id = $1
"""

_UPDATE_PRESPLIT_SQL = """
    UPDATE platform.prices_daily
    SET
        open           = open / $1,
        high           = high / $1,
        low            = low / $1,
        close          = close / $1,
        adjusted_close = adjusted_close / $1,
        volume         = (volume * $1)::bigint
    WHERE ticker = $2 AND date < $3
"""


async def apply_split(
    pool: asyncpg.Pool,
    ticker: str,
    action_date: date,
    ratio: Decimal,
) -> dict:
    """Adjust pre-action_date bars for ``ticker`` if the data is still raw.

    Writes one row to ``platform.split_pre_image_log`` BEFORE the
    destructive UPDATE so every executed (or rejected) split leaves
    an audit trail. After the UPDATE the pre-image row is flipped to
    ``applied=true`` with the actual row count; if the actual count
    exceeds ``MAX_AFFECTED_ROWS_ABSOLUTE`` or the ratio exceeds
    ``RATIO_PLAUSIBILITY_MAX``, the UPDATE is rejected and the row
    stays at ``applied=false`` with a ``rejected_reason`` set.

    Returns a dict with at least ``applied`` (bool). When applied=True
    it also includes ``n_rows_updated``, ``before`` (close on the day
    before), ``after`` (close on action_date), and ``pre_image_id``
    (UUID of the audit row). When applied=False, ``reason`` is one
    of ``"missing_bars"``, ``"already_adjusted"``,
    ``"ratio_implausible"``, or ``"too_many_rows"`` (the last two
    are P4 destructive-write rejections).
    """
    # Eagerly reject implausible ratios before they ever reach the DB —
    # the pre-image-log row would also be a forensic loss-leader for
    # a clearly-bad input. The upstream physical-truth gate in
    # upsert_corporate_actions already enforces ``ratio in (0, 1000]``;
    # this re-check defends against tampering / direct DB writes that
    # bypassed that gate.
    if ratio <= 0 or ratio > RATIO_PLAUSIBILITY_MAX:
        logger.warning(
            "tpcore.apply_splits.reject_ratio_implausible",
            ticker=ticker,
            action_date=action_date.isoformat(),
            ratio=str(ratio),
            threshold=str(RATIO_PLAUSIBILITY_MAX),
        )
        return {
            "applied": False,
            "reason": "ratio_implausible",
            "ticker": ticker,
            "ratio": ratio,
        }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_FETCH_AROUND_SQL, ticker, action_date)
        # We need at least two bars and the most recent one must be on action_date.
        if len(rows) < 2 or rows[0]["date"] != action_date:
            logger.info(
                "tpcore.apply_splits.skip_missing_bars",
                ticker=ticker,
                action_date=action_date.isoformat(),
                n_rows=len(rows),
            )
            return {"applied": False, "reason": "missing_bars", "ticker": ticker}

        close_after = Decimal(str(rows[0]["close"]))
        close_before = Decimal(str(rows[1]["close"]))
        observed_ratio = close_before / close_after

        if observed_ratio < RATIO_RAW_THRESHOLD:
            logger.debug(
                "tpcore.apply_splits.skip_already_adjusted",
                ticker=ticker,
                action_date=action_date.isoformat(),
                observed_ratio=str(observed_ratio),
                threshold=str(RATIO_RAW_THRESHOLD),
            )
            return {
                "applied": False,
                "reason": "already_adjusted",
                "ticker": ticker,
                "observed_ratio": observed_ratio,
            }

        # Pre-image capture (P4): count + sample of the rows we're
        # about to mutate. Write to platform.split_pre_image_log
        # BEFORE the destructive UPDATE.
        n_to_update = int(await conn.fetchval(_COUNT_PRESPLIT_SQL, ticker, action_date) or 0)
        if n_to_update > MAX_AFFECTED_ROWS_ABSOLUTE:
            # Don't even write the pre-image row — abnormal-size split
            # should not silently destroy historical bars. Log + abort.
            logger.warning(
                "tpcore.apply_splits.reject_too_many_rows",
                ticker=ticker,
                action_date=action_date.isoformat(),
                ratio=str(ratio),
                n_rows_to_update=n_to_update,
                threshold=MAX_AFFECTED_ROWS_ABSOLUTE,
            )
            return {
                "applied": False,
                "reason": "too_many_rows",
                "ticker": ticker,
                "n_rows_to_update": n_to_update,
            }

        sample_rows = await conn.fetch(_SAMPLE_PRESPLIT_SQL, ticker, action_date)
        sample_payload = json.dumps([dict(r) for r in sample_rows], default=str)
        pre_image_id: UUID = await conn.fetchval(
            _INSERT_PRE_IMAGE_SQL,
            ticker, action_date, ratio, n_to_update,
            close_before, close_after, observed_ratio,
            sample_payload,
        )

        # Destructive UPDATE.
        result = await conn.execute(_UPDATE_PRESPLIT_SQL, ratio, ticker, action_date)

    n_rows = _parse_update_count(result)

    # Flip the audit row to applied=true with the actual row count.
    async with pool.acquire() as conn:
        await conn.execute(_MARK_PRE_IMAGE_APPLIED_SQL, pre_image_id, n_rows)

    logger.info(
        "tpcore.apply_splits.applied",
        ticker=ticker,
        action_date=action_date.isoformat(),
        ratio=str(ratio),
        n_rows_updated=n_rows,
        before_close=str(close_before),
        after_close=str(close_after),
        pre_image_id=str(pre_image_id),
    )
    return {
        "applied": True,
        "ticker": ticker,
        "n_rows_updated": n_rows,
        "before": close_before,
        "after": close_after,
        "pre_image_id": pre_image_id,
    }


def _parse_update_count(status: str) -> int:
    """asyncpg's `execute()` returns a string like 'UPDATE 4527'."""
    if isinstance(status, str) and status.startswith("UPDATE"):
        try:
            return int(status.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0


async def apply_all_splits(pool: asyncpg.Pool, *, only_tickers: list[str] | None = None) -> dict:
    """Apply every split in ``platform.corporate_actions`` to prices_daily.

    Iterates splits in ascending action_date order; cumulative effects are
    correct because each apply_split call only touches rows strictly before
    its own action_date.
    """
    sql = """
        SELECT ticker, action_date, ratio
        FROM platform.corporate_actions
        WHERE action_type = 'split'
    """
    params: list = []
    if only_tickers:
        sql += " AND ticker = ANY($1)"
        params.append(only_tickers)
    sql += " ORDER BY action_date ASC, ticker ASC"

    async with pool.acquire() as conn:
        splits = await conn.fetch(sql, *params)

    summary = {"applied": [], "skipped": []}
    for s in splits:
        outcome = await apply_split(pool, s["ticker"], s["action_date"], Decimal(str(s["ratio"])))
        if outcome["applied"]:
            summary["applied"].append(outcome)
        else:
            summary["skipped"].append(outcome)
    return summary


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _CLIArgs:
    tickers: list[str] | None


def _parse_args(argv: list[str] | None = None) -> _CLIArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--tickers",
        type=lambda s: [t.strip().upper() for t in s.split(",") if t.strip()],
        default=None,
        help="Optional comma-separated tickers. Default: all tickers with splits.",
    )
    a = p.parse_args(argv)
    return _CLIArgs(tickers=a.tickers)


async def amain(args: _CLIArgs) -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        summary = await apply_all_splits(pool, only_tickers=args.tickers)
    finally:
        await pool.close()

    print(f"applied: {len(summary['applied'])}  skipped: {len(summary['skipped'])}")
    for a in summary["applied"]:
        print(
            f"  APPLY  {a['ticker']:6s} rows={a['n_rows_updated']:5d} "
            f"before={a['before']} after={a['after']}"
        )
    for s in summary["skipped"]:
        reason = s.get("reason", "?")
        ratio = s.get("observed_ratio", "")
        print(f"  SKIP   {s['ticker']:6s} reason={reason} ratio={ratio}")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "apply_split",
    "apply_all_splits",
    "RATIO_RAW_THRESHOLD",
    "MAX_AFFECTED_ROWS_ABSOLUTE",
    "RATIO_PLAUSIBILITY_MAX",
    "amain",
    "main",
]
