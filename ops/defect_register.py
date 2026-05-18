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

Phase DR1 is DARK: escalation-origin rows only (review/todo origins +
the ``REVIEW_DEFECT_LOGGED`` event class are DR2), ``fix_ref`` is
always ``None``, no caller is wired, no new event class is added.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime
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
    origin: Literal["escalation"]
    lane: str  # "engine" | "data"
    summary: str
    state: str  # "open" | "dispositioned-structural-parked"
    opened_at: datetime
    policy: str | None  # the advisory disposition/policy the Ladder attaches
    fix_ref: str | None  # always None in DR1 (DR2 introduces fix linkage)


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
        if ref is None or ref in by_ref:
            # No keyable ref → drop (no phantom). Already-seen ref →
            # JOIN (collapse to the existing row; never a second row).
            # Engine-first-wins here is deliberate (see engine loop).
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

    return sorted(by_ref.values(),
                  key=lambda r: (r.opened_at, r.defect_ref))


def _fmt(rows: list[DefectRow]) -> str:
    head = (f"CONSOLIDATED DEFECT REGISTER ({len(rows)}) — "
            "escalation-origin (DR1; review/todo origins are DR2)")
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
    args = p.parse_args(argv or ["list"])
    pool = await build_asyncpg_pool(dsn)
    try:
        if args.cmd == "list":
            rows = await consolidated_defects(pool)
            print(_fmt(rows))
            return 0
        p.print_usage(sys.stderr)
        return 2
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
