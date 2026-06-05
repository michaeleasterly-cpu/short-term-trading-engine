"""Ticker-history reuse derivation — the ``ticker_classifications`` lifetime
→ SCD-2 ``ticker_history`` pure layer (Plan 3 Phase 1).

Spec: ``docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md`` §4
/ §5.3; corp-history §3.1-§3.4. Invariant G3 (delisted-then-reused ticker).

This is the **pure** half of the ``ticker_history_reuse_build`` stage — no
DB, no network. It takes the already-fetched ``ticker_classifications``
lifetimes (``classification_id``, ``ticker``, ``lifetime_start``,
``lifetime_end``) and DERIVES the SCD-2 ``ticker_history`` timeline:

  * one ``TickerHistoryRow`` per classification —
    ``(classification_id, ticker, valid_from=lifetime_start,
    valid_to=lifetime_end)`` — so a **delisted-then-reused** ticker (the
    same symbol later assigned to a different entity) gets MULTIPLE
    contiguous rows (G3), one per classification.

The ``ticker_history_no_overlap`` EXCLUDE constraint (migration
``20260524_0100``:68-72) keys on ``classification_id WITH =``, so it ONLY
rejects overlap WITHIN a single classification — it does NOT guard the
cross-classification, same-ticker (G3 reuse) overlap that this module
derives. That cross-ticker overlap is guarded by **this module's pure-layer
hard-stop** below AND the ``identity_gate`` ``ticker_history_overlaps``
probe — NOT by the DB EXCLUDE. The gate must therefore run on every live
build (the EXCLUDE alone would let a cross-classification overlap through).
This module uses the same half-open ``daterange(valid_from,
COALESCE(valid_to,'infinity'),'[)')`` semantics: contiguous handoff
(``predecessor.valid_to == successor.valid_from``) is fine, **overlap
HARD-STOPS** (a data defect to surface, not silently mangle).

The stage handler in ``scripts/ops.py::_stage_ticker_history_reuse_build``
owns the I/O — it SELECTs the lifetimes, calls ``derive_ticker_history``
here, and chunk-upserts ``ON CONFLICT (classification_id, valid_from) DO
NOTHING`` (idempotent — a re-run is a no-op).

Review lessons applied PROACTIVELY:
  * **Date-order guard** — a classification with ``lifetime_end <=
    lifetime_start`` (garbled) is DROPPED + WARN (the ``tc_lifetime_order``
    / ``ticker_history`` CHECK would reject the whole chunk).
  * **No-sentinel** — a NULL ``lifetime_start`` is rejected at the model
    (universe_build always sets it, A6); a missing one is a surfaced
    defect, never silently defaulted.
  * **Overlap hard-stop** — surfaces the defect rather than dropping a row
    the EXCLUDE would reject.
"""
from __future__ import annotations

from datetime import date

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class ClassificationLifetime(BaseModel):
    """One ``ticker_classifications`` lifetime row (the input to the
    derivation). ``lifetime_start`` is NOT NULL (the no-sentinel A6
    invariant — universe_build always sets it)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification_id: str
    ticker: str
    lifetime_start: date
    lifetime_end: date | None = None


class TickerHistoryRow(BaseModel):
    """One SCD-2 ``platform.ticker_history`` row. Half-open window
    ``[valid_from, valid_to)``; the current (open) row carries
    ``valid_to=None``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification_id: str
    ticker: str
    valid_from: date
    valid_to: date | None = None


def derive_ticker_history(
    lifetimes: list[ClassificationLifetime],
) -> list[TickerHistoryRow]:
    """Derive the SCD-2 ``ticker_history`` timeline from classification
    lifetimes (G3 reuse).

    Pure function: no I/O. For each ``ticker``, its classifications are
    ordered by ``lifetime_start`` and emitted one ``TickerHistoryRow`` each.
    A delisted-then-reused ticker therefore gets MULTIPLE contiguous rows.

    Guards:
      * ``lifetime_end <= lifetime_start`` → DROP + WARN (date-order).
      * Half-open windows for the same ticker (ACROSS classifications) that
        OVERLAP → ``ValueError``. NOTE: the DB EXCLUDE keys on
        ``classification_id WITH =`` and so does NOT catch this
        cross-classification overlap — this hard-stop (and the
        ``identity_gate`` overlap probe) is the ONLY guard for G3 reuse
        overlap; surfaced, not mangled. Contiguous handoff
        (``valid_to == next valid_from``) is allowed.
    """
    by_ticker: dict[str, list[ClassificationLifetime]] = {}
    for cl in lifetimes:
        ticker = cl.ticker.strip().upper()
        if not ticker:
            continue
        if cl.lifetime_end is not None and cl.lifetime_end <= cl.lifetime_start:
            logger.warning(
                "ticker_history_reuse.bad_lifetime_order_dropped",
                classification_id=cl.classification_id,
                ticker=ticker,
                lifetime_start=cl.lifetime_start.isoformat(),
                lifetime_end=cl.lifetime_end.isoformat(),
            )
            continue
        by_ticker.setdefault(ticker, []).append(cl)

    out: list[TickerHistoryRow] = []
    for ticker in sorted(by_ticker):
        # Order by (lifetime_start, classification_id) — deterministic.
        ordered = sorted(
            by_ticker[ticker],
            key=lambda c: (c.lifetime_start, c.classification_id),
        )
        prev: ClassificationLifetime | None = None
        for cl in ordered:
            if prev is not None:
                prev_end = prev.lifetime_end  # half-open upper bound
                # Overlap iff predecessor has no end (open → infinity) OR
                # its end is strictly AFTER this start. Equal end==start is
                # contiguous handoff (allowed by the '[)' EXCLUDE).
                if prev_end is None or prev_end > cl.lifetime_start:
                    raise ValueError(
                        "ticker_history_reuse: overlapping windows for ticker "
                        f"{ticker!r} — {prev.classification_id} "
                        f"[{prev.lifetime_start}, {prev_end}) and "
                        f"{cl.classification_id} starting "
                        f"{cl.lifetime_start}. The DB EXCLUDE keys on "
                        "classification_id WITH = and does NOT catch this "
                        "cross-classification overlap; this hard-stop + the "
                        "identity_gate overlap probe are the guard. Surfacing "
                        "the defect rather than silently mangling it (G3)."
                    )
            out.append(
                TickerHistoryRow(
                    classification_id=cl.classification_id,
                    ticker=ticker,
                    valid_from=cl.lifetime_start,
                    valid_to=cl.lifetime_end,
                )
            )
            prev = cl

    logger.info(
        "ticker_history_reuse.derived",
        n_classifications=len(lifetimes),
        n_rows=len(out),
        n_reused_tickers=sum(
            1 for t in by_ticker.values() if len(t) > 1
        ),
    )
    return out


__all__ = [
    "ClassificationLifetime",
    "TickerHistoryRow",
    "derive_ticker_history",
]
