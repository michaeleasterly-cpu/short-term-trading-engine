"""Shared test fixtures for the validation suite.

Provides:
- a fake asyncpg pool (rows-as-dict store) for check tests
- temp YAML writers used by source/check/end-to-end tests
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool
# ────────────────────────────────────────────────────────────────────────────


class FakePool:
    """Tiny in-memory stand-in for asyncpg.Pool.

    Stores rows as a list of dicts; supports `fetch(sql, *args)` and
    `fetchrow(sql, *args)` by routing on substrings of the SQL text. This
    is intentionally narrow: it understands only the queries the validation
    checks emit, and adding a new query shape requires extending the
    routing here.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.calls: list[tuple[str, tuple]] = []

    # ------------------------------ context-manager plumbing
    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self)

    # ------------------------------ query routing (used by checks + writer)
    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        sql_lower = sql.lower()
        # prices_daily_completeness check: return one liquid live ticker
        # whose single active session is fully covered, so the
        # zero-tolerance invariant passes for e2e tests focused on
        # unrelated checks. args[2] is the real NYSE window-session list
        # the check derived from tpcore.calendar; its last element is
        # the most-recent session.
        if "window_dates" in sql_lower:
            window = list(args[2]) if len(args) > 2 else []
            if not window:
                return []
            latest = window[-1]
            return [{
                "ticker": "AAPL",
                "first_bar": latest,
                "last_bar": latest,
                "window_dates": [latest],
            }]
        if "platform.prices_daily" in sql_lower and "ticker = any($1)" in sql_lower:
            tickers = set(args[0])
            return [r for r in self.rows if r["ticker"] in tickers]
        if "platform.prices_daily" in sql_lower and "ticker = $1" in sql_lower:
            ticker = args[0]
            return [r for r in self.rows if r["ticker"] == ticker]
        # macro_indicators_freshness check: return one fresh row per
        # expected indicator so the suite passes when running e2e
        # tests focused on unrelated checks.
        if "platform.macro_indicators" in sql_lower and "group by indicator" in sql_lower:
            from datetime import UTC, datetime, timedelta
            today = datetime.now(UTC).date() - timedelta(days=5)
            return [
                {"indicator": name, "latest_date": today, "rows_total": 100}
                for name in (
                    "sahm_rule", "industrial_production", "initial_claims",
                    "yield_curve", "credit_spread", "hy_spread", "vix",
                )
            ]
        # options_max_pain_freshness: one fresh snapshot per expected
        # symbol so the suite passes in e2e tests for unrelated checks.
        if "platform.options_max_pain" in sql_lower:
            from datetime import UTC, datetime, timedelta
            fresh = datetime.now(UTC).date() - timedelta(days=1)
            return [{"symbol": "SPY", "latest": fresh}]
        return []

    async def fetchrow(self, sql: str, *args) -> dict[str, Any] | None:
        sql_lower = sql.lower()
        # insider_sentiment_freshness: a current-month record so the
        # suite passes in e2e tests for unrelated checks.
        if "platform.insider_sentiment" in sql_lower:
            from datetime import UTC, datetime
            now = datetime.now(UTC)
            return {"newest_period": now.year * 12 + now.month, "rows_total": 10}
        # social_sentiment_freshness coverage CTE: 50% coverage (passes
        # the 30% floor) so the suite is green in unrelated e2e tests.
        if "platform.social_sentiment" in sql_lower and "covered" in sql_lower:
            return {"universe": 100, "covered": 50}
        # Catalyst freshness check fires its own CTE that doesn't hit
        # the prices_daily routes above. Return a "clean" snapshot so
        # e2e tests focused on unrelated checks (splits etc.) don't
        # false-fail on catalyst coverage.
        if "platform.catalyst_events" in sql_lower and "addressable" in sql_lower:
            from datetime import UTC, datetime, timedelta
            return {
                "newest_event": datetime.now(UTC).date() - timedelta(days=5),
                "addressable_count": 50,
                "covered_count": 30,  # 60% — well above 20% floor
                "total_rows": 1000,
            }
        # SEC freshness check has the same CTE shape (addressable +
        # newest filing). Return a "clean" snapshot so e2e tests for
        # unrelated checks don't false-fail.
        if "sec_insider_transactions" in sql_lower and "addressable" in sql_lower:
            from datetime import UTC, datetime, timedelta
            return {
                "newest_filing": datetime.now(UTC).date() - timedelta(days=2),
                "addressable_count": 50,
                "covered_count": 25,  # 50% — well above 30% floor
                "insider_rows": 500,
                "material_rows": 700,
            }
        # Liquidity tiers freshness check probes the table + universe.
        # Return a passing snapshot so unrelated tests don't false-fail.
        if "liquidity_tiers" in sql_lower and "active_universe" in sql_lower:
            from datetime import UTC, datetime, timedelta
            return {
                "latest": datetime.now(UTC) - timedelta(days=10),
                "rows_total": 5000,
                "t1_t2_count": 1000,  # 20% — well above 3% floor
                "active_universe": 5000,
            }
        # Ticker classifications coverage check.
        if "ticker_classifications" in sql_lower and "unclassified" in sql_lower:
            from datetime import UTC, datetime, timedelta
            return {
                "latest_update": datetime.now(UTC) - timedelta(days=10),
                "classified_rows": 13000,
                "active_universe": 5000,
                "unclassified": 100,  # 98% coverage — above 90% floor
            }
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args) -> Any:
        """Scalar query. Used by the row_integrity check's COUNT(*).
        Returns 0 by default so existing tests, which don't care about
        integrity, get the "clean" signal automatically."""
        self.calls.append((sql, args))
        if "count(*)" in sql.lower() and "platform.prices_daily" in sql.lower():
            return 0
        # social_sentiment_freshness MAX(date): a fresh date so the
        # suite is green in e2e tests for unrelated checks.
        if "max(date)" in sql.lower() and "platform.social_sentiment" in sql.lower():
            from datetime import UTC, datetime, timedelta
            return datetime.now(UTC).date() - timedelta(days=1)
        # fear_greed_freshness MAX(date): a fresh date so the suite is
        # green in e2e tests for unrelated checks.
        if "max(date)" in sql.lower() and "platform.fear_greed" in sql.lower():
            from datetime import UTC, datetime, timedelta
            return datetime.now(UTC).date() - timedelta(days=1)
        # short_interest_freshness MAX(settlement_date) / borrow_rates
        # MAX(date): fresh dates so the suite is green for unrelated e2e.
        if "max(settlement_date)" in sql.lower() and "platform.short_interest" in sql.lower():
            from datetime import UTC, datetime, timedelta
            return datetime.now(UTC).date() - timedelta(days=10)
        if "max(date)" in sql.lower() and "platform.borrow_rates" in sql.lower():
            from datetime import UTC, datetime, timedelta
            return datetime.now(UTC).date() - timedelta(days=1)
        return None


class _FakeAcquireCM:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakePool:
        return self._pool

    async def __aexit__(self, *exc) -> None:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Builders for synthetic prices_daily rows
# ────────────────────────────────────────────────────────────────────────────


def make_bar(
    ticker: str,
    bar_date: date,
    close: Decimal,
    *,
    delisted: bool = False,
    delisting_date: date | None = None,
) -> dict[str, Any]:
    """Build one row matching `platform.prices_daily` columns the checks read."""
    return {
        "ticker": ticker,
        "date": bar_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1_000_000,
        "adjusted_close": close,
        "delisted": delisted,
        "delisting_date": delisting_date,
    }


# ────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def write_yaml(tmp_path: Path):
    """Helper that writes a YAML payload to ``tmp_path/<name>``."""

    def _write(name: str, body: str) -> Path:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        return p

    return _write
