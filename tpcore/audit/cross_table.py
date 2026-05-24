"""Structured cross-table referential audit — the single SoT.

Replaces the print-only inline ``q()`` calls in
``scripts/audit_all_tables.py`` with a declared list of checks that
ALSO persist to ``platform.data_quality_log`` (so the auditheal loop
can detect reds), reusing ``audit_data_pipeline._persist``'s exact
severity convention. The stdout roll-up is preserved by the thin
script caller; the informational ``dump`` sections (risk_state /
open_orders) stay in the script.

Convergence contract: a check whose violation has a proven canonical
remediation (today: the two ``tradier_options_chains`` checks fixed by
``cross_ref_cleanup``) MUST use the exact predicate that stage deletes,
or remediate→re-audit can never converge. The orphan check therefore
uses ``NOT EXISTS … prices_daily_tickers`` — identical to
``_stage_cross_ref_cleanup``'s delete — not a ``prices_daily`` join.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CrossTableCheck(BaseModel):
    """One declared cross-table violation check. ``sql`` MUST return a
    single integer violation count and MUST embed a ``/*<table>/<check_name>*/``
    marker (greppable; keeps the SQL self-identifying)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    check_name: str
    sql: str
    kind: Literal["violation_count"] = "violation_count"

    @property
    def key(self) -> str:
        return f"{self.table}/{self.check_name}"


@dataclass(frozen=True)
class CrossTableFinding:
    table: str
    check_name: str
    count: int
    severity: Literal["OK", "FAIL"]

    @property
    def source_key(self) -> str:
        return f"cross_table_audit.{self.table}.{self.check_name}"


CROSS_TABLE_CHECKS: tuple[CrossTableCheck, ...] = (
    CrossTableCheck(table="earnings_events", check_name="null_ticker",
        sql="SELECT COUNT(*) /*earnings_events/null_ticker*/ FROM platform.earnings_events WHERE ticker IS NULL"),
    CrossTableCheck(table="earnings_events", check_name="null_event_date",
        sql="SELECT COUNT(*) /*earnings_events/null_event_date*/ FROM platform.earnings_events WHERE event_date IS NULL"),
    CrossTableCheck(table="earnings_events", check_name="event_date_far_future",
        sql="SELECT COUNT(*) /*earnings_events/event_date_far_future*/ FROM platform.earnings_events WHERE event_date > CURRENT_DATE + INTERVAL '365 days'"),
    CrossTableCheck(table="earnings_events", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*earnings_events/orphan_no_prices*/ FROM platform.earnings_events ce LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ce.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="liquidity_tiers", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*liquidity_tiers/orphan_no_prices*/ FROM platform.liquidity_tiers lt LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = lt.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="liquidity_tiers", check_name="stale_30d",
        sql="SELECT COUNT(*) /*liquidity_tiers/stale_30d*/ FROM platform.liquidity_tiers WHERE last_updated < now() - INTERVAL '30 days'"),
    CrossTableCheck(table="liquidity_tiers", check_name="negative_median_spread",
        sql="SELECT COUNT(*) /*liquidity_tiers/negative_median_spread*/ FROM platform.liquidity_tiers WHERE median_spread_pct < 0"),
    CrossTableCheck(table="liquidity_tiers", check_name="negative_p95_spread",
        sql="SELECT COUNT(*) /*liquidity_tiers/negative_p95_spread*/ FROM platform.liquidity_tiers WHERE p95_spread_pct < 0"),
    CrossTableCheck(table="liquidity_tiers", check_name="nonpositive_observations",
        sql="SELECT COUNT(*) /*liquidity_tiers/nonpositive_observations*/ FROM platform.liquidity_tiers WHERE observations <= 0"),
    CrossTableCheck(table="universe_candidates", check_name="null_engine",
        sql="SELECT COUNT(*) /*universe_candidates/null_engine*/ FROM platform.universe_candidates WHERE engine IS NULL"),
    CrossTableCheck(table="universe_candidates", check_name="as_of_date_future",
        sql="SELECT COUNT(*) /*universe_candidates/as_of_date_future*/ FROM platform.universe_candidates WHERE as_of_date > CURRENT_DATE"),
    CrossTableCheck(table="universe_candidates", check_name="nonpositive_last_close",
        sql="SELECT COUNT(*) /*universe_candidates/nonpositive_last_close*/ FROM platform.universe_candidates WHERE last_close IS NOT NULL AND last_close <= 0"),
    CrossTableCheck(table="universe_candidates", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*universe_candidates/orphan_no_prices*/ FROM platform.universe_candidates uc LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = uc.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="spread_observations", check_name="negative_spread",
        sql="SELECT COUNT(*) /*spread_observations/negative_spread*/ FROM platform.spread_observations WHERE spread_pct < 0"),
    CrossTableCheck(table="spread_observations", check_name="extreme_spread",
        sql="SELECT COUNT(*) /*spread_observations/extreme_spread*/ FROM platform.spread_observations WHERE spread_pct > 0.5"),
    CrossTableCheck(table="spread_observations", check_name="future_observed_at",
        sql="SELECT COUNT(*) /*spread_observations/future_observed_at*/ FROM platform.spread_observations WHERE observed_at > now()"),
    CrossTableCheck(table="risk_state", check_name="null_engine",
        sql="SELECT COUNT(*) /*risk_state/null_engine*/ FROM platform.risk_state WHERE engine IS NULL"),
    CrossTableCheck(table="corporate_actions", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*corporate_actions/orphan_no_prices*/ FROM platform.corporate_actions ca LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = ca.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="fundamentals_quarterly", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*fundamentals_quarterly/orphan_no_prices*/ FROM platform.fundamentals_quarterly fq LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p ON p.ticker = fq.ticker WHERE p.ticker IS NULL"),
    CrossTableCheck(table="tradier_options_chains", check_name="null_ticker",
        sql="SELECT COUNT(*) /*tradier_options_chains/null_ticker*/ FROM platform.tradier_options_chains WHERE ticker IS NULL"),
    CrossTableCheck(table="tradier_options_chains", check_name="expiration_in_past",
        sql="SELECT COUNT(*) /*tradier_options_chains/expiration_in_past*/ FROM platform.tradier_options_chains WHERE expiration_date < CURRENT_DATE"),
    CrossTableCheck(table="tradier_options_chains", check_name="orphan_no_prices",
        sql="SELECT COUNT(*) /*tradier_options_chains/orphan_no_prices*/ FROM platform.tradier_options_chains tc WHERE NOT EXISTS (SELECT 1 FROM platform.prices_daily_tickers t WHERE t.ticker = tc.ticker)"),
)

_OK = Decimal("1.000")
_FAIL = Decimal("0.000")


async def run_cross_table_audit(
    pool: asyncpg.Pool, *, persist: bool = True
) -> list[CrossTableFinding]:
    """Run every declared check; optionally persist structured rows to
    data_quality_log under ``cross_table_audit.<table>.<check_name>``
    using the audit_data_pipeline._persist severity convention."""
    run_ts = datetime.now(UTC)
    findings: list[CrossTableFinding] = []
    rows: list[tuple] = []
    async with pool.acquire() as conn:
        for c in CROSS_TABLE_CHECKS:
            raw = await conn.fetchval(c.sql)
            n = int(raw) if raw is not None else 0
            sev = "OK" if n == 0 else "FAIL"
            findings.append(
                CrossTableFinding(c.table, c.check_name, n, sev)
            )
            rows.append((
                f"cross_table_audit.{c.table}.{c.check_name}",
                run_ts, 0, 0,
                sev != "OK",
                _OK if sev == "OK" else _FAIL,
                json.dumps({
                    "table": c.table, "check_name": c.check_name,
                    "count": n, "severity": sev,
                })[:8000],
            ))
        if persist and rows:
            await conn.executemany(
                """
                INSERT INTO platform.data_quality_log
                    (source, timestamp, latency_ms, missing_bars,
                     stale, confidence, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (source, timestamp) DO NOTHING
                """,
                rows,
            )
    n_red = sum(1 for f in findings if f.severity != "OK")
    logger.info("cross_table_audit.done", checks=len(findings), red=n_red)
    return findings


__all__ = [
    "CROSS_TABLE_CHECKS",
    "CrossTableCheck",
    "CrossTableFinding",
    "run_cross_table_audit",
]
