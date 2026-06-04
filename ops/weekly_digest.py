"""Weekly state-comprehension digest + acknowledgement + auto-de-escalate.

The operator's stated goal is to interact with the system as little as
possible. The expert correction: minimizing *interaction* is the wrong
objective — minimize *opportunity for irreversible harm* while keeping
the operator's mental model warm enough to intervene in the rare
crisis. A fully-autonomous data layer the operator never looks at is
one whose state they cannot model when something is silently wrong
(automation complacency / deskilling), and "config-reversible" is not
"consequence-reversible" — money moved on quietly-degraded data is
already irreversible.

So this is the non-skippable comprehension floor:

* **emit** — once per ISO week, the system PUSHES a one-page digest
  (to ``platform.application_log`` as ``WEEKLY_DIGEST`` + best-effort
  local notification): every provider cutover, every self-heal that
  fired and *what it changed*, every gate that passed *within margin
  of failing*, and ONE adversarially-surfaced "most likely silently
  wrong right now" item (the verify-the-verifier slot).
* **ack** — a 30-second binary operator acknowledgement
  (``WEEKLY_DIGEST_ACK``). Read-then-ack: zero fat-finger surface.
* **live_clearance** — if the latest weekly digest has gone
  unacknowledged for ≥ ``DEESCALATE_AFTER_WEEKS`` ISO weeks, live
  trading is auto-de-escalated: the data layer withholds the
  live-clear. An ack restores it. This is the teeth — the digest is
  not theatre.

Deterministic, idempotent (one digest per ISO week; dedup on the DB
row), Railway-portable (env + pool; the bus is the durable channel,
the macOS notification is best-effort/local only).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.ladder import policy_for

logger = structlog.get_logger(__name__)

DIGEST_EVENT = "WEEKLY_DIGEST"
ACK_EVENT = "WEEKLY_DIGEST_ACK"
DEESCALATED_EVENT = "LIVE_DEESCALATED"
DAEMON_TAG = "weekly-digest"

# Two consecutive missed weekly acks → auto-de-escalate. One miss is a
# warning (life happens); two is "the operator is structurally out of
# the loop" — the condition this whole mechanism exists to prevent.
DEESCALATE_AFTER_WEEKS = 2
# A gate that passed but whose confidence is within this of the 1.0
# pass line is a "near-miss" — surfaced so a human pressure-tests it
# before it silently tips red.
NEAR_MISS_MARGIN = 0.05

_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""


@dataclass(frozen=True)
class UndispositionedEntry:
    """The STRUCTURED open-undispositioned data-lane escalation —
    the machine-consumable counterpart of the human ``undispositioned``
    line. Both the rendered string AND this struct derive from the SAME
    ``open_esc`` row inside ``build_weekly_digest`` (single source; no
    duplicated logic). Consumers that need a clean ``ref`` (e.g. the
    consolidated defect register) read THIS — never regex-scrape the
    display string (a display-format change would silently break them;
    the doctrine is "consume the structured SoT datum")."""

    ref: str
    etype: str
    recorded_at: datetime
    message: str
    policy: str  # the inline disposition-policy label (== _disposition_label)
    rendered: str  # the exact human line emitted in ``undispositioned``


@dataclass(frozen=True)
class WeeklyDigest:
    iso_week: str          # e.g. "2026-W20" — the idempotency key
    period_start: datetime
    period_end: datetime
    cutovers: list[str]
    self_heals: list[str]
    near_miss_gates: list[str]
    undispositioned: list[str]
    # Additive structured surface (pure add — ``undispositioned`` above
    # and every existing consumer are byte-unchanged). Same length /
    # order as ``undispositioned``; entry i is the struct for line i.
    undispositioned_entries: list[UndispositionedEntry]
    most_likely_wrong: str
    generated_at: datetime

    def render(self) -> str:
        def _section(title: str, items: list[str]) -> list[str]:
            body = [f"  - {x}" for x in items] if items else ["  (none)"]
            return [title, *body, ""]

        L = [
            f"WEEKLY DATA-LAYER DIGEST — {self.iso_week}",
            f"  window: {self.period_start:%Y-%m-%d} → {self.period_end:%Y-%m-%d}",
            "",
            *_section(f"PROVIDER CUTOVERS ({len(self.cutovers)}):", self.cutovers),
            *_section(
                f"SELF-HEAL FIRINGS ({len(self.self_heals)}):", self.self_heals
            ),
            *_section(
                f"GATES THAT PASSED WITHIN {NEAR_MISS_MARGIN:.0%} OF FAILING "
                f"({len(self.near_miss_gates)}):",
                self.near_miss_gates,
            ),
            *_section(
                f"UNDISPOSITIONED DATA-LANE ESCALATIONS "
                f"({len(self.undispositioned)}) — rung-3: each MUST be "
                f"converted | structural | removed:",
                self.undispositioned,
            ),
            "MOST LIKELY SILENTLY WRONG RIGHT NOW:",
            f"  → {self.most_likely_wrong}",
            "",
            "Acknowledge within 30s to keep live trading enabled:",
            "  python -m ops.weekly_digest ack",
            f"(unacked ≥ {DEESCALATE_AFTER_WEEKS} weeks ⇒ auto-de-escalate "
            f"to no-live-trading until acked)",
        ]
        return "\n".join(L)


def _disposition_label(etype: str) -> str:
    """The disposition policy for an escalation event class, rendered
    inline so the operator sees WHAT terminates it without a lookup
    (spec §4.3). A future etype with no event:<etype> policy degrades
    to UNREGISTERED rather than crashing the digest (the clockwork
    drift-test is the real guard; this is graceful display)."""
    try:
        p = policy_for(f"event:{etype}")
    except KeyError:
        return "policy:UNREGISTERED (add event: disposition)"
    base = f"policy:{p.disposition.value}"
    return f"{base} — {p.reason}" if p.reason else base


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


async def _q(pool: Any, sql: str, *args: Any) -> list[dict]:
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *args)]


async def build_weekly_digest(pool: Any, now: datetime | None = None) -> WeeklyDigest:
    """Assemble the digest from the trailing 7 days of the bus +
    the latest validation rows. Pure read."""
    now = now or datetime.now(UTC)
    start = now - timedelta(days=7)

    cut = await _q(
        pool,
        """SELECT recorded_at, message, data FROM platform.application_log
           WHERE event_type = 'PROVIDER_CUTOVER' AND recorded_at > $1
           ORDER BY recorded_at""",
        start,
    )
    cutovers = [f"{r['recorded_at']:%Y-%m-%d} {r['message']}" for r in cut]

    heals = await _q(
        pool,
        """SELECT recorded_at, event_type, message FROM platform.application_log
           WHERE recorded_at > $1
             AND event_type IN ('DATA_REPAIR_COMPLETE','DATA_REPAIR_ESCALATED',
                                 'INGESTION_FAILED')
           ORDER BY recorded_at""",
        start,
    )
    self_heals = [
        f"{r['recorded_at']:%Y-%m-%d} [{r['event_type']}] {r['message']}"
        for r in heals
    ]

    nearmiss = await _q(
        pool,
        """WITH latest AS (
               SELECT source, MAX(timestamp) t FROM platform.data_quality_log
               WHERE kind = 'validation' AND source LIKE 'validation.%'
               GROUP BY source)
           SELECT q.source, q.confidence
           FROM platform.data_quality_log q JOIN latest l
             ON l.source=q.source AND l.t=q.timestamp
           WHERE q.stale = false AND q.confidence IS NOT NULL
             AND q.confidence < 1.0 AND q.confidence >= $1
           ORDER BY q.confidence""",
        1.0 - NEAR_MISS_MARGIN,
    )
    near_miss_gates = [
        f"{r['source']} (confidence {r['confidence']:.3f} — within "
        f"{1.0 - r['confidence']:.3f} of failing)"
        for r in nearmiss
    ]

    # Adversarial "most likely silently wrong": the tightest near-miss
    # if any; else the feed with the most self-heal firings this week
    # (repeated healing = an unstable feed); else honest "nothing
    # obvious — which is itself the thing to distrust".
    if near_miss_gates:
        mlw = (
            f"{near_miss_gates[0]} — closest to silently tipping red; "
            f"pressure-test this feed's data, not just its green flag."
        )
    elif self_heals:
        from collections import Counter
        feeds = Counter(
            h.split("] ", 1)[-1].split(":")[0] for h in self_heals
        )
        worst, n = feeds.most_common(1)[0]
        mlw = (
            f"{worst!r} self-healed {n}× this week — repeated bounded "
            f"repair can mask a feed that is structurally degrading; "
            f"verify it independently, don't trust the green."
        )
    else:
        mlw = (
            "No near-miss gate and no self-heal fired — distrust THIS: "
            "a perfectly quiet week is also what a stuck/false-green "
            "validation layer looks like. Spot-check one feed by hand."
        )

    open_esc = await _q(
        pool,
        """-- OPEN_ESCALATIONS
        WITH esc AS (
          SELECT e.data->>'request_id' AS ref,
                 'DATA_REPAIR_ESCALATED' AS etype,
                 e.recorded_at, e.message
          FROM platform.application_log e
          WHERE e.event_type = 'DATA_REPAIR_ESCALATED'
          UNION ALL
          SELECT e.data->>'hold_id' AS ref,
                 'DATA_SOURCE_ESCALATED' AS etype,
                 e.recorded_at, e.message
          FROM platform.application_log e
          WHERE e.event_type = 'DATA_SOURCE_ESCALATED'
        )
        SELECT ref, etype, recorded_at, message FROM esc x
        WHERE x.ref IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.event_type IN
                  ('DATA_REPAIR_COMPLETE','DATA_SOURCE_CLEARED')
              AND (t.data->>'request_id' = x.ref
                   OR t.data->>'hold_id' = x.ref)
              AND t.recorded_at > x.recorded_at)
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log dp
            WHERE dp.event_type = 'DATA_ESCALATION_DISPOSITIONED'
              AND dp.data->>'ref' = x.ref)
          AND x.recorded_at < $1
        ORDER BY x.recorded_at
        """,
        start,
    )
    # 2026-05-22 — the LT-P3 §5 LLM-triage advisory-proposal annotation
    # has been REMOVED. Operator directive ("we aren't going to use the
    # llm triage... take it out") deleted ``ops.llm_data_triage`` and
    # the ``DATA_LLM_TRIAGE_PROPOSAL`` event class is no longer emitted.
    # The digest line is now purely deterministic with no LLM suffix.

    # Single source: build the structured entry per open_esc row and
    # derive the human line from it. The rendered string and the struct
    # cannot disagree (no duplicated formatting / no regex round-trip).
    undispositioned_entries: list[UndispositionedEntry] = []
    for r in open_esc:
        policy = _disposition_label(r["etype"])
        rendered = (
            f"{r['recorded_at']:%Y-%m-%d} [{r['etype']}] ref={r['ref']} "
            f"{r['message']} | {policy}"
        )
        undispositioned_entries.append(
            UndispositionedEntry(
                ref=r["ref"], etype=r["etype"],
                recorded_at=r["recorded_at"], message=r["message"],
                policy=policy, rendered=rendered,
            )
        )
    undispositioned = [e.rendered for e in undispositioned_entries]

    return WeeklyDigest(
        iso_week=_iso_week(now), period_start=start, period_end=now,
        cutovers=cutovers, self_heals=self_heals,
        near_miss_gates=near_miss_gates, undispositioned=undispositioned,
        undispositioned_entries=undispositioned_entries,
        most_likely_wrong=mlw, generated_at=now,
    )


async def _emit(pool: Any, event: str, message: str, data: dict,
                *, severity: str = "INFO") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, DAEMON_TAG, uuid.uuid4(), event, severity,
            message, json.dumps(data, default=str),
        )


def _notify_local(text: str) -> None:
    """Best-effort macOS notification (local only; no-ops elsewhere)."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "weekly data digest — ack required" '
             'with title "STE"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - notification is non-critical
        logger.info("weekly_digest.notify_skipped", error=str(exc))
    del text


async def emit_digest(pool: Any, now: datetime | None = None) -> bool:
    """Build + push this ISO week's digest. Idempotent: skip if one
    already exists for the week (the DB row is the dedup state).
    Returns True iff a new digest was emitted."""
    now = now or datetime.now(UTC)
    wk = _iso_week(now)
    existing = await _q(
        pool,
        """SELECT 1 FROM platform.application_log
           WHERE event_type=$1 AND data->>'iso_week'=$2 LIMIT 1""",
        DIGEST_EVENT, wk,
    )
    if existing:
        logger.info("weekly_digest.already_emitted", iso_week=wk)
        return False
    d = await build_weekly_digest(pool, now)
    rendered = d.render()
    await _emit(
        pool, DIGEST_EVENT, f"weekly data-layer digest {wk}",
        {
            "iso_week": wk,
            "cutovers": d.cutovers,
            "self_heals": d.self_heals,
            "near_miss_gates": d.near_miss_gates,
            "most_likely_wrong": d.most_likely_wrong,
            "rendered": rendered,
        },
        severity="WARNING",  # WARNING so it surfaces in dashboards
    )
    _notify_local(rendered)
    logger.info("weekly_digest.emitted", iso_week=wk)
    print(rendered)
    return True


async def ack_digest(pool: Any, now: datetime | None = None) -> str:
    """Acknowledge the most recent weekly digest. Idempotent per
    ISO week. Returns the acked iso_week (or '' if there is no digest
    to ack)."""
    now = now or datetime.now(UTC)
    latest = await _q(
        pool,
        """SELECT data->>'iso_week' AS wk FROM platform.application_log
           WHERE event_type=$1 ORDER BY recorded_at DESC LIMIT 1""",
        DIGEST_EVENT,
    )
    if not latest or not latest[0]["wk"]:
        logger.info("weekly_digest.nothing_to_ack")
        return ""
    wk = latest[0]["wk"]
    already = await _q(
        pool,
        """SELECT 1 FROM platform.application_log
           WHERE event_type=$1 AND data->>'iso_week'=$2 LIMIT 1""",
        ACK_EVENT, wk,
    )
    if already:
        logger.info("weekly_digest.already_acked", iso_week=wk)
        return wk
    await _emit(pool, ACK_EVENT, f"weekly digest {wk} acknowledged",
                {"iso_week": wk})
    logger.info("weekly_digest.acked", iso_week=wk)
    return wk


_VALID_DISPOSITIONS = {"converted", "structural", "removed"}


async def disposition_escalation(
    pool: Any, ref: str, disposition: str, note: str
) -> int:
    """Record an operator disposition for an open escalation instance.
    Mirrors ack_digest's emit pattern. 0 ok; 1 on a bad disposition."""
    if disposition not in _VALID_DISPOSITIONS:
        logger.error("weekly_digest.bad_disposition", value=disposition)
        return 1
    await _emit(
        pool, "DATA_ESCALATION_DISPOSITIONED",
        f"escalation {ref} dispositioned: {disposition}",
        {"schema": 1, "ref": ref, "disposition": disposition,
         "note": note},
    )
    logger.info("weekly_digest.dispositioned", ref=ref,
                disposition=disposition)
    return 0


async def live_clearance(
    pool: Any, now: datetime | None = None
) -> tuple[bool, str]:
    """The teeth. Count consecutive most-recent WEEKLY_DIGEST weeks
    with NO matching ACK. ≥ DEESCALATE_AFTER_WEEKS ⇒ NOT cleared
    (withhold the live-clear; an ack restores it).

    Returns ``(cleared, reason)``. The data-ops all-clear / engine
    dispatch consults this — documented handshake, not wired across
    the lane boundary here.
    """
    now = now or datetime.now(UTC)
    digests = await _q(
        pool,
        """SELECT DISTINCT data->>'iso_week' AS wk, MAX(recorded_at) r
           FROM platform.application_log WHERE event_type=$1
           GROUP BY 1 ORDER BY r DESC LIMIT 8""",
        DIGEST_EVENT,
    )
    if not digests:
        return True, "no weekly digest emitted yet (bootstrap)"
    acked = {
        r["wk"] for r in await _q(
            pool,
            "SELECT DISTINCT data->>'iso_week' AS wk FROM "
            "platform.application_log WHERE event_type=$1", ACK_EVENT,
        )
    }
    consecutive_unacked = 0
    for d in digests:  # most-recent first
        if d["wk"] in acked:
            break
        consecutive_unacked += 1
    if consecutive_unacked >= DEESCALATE_AFTER_WEEKS:
        return False, (
            f"weekly digest unacknowledged {consecutive_unacked} "
            f"consecutive weeks — live trading auto-de-escalated. "
            f"Run `python -m ops.weekly_digest ack` to restore."
        )
    if consecutive_unacked == 1:
        return True, (
            "1 weekly digest unacknowledged (warning — one more miss "
            "auto-de-escalates live trading)"
        )
    return True, "weekly digest current"


async def _amain(argv: list[str]) -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("weekly_digest.no_dsn")
        return 1
    cmd = argv[0] if argv else "status"
    pool = await build_asyncpg_pool(dsn)
    try:
        if cmd == "emit":
            await emit_digest(pool)
            return 0
        if cmd == "ack":
            wk = await ack_digest(pool)
            print(f"acknowledged: {wk or '(nothing to ack)'}")
            return 0
        if cmd == "status":
            cleared, reason = await live_clearance(pool)
            print(f"live_cleared={cleared}  — {reason}")
            return 0 if cleared else 2
        if cmd == "disposition":
            if len(argv) < 3:
                print(
                    "usage: python -m ops.weekly_digest disposition "
                    "<ref> <converted|structural|removed> [note ...]",
                    file=sys.stderr,
                )
                return 2
            ref = argv[1]
            disp = argv[2]
            note_words = argv[3:]
            rc = await disposition_escalation(pool, ref, disp,
                                              " ".join(note_words))
            return rc
        print(f"usage: python -m ops.weekly_digest {{emit|ack|status|disposition}}; "
              f"got {cmd!r}", file=sys.stderr)
        return 2
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover
    main()
