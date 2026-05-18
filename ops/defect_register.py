"""Consolidated Defect Register — derived read-model (#254, Phase DR1).

The platform's render-the-SoT doctrine: defect/issue state is
distributed across the two Escalation & Hardening Ladders (engine lane
+ data lane). A new authoritative `defects` table everything writes to
is the parallel-SoT / rat's-nest anti-pattern (rejected by the spec).

This module is a **derived consolidated READ-MODEL** that calls the
existing Ladder read APIs *verbatim* and re-derives nothing:

* engine lane → ``ops.engine_ladder.list_undispositioned`` (the
  open-undispositioned engine escalations; keyed on ``hold_id``).
* data lane  → ``ops.weekly_digest.build_weekly_digest`` →
  ``.undispositioned_entries`` (the data-lane Ladder's STRUCTURED
  open-undispositioned escalations; the clean ``ref`` is read off the
  struct — never regex-scraped from the rendered display string, whose
  format is the digest's own concern and may drift).

It issues **NO** ``application_log`` escalation query of its own — the
register and the weekly digest call the SAME functions, so they are
*incapable of disagreeing*. The two collections are mapped to typed
``DefectRow``s and **joined by ``defect_ref`` (never summed)** so a ref
present in both lanes collapses to ONE row.

Phase DR2 adds the ONE missing primitive: the review-found-defect
class (``REVIEW_DEFECT_LOGGED`` / ``REVIEW_DEFECT_RESOLVED``) on the
existing ``application_log`` substrate (schemaless ``data jsonb`` — no
table, no migration, no new daemon, no new write-coupling on any
existing producer). These two event types are retention-exempt
(``tpcore.logging.db_handler.RETENTION_EXEMPT_EVENT_TYPES``) so an open
review defect never silently expires. The register now folds the
review open-set (anti-join open-predicate, identical to the engine
Ladder's) into the same ``defect_ref`` join — never sums.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog

from ops import engine_ladder, weekly_digest
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DefectRow:
    """One consolidated defect. DR1 emits escalation-origin rows only.

    ``state`` is derived ONLY from the disposition state the Ladders
    already expose (an entry still present in a Ladder's
    undispositioned set IS, by that Ladder's own anti-join open
    predicate, ``open``) — it is never re-derived here.
    """

    defect_ref: str
    origin: Literal["escalation", "review"]
    lane: str  # "engine" | "data" | review-supplied lane
    summary: str
    state: str  # "open" | "dispositioned-structural-parked" | "fixed"
    opened_at: datetime
    policy: str | None  # the advisory disposition/policy the Ladder attaches
    fix_ref: str | None  # review-found: the resolving PR/SHA (else None)


# ── DR2: the missing primitive — the review-found-defect event class ──
#
# A non-escalation, human/review/failing-test-found defect has no
# durable home except an ad-hoc TODO.md line. DR2 adds ONE minimal
# event class on the existing application_log substrate (schemaless
# `data jsonb` — no table, no migration, no new daemon, no new
# write-coupling on any existing producer). These two event types are
# retention-exempt (tpcore.logging.db_handler.RETENTION_EXEMPT_EVENT_TYPES)
# so an open review defect never silently expires.

REVIEW_DEFECT_LOGGED = "REVIEW_DEFECT_LOGGED"
REVIEW_DEFECT_RESOLVED = "REVIEW_DEFECT_RESOLVED"
_DAEMON_TAG = "ops"

# Byte-for-byte the same INSERT shape as engine_ladder._emit /
# weekly_digest._emit (column order parity locked).
_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

# The review open-set, derived with the SAME anti-join open-predicate
# the engine Ladder uses (engine_ladder.py:183-191): a LOGGED is OPEN
# iff there is NO later matching RESOLVED for the same defect_ref. A
# defect closes ONLY on a durable RESOLVED event — never by omission.
# The clean defect_ref / fix linkage is read off the typed `data`
# jsonb, never regex-scraped from the message.
_REVIEW_OPEN_SQL = f"""
    SELECT lg.data->>'defect_ref' AS defect_ref,
           lg.data->>'summary'    AS summary,
           lg.data->>'lane'       AS lane,
           lg.data->>'logged_at'  AS logged_at,
           EXISTS (
             SELECT 1 FROM platform.application_log r
             WHERE r.event_type = '{REVIEW_DEFECT_RESOLVED}'
               AND (r.data->>'defect_ref') = (lg.data->>'defect_ref')
               AND r.recorded_at >= lg.recorded_at
           )                      AS is_resolved,
           (SELECT r2.data->>'pr' FROM platform.application_log r2
              WHERE r2.event_type = '{REVIEW_DEFECT_RESOLVED}'
                AND (r2.data->>'defect_ref') = (lg.data->>'defect_ref')
                AND r2.recorded_at >= lg.recorded_at
              ORDER BY r2.recorded_at DESC LIMIT 1) AS fix_ref
    FROM platform.application_log lg
    WHERE lg.event_type = '{REVIEW_DEFECT_LOGGED}'
"""


async def _emit(pool, event_type: str, message: str,
                payload: dict) -> None:
    """One application_log row via the locked INSERT — byte-for-byte
    column-order parity with engine_ladder._emit / weekly_digest._emit.
    The ONLY write surface this module owns (no new write-coupling on
    any existing producer)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, _DAEMON_TAG, uuid.uuid4(), event_type, "INFO",
            message, json.dumps(payload, default=str))


async def log_review_defect(pool, *, ref: str, summary: str,
                            lane: str | None = None) -> int:
    """Emit one REVIEW_DEFECT_LOGGED. Mirrors the engine_ladder emit
    idiom; payload schema is frozen (the TODO-parity test + the
    register both key on it)."""
    payload = {
        "schema": 1,
        "defect_ref": ref,
        "origin": "review",
        "summary": summary,
        "lane": lane,
        "pr": None,
        "logged_at": datetime.now(UTC).isoformat(),
    }
    await _emit(pool, REVIEW_DEFECT_LOGGED,
                f"review-found defect logged: {ref}", payload)
    logger.info("defect_register.review_logged", defect_ref=ref)
    return 0


async def resolve_review_defect(pool, *, ref: str, pr: str) -> int:
    """Emit one REVIEW_DEFECT_RESOLVED (durable fix linkage). The
    register's anti-join open-predicate consumes this — a defect closes
    ONLY on this event, never by omission."""
    payload = {
        "schema": 1,
        "defect_ref": ref,
        "origin": "review",
        "pr": pr,
        "resolved_at": datetime.now(UTC).isoformat(),
    }
    await _emit(pool, REVIEW_DEFECT_RESOLVED,
                f"review-found defect resolved: {ref} ({pr})", payload)
    logger.info("defect_register.review_resolved",
                defect_ref=ref, pr=pr)
    return 0


async def _review_rows(pool) -> list[dict]:
    """The review-found-defect rows (open + resolved), keyed by
    defect_ref via the anti-join open-predicate. This is the ONE DB
    query the register itself owns — it is NOT an escalation
    re-derivation (escalation state is composed verbatim from the two
    Ladder APIs and never re-queried here)."""
    async with pool.acquire() as conn:
        return list(await conn.fetch(_REVIEW_OPEN_SQL))


async def consolidated_defects(pool) -> list[DefectRow]:
    """The unified, deterministically-ordered defect view.

    Composes BOTH Ladder read APIs **verbatim** (import + call;
    reimplement neither; issue NO application_log escalation query of
    our own), maps each entry → ``DefectRow``, **joins by
    ``defect_ref`` (never sums)** so a ref present in both lanes
    collapses to ONE row, ordered deterministically by
    ``(opened_at, defect_ref)``.
    """
    engine_entries = await engine_ladder.list_undispositioned(pool)
    digest = await weekly_digest.build_weekly_digest(pool)
    # Structured surface — the clean ``ref`` is read off the struct, NOT
    # regex-scraped from the rendered display string (a digest display
    # reformat must never silently drop every data-lane defect).
    data_entries = digest.undispositioned_entries

    # Join, never sum: first-writer-wins per defect_ref. Engine entries
    # are mapped first so an engine hold_id that also appears in a
    # data-lane line keeps the (richer, structured) engine row.
    by_ref: dict[str, DefectRow] = {}

    for e in engine_entries:
        ref = e["hold_id"]
        # engine-first-wins on a defect_ref collision is DELIBERATE: the
        # engine row is the richer/structured one, so a later data-lane
        # entry with the same ref is dropped as a duplicate (by design,
        # not an oversight — see the data-lane loop's collision branch).
        if ref is None or ref in by_ref:
            continue
        pol = e.get("policy_default")
        policy = (f"{pol} ({e.get('policy_rationale')})"
                  if pol else None)
        by_ref[ref] = DefectRow(
            defect_ref=ref,
            origin="escalation",
            lane="engine",
            summary=(f"{e.get('engine')}/{e.get('failure_class')} "
                     f"[{e.get('shape')}] {e.get('reason')}"),
            # Present in list_undispositioned ⇒ open by that Ladder's
            # own anti-join predicate. Not re-derived here.
            state="open",
            opened_at=e["recorded_at"],
            policy=policy,
            fix_ref=None,
        )

    for ent in data_entries:
        ref = ent.ref
        if ref is None:
            continue  # no keyable ref → drop (no phantom)
        if ref in by_ref:
            # Already-seen ref → JOIN (collapse to the existing row;
            # never a second row). Engine-first-wins is deliberate (see
            # engine loop). DR1-deferred nit closed: the silent collapse
            # is now observable — a structured counter at the join site
            # so a data row dropped by an engine-ref collision is never
            # invisible.
            logger.info("defect_register.ref_collision_dropped",
                        defect_ref=ref, kept_origin="escalation",
                        dropped_lane="data")
            continue
        by_ref[ref] = DefectRow(
            defect_ref=ref,
            origin="escalation",
            lane="data",
            summary=ent.rendered,
            # Present in build_weekly_digest().undispositioned_entries ⇒
            # open by the data-lane Ladder's anti-join predicate.
            state="open",
            # The struct's typed recorded_at (UTC-aware, same as the
            # engine Ladder's) — no display-string date parse.
            opened_at=ent.recorded_at,
            policy=ent.policy,  # the inline disposition-policy label
            fix_ref=None,
        )

    # DR2: fold in the review-found-defect class. open = a LOGGED with
    # no later matching RESOLVED (the SAME anti-join open-predicate as
    # engine_ladder._DISPOSITIONED_EVENT, py:183-191); a resolved one →
    # state="fixed" + fix_ref. Same join, never sum: a review defect
    # whose defect_ref == an escalation hold_id collapses to ONE row.
    for rv in await _review_rows(pool):
        ref = rv["defect_ref"]
        if ref is None:
            continue
        if ref in by_ref:
            # Collision with an escalation row → JOIN (drop the dup
            # review row; escalation wins). Observable, never silent.
            logger.info("defect_register.ref_collision_dropped",
                        defect_ref=ref, kept_origin="escalation",
                        dropped_lane="review")
            continue
        logged_at = rv.get("logged_at")
        opened_at = (datetime.fromisoformat(logged_at)
                     if logged_at else datetime.now(UTC))
        resolved = bool(rv["is_resolved"])
        by_ref[ref] = DefectRow(
            defect_ref=ref,
            origin="review",
            lane=rv.get("lane") or "review",
            summary=rv.get("summary") or "",
            # Closes ONLY on a durable RESOLVED — never by omission.
            state="fixed" if resolved else "open",
            opened_at=opened_at,
            policy=None,
            fix_ref=rv.get("fix_ref") if resolved else None,
        )

    return sorted(by_ref.values(),
                  key=lambda r: (r.opened_at, r.defect_ref))


def _fmt(rows: list[DefectRow]) -> str:
    head = (f"CONSOLIDATED DEFECT REGISTER ({len(rows)}) — "
            "escalation + review-found origins")
    if not rows:
        return head
    lines = [head]
    for r in rows:
        lines.append(
            f"  [{r.lane}] ref={r.defect_ref} state={r.state} "
            f"opened={r.opened_at} origin={r.origin} "
            f"fix={r.fix_ref or '-'} :: {r.summary}"
            + (f" -> policy={r.policy}" if r.policy else ""))
    return "\n".join(lines)


async def _amain(argv: list[str]) -> int:
    dsn = (os.environ.get("DATABASE_URL")
           or os.environ.get("DATABASE_URL_IPV4"))
    if not dsn:
        logger.error("defect_register.no_dsn")
        return 1
    p = argparse.ArgumentParser(prog="python -m ops.defect_register")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    pl = sub.add_parser("log")
    pl.add_argument("--ref", required=True)
    pl.add_argument("--summary", required=True)
    pl.add_argument("--lane", default=None)
    pr = sub.add_parser("resolve")
    pr.add_argument("--ref", required=True)
    pr.add_argument("--pr", required=True)
    args = p.parse_args(argv or ["list"])
    pool = await build_asyncpg_pool(dsn)
    try:
        if args.cmd == "list":
            rows = await consolidated_defects(pool)
            print(_fmt(rows))
            return 0
        if args.cmd == "log":
            return await log_review_defect(
                pool, ref=args.ref, summary=args.summary,
                lane=args.lane)
        if args.cmd == "resolve":
            return await resolve_review_defect(
                pool, ref=args.ref, pr=args.pr)
        p.print_usage(sys.stderr)
        return 2
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
