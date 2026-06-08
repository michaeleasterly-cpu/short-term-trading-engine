"""Staging-spine completeness gate — the P1-P5 make-it-work gate (Phase A).

Plan: ``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md``
A5. Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §0 / §5.

This gate runs against the **staged** clean-slate spine
(``ticker_classifications`` + ``ticker_history`` in a staging schema, default
``platform_stage_spine``) and the LIVE ``platform.prices_daily`` bars. It is
the proof, BEFORE any destructive live change, that the rebuilt spine resolves
every priced ticker — "the next run works" by construction.

It is distinct from ``tpcore.identity.identity_gate`` (which guards the LIVE
``platform.*`` substrate post-build): this gate is parameterized on the spine
schema so it can run against staging tables that the live FK / EXCLUDE
constraints have not yet been applied to, and it adds the P3 price-coverage
dry-run that the live identity_gate does not (the live gate's probe #2 only
catches the inverse — bars EARLIER than lifetime_start — once bars are already
attributed; P3 here proves coverage of both the min AND max bar against the
EXACT live bars the re-ingest will write).

Probes (a clean staged spine returns 0 for every one):

  * **P1** — every ticker-bearing classification has ≥1 ``ticker_history``
    window (no windowless classification).
  * **P2** — no cross-entity same-ticker window overlap (the disjointness
    guarantee for G3 reuse; the half-open ``[valid_from, valid_to)`` semantics
    treat a contiguous handoff ``valid_to == next valid_from`` as NON-overlap).
  * **P3 (critical)** — every symbol with bars in ``platform.prices_daily``
    has staged window coverage spanning BOTH its min AND its max bar date. A
    symbol is covered iff there EXISTS a staged window for that literal symbol
    string with ``valid_from <= min_bar`` AND
    ``(valid_to IS NULL OR valid_to > max_bar)``. (Per-symbol single-window
    coverage is the resolver's model — the survivorship-free snapshot keys all
    of a symbol's bars under that one literal string, so one window must span
    the whole observed span.) Violators are reported precisely.
  * **P4** — no synthetic Jan-1 ``lifetime_start`` when real-day corroboration
    (a price bar OR a non-Jan-1 FPFD) is available. A genuine Jan-1 listing
    (rare but real — some IPOs/effective dates ARE Jan-1) is allowed only when
    no earlier real-day evidence exists.
  * **P5** — staged spine internally consistent: no duplicate TKR-14 id, no
    ``ticker_history`` row orphaned from ``ticker_classifications``, no
    classification with NULL ``current_ticker`` (the reuse-build keys on it),
    no row with ``lifetime_end <= lifetime_start`` (date-order), and no
    ``lifetime_start = '1900-01-01'`` sentinel.

The orchestrator runs this in BLOCKING mode (``raise_on_fail=True``) after the
staging build; a non-zero P1/P2/P3 means the mint is wrong — fix the build, do
NOT lower the gate.
"""
from __future__ import annotations

from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class _Conn(Protocol):
    async def fetchval(self, sql: str) -> int | None: ...
    async def fetch(self, sql: str) -> list: ...


class StagingGateResult(BaseModel):
    """The gate verdict + per-probe violation counts (only non-zero probes
    appear in ``violations``) + a small sample of P3 violators for the report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    violations: dict[str, int]
    p3_violator_sample: list[dict] = []


def _span_cte(prices_table: str, span_table: str | None) -> str:
    """The per-symbol ``(ticker, min_date, max_date)`` span source.

    When ``span_table`` is supplied (a pre-materialized
    ``platform.tmp_price_bar_span``-shaped table with ``ticker / min_date /
    max_date`` columns), the gate reads it directly — making P3 instant
    instead of re-aggregating the 21M-row prices table on every probe. When
    None, the gate self-aggregates ``prices_table`` (self-contained default)."""
    if span_table:
        return f"SELECT ticker, min_date, max_date FROM {span_table}"
    return (
        f"SELECT ticker, min(date) AS min_date, max(date) AS max_date "
        f"FROM {prices_table} GROUP BY ticker"
    )


def _probes(
    spine_schema: str, prices_table: str, span_table: str | None
) -> tuple[tuple[str, str], ...]:
    """Build the parameterized probe SQL for a given staging schema.

    ``spine_schema`` is trusted (orchestrator-supplied identifier, not user
    input). The probes COUNT offending rows; a clean spine returns 0.
    """
    s = spine_schema
    p = prices_table
    span = _span_cte(prices_table, span_table)

    p1 = f"""
        SELECT count(*) FROM {s}.ticker_classifications tc
        WHERE tc.current_ticker IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM {s}.ticker_history th
            WHERE th.classification_id = tc.id)
    """

    p2 = f"""
        SELECT count(*) FROM {s}.ticker_history th1
        JOIN {s}.ticker_history th2
          ON th1.ticker = th2.ticker
         AND th1.classification_id <> th2.classification_id
         AND daterange(th1.valid_from, COALESCE(th1.valid_to, 'infinity'::date), '[)')
           && daterange(th2.valid_from, COALESCE(th2.valid_to, 'infinity'::date), '[)')
    """

    # P3 — every priced symbol has single-window coverage of [min,max].
    p3 = f"""
        WITH span AS ({span})
        SELECT count(*) FROM span sp
        WHERE NOT EXISTS (
            SELECT 1 FROM {s}.ticker_history th
            WHERE th.ticker = sp.ticker
              AND th.valid_from <= sp.min_date
              AND (th.valid_to IS NULL OR th.valid_to > sp.max_date))
    """

    # P4 — Jan-1 lifetime_start with real-day corroboration available.
    # Corroboration = a price bar for the symbol OR a non-Jan-1 FPFD on the
    # classification. A Jan-1 start with NO such corroboration is allowed
    # (genuine, no earlier evidence).
    p4 = f"""
        SELECT count(*) FROM {s}.ticker_classifications tc
        WHERE EXTRACT(month FROM tc.lifetime_start) = 1
          AND EXTRACT(day FROM tc.lifetime_start) = 1
          AND (
            EXISTS (SELECT 1 FROM {p} pd WHERE pd.ticker = tc.ticker)
            OR (tc.first_public_filing_date IS NOT NULL
                AND NOT (EXTRACT(month FROM tc.first_public_filing_date) = 1
                         AND EXTRACT(day FROM tc.first_public_filing_date) = 1))
          )
    """

    p5_dup_id = f"""
        SELECT count(*) FROM (
            SELECT id FROM {s}.ticker_classifications
            GROUP BY id HAVING count(*) > 1) d
    """
    p5_orphan_th = f"""
        SELECT count(*) FROM {s}.ticker_history th
        WHERE NOT EXISTS (
            SELECT 1 FROM {s}.ticker_classifications tc WHERE tc.id = th.classification_id)
    """
    p5_null_current = f"""
        SELECT count(*) FROM {s}.ticker_classifications WHERE current_ticker IS NULL
    """
    p5_bad_order = f"""
        SELECT count(*) FROM {s}.ticker_classifications
        WHERE lifetime_end IS NOT NULL AND lifetime_end <= lifetime_start
    """
    p5_sentinel = f"""
        SELECT count(*) FROM {s}.ticker_classifications
        WHERE lifetime_start = DATE '1900-01-01'
    """

    return (
        ("P1_windowless_classification", p1),
        ("P2_cross_entity_overlap", p2),
        ("P3_priced_uncovered", p3),
        ("P4_synthetic_jan1_with_corroboration", p4),
        ("P5_duplicate_tkr14_id", p5_dup_id),
        ("P5_orphan_ticker_history", p5_orphan_th),
        ("P5_null_current_ticker", p5_null_current),
        ("P5_bad_lifetime_order", p5_bad_order),
        ("P5_sentinel_lifetime_start", p5_sentinel),
    )


def _p3_violator_sample_sql(
    spine_schema: str, prices_table: str, span_table: str | None
) -> str:
    s = spine_schema
    if span_table:
        span = (
            f"SELECT ticker, min_date, max_date, n_bars FROM {span_table}"
        )
    else:
        span = (
            f"SELECT ticker, min(date) AS min_date, max(date) AS max_date, "
            f"count(*) AS n_bars FROM {prices_table} GROUP BY ticker"
        )
    return f"""
        WITH span AS ({span})
        SELECT sp.ticker, sp.min_date, sp.max_date, sp.n_bars FROM span sp
        WHERE NOT EXISTS (
            SELECT 1 FROM {s}.ticker_history th
            WHERE th.ticker = sp.ticker
              AND th.valid_from <= sp.min_date
              AND (th.valid_to IS NULL OR th.valid_to > sp.max_date))
        ORDER BY sp.ticker
        LIMIT 100
    """


async def evaluate_staging_gate(
    conn: _Conn,
    *,
    spine_schema: str = "platform_stage_spine",
    prices_table: str = "platform.prices_daily",
    span_table: str | None = None,
    raise_on_fail: bool = False,
) -> StagingGateResult:
    """Run the P1-P5 staging-spine completeness probes.

    Read-only. Returns ``StagingGateResult`` with the non-zero probe counts in
    ``violations`` (empty ⇒ green) + up to 100 P3 violators for the report.
    When ``raise_on_fail`` is True and any probe is non-zero, raises
    ``RuntimeError`` (the build is wrong — fix the mint, never lower the gate).

    ``span_table`` (optional): a pre-materialized ``(ticker, min_date,
    max_date, n_bars)`` table (e.g. ``platform.tmp_price_bar_span``) the P3
    probes read instead of re-aggregating the full ``prices_table`` — makes the
    gate instant against the 21M-row live prices table. When None, P3
    self-aggregates ``prices_table`` (self-contained default).
    """
    violations: dict[str, int] = {}
    for key, sql in _probes(spine_schema, prices_table, span_table):
        count = await conn.fetchval(sql)
        n = int(count or 0)
        if n > 0:
            violations[key] = n

    p3_sample: list[dict] = []
    if "P3_priced_uncovered" in violations:
        rows = await conn.fetch(
            _p3_violator_sample_sql(spine_schema, prices_table, span_table)
        )
        p3_sample = [
            {
                "ticker": r["ticker"],
                "min_date": r["min_date"].isoformat(),
                "max_date": r["max_date"].isoformat(),
                "n_bars": int(r["n_bars"]),
            }
            for r in rows
        ]

    passed = not violations
    logger.info(
        "staging_gate.evaluated",
        passed=passed,
        violations=violations,
        spine_schema=spine_schema,
    )
    result = StagingGateResult(
        passed=passed, violations=violations, p3_violator_sample=p3_sample
    )
    if raise_on_fail and not passed:
        raise RuntimeError(
            "staging gate FAILED — the staged clean-slate spine does not "
            f"resolve every priced ticker: {violations}. Fix the mint; do NOT "
            "lower the gate (the destructive cut is blocked on a green gate)."
        )
    return result


__all__ = [
    "StagingGateResult",
    "evaluate_staging_gate",
]
