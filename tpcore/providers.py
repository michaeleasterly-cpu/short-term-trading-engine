"""Data-provider binding registry — the snap-in/out control surface.

Flat single-source-of-truth, symmetric to ``tpcore.engine_profile`` /
``tpcore.risk.limits_profile`` / ``tpcore.feeds.FeedProfile`` /
``tpcore.selfheal.HealSpec``. Decouples **feed** (the logical data need;
what consumers reference via ``DataProviderInterface``) from
**provider** (a concrete source + adapter that satisfies it).

Phase 1 of the Data Provider Lifecycle (spec
``docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md``,
plan ``…/plans/2026-05-17-data-provider-lifecycle-plan.md``). **Landed
dark**: nothing in the runtime/ingest path imports this in Phase 1 —
it records *current reality* and is the SoT the later CUTOVER/EVALUATE
phases act on. Same model as ``engine_profile`` Sub-project A.

Bindings are EVIDENCE-DERIVED (read out of each handler/adapter), never
assumed — the same discipline as ``HealSpec.depends_on`` and the
``FeedProfile`` evidence strings. Today every feed has exactly one
ACTIVE provider and no fallbacks; Phase 4 adds parity-verified
candidates.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class ProviderStatus(StrEnum):
    """Lifecycle status of one (feed, provider) binding."""

    CANDIDATE = "candidate"    # proposed; not serving
    ACTIVE = "active"          # the one serving the feed now
    FALLBACK = "fallback"      # parity-verified; cutover-ready standby
    DEPRECATED = "deprecated"  # scheduled for retirement
    RETIRED = "retired"        # offboarded; kept for provenance only


class ProviderBinding(BaseModel):
    """One (feed, provider) binding. Frozen — the registry is a SoT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Logical feed — the FeedProfile / HealSpec.source vocabulary.
    feed: str
    # Concrete provider identity ("alpaca", "fred", "internal", …).
    provider: str
    # Dotted path to the CURRENT ingest entrypoint for this binding.
    # Phase 1 records the true entrypoint (function/stage); the
    # DataProviderInterface conformance is an ONBOARD-gate concern for
    # NEW providers (spec §4 stage 3), not retrofitted onto the SoT.
    adapter_module: str
    status: ProviderStatus
    # WHY this binding/status — no-vendor-blame discipline (mirrors
    # FeedProfile.evidence). How the provider was determined; for
    # derived feeds, what it is computed from.
    evidence: str
    # Last EVALUATE data-parity pass vs the incumbent. Required for a
    # FALLBACK (it cannot stand in without a parity pass) — enforced
    # now even though the parity gate itself lands in Phase 2.
    parity_verified_at: date | None = None

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        if self.status is ProviderStatus.FALLBACK and self.parity_verified_at is None:
            raise ValueError(
                f"ProviderBinding[{self.feed}/{self.provider}]: FALLBACK "
                f"requires parity_verified_at (a standby must be parity-"
                f"verified vs the incumbent before it can be cut over)"
            )
        if not self.evidence.strip():
            raise ValueError(
                f"ProviderBinding[{self.feed}/{self.provider}]: evidence "
                f"is mandatory (no-vendor-blame discipline)"
            )


# Evidence-derived from each handler/adapter (read, not assumed).
# Exactly one ACTIVE per feed; no fallbacks yet (Phase 4). Feed set ==
# tpcore.feeds.FEED_PROFILES keys (the drift test enforces both ways).
_BINDINGS: tuple[ProviderBinding, ...] = (
    ProviderBinding(
        feed="prices_daily", provider="alpaca",
        adapter_module="tpcore.data.ingest_alpaca_bars",
        status=ProviderStatus.ACTIVE,
        evidence="Alpaca /v2/stocks/bars multi-symbol; feed=iex (free "
                 "tier has no SIP entitlement — verified 2026-05-17).",
    ),
    ProviderBinding(
        feed="macro_indicators", provider="fred",
        adapter_module="tpcore.ingestion.handlers.handle_macro_indicators",
        status=ProviderStatus.ACTIVE,
        evidence="FRED series (INDICATOR_SERIES), pulled per-series with "
                 "skip_guard. hy_spread (BAMLH0A0HYM2) is subject to FRED "
                 "rolling-window truncation (the BAMLH0A0HYM2 incident); "
                 "the eco_archive CANDIDATE below is the recovery path.",
    ),
    # Phase 4: the ONE real alternative for this feed (no others exist —
    # the registry is not padded with fictitious fallbacks). Honest
    # CANDIDATE, NOT FALLBACK: a FALLBACK requires parity_verified_at
    # and "cutover-ready standby" semantics. This is the
    # hist_csv_path/hist_indicator recovery path that reloaded
    # BAMLH0A0HYM2 1996-2021 (eco-archive + Scribd fred-graph gap),
    # validated 772/772 EXACT on 2026-05-16 — parity-grade accuracy on
    # the historical overlap. It is NOT a live drop-in: it serves the
    # historical span only and does NOT keep the recent tail fresh
    # (FRED does). A true FALLBACK would be a hybrid (eco-archive
    # history + FRED live tail) — a future EVALUATE/ONBOARD, not
    # claimable today. CANDIDATE needs no parity_verified_at, so this
    # records the real recovery capability without fabricating a
    # cutover-ready date.
    ProviderBinding(
        feed="macro_indicators", provider="eco_archive",
        adapter_module="tpcore.ingestion.handlers._ingest_macro_hist_csv",
        status=ProviderStatus.CANDIDATE,
        evidence="Static-history recovery for hy_spread (BAMLH0A0HYM2) "
                 "when FRED truncates: loads the eco-archive + Scribd "
                 "fred-graph CSV (1996-2021), validated 772/772 EXACT "
                 "2026-05-16. CANDIDATE not FALLBACK — covers the "
                 "historical span only, does not keep the live tail "
                 "fresh; a full fallback (hybrid history+live tail) is a "
                 "future EVALUATE/ONBOARD.",
    ),
    ProviderBinding(
        feed="earnings_events", provider="fmp",
        adapter_module="scripts.ops._stage_earnings_refresh",
        status=ProviderStatus.ACTIVE,
        evidence="FMP earnings beats (weekly refresh; stock universe only).",
    ),
    ProviderBinding(
        feed="sec_insider_transactions", provider="sec_edgar",
        adapter_module="tpcore.ingestion.handlers.handle_sec_filings",
        status=ProviderStatus.ACTIVE,
        evidence="SEC EDGAR — bulk Form-345 datasets (insider) + 8-K "
                 "(material events).",
    ),
    ProviderBinding(
        feed="finra_short_interest", provider="finra",
        adapter_module="tpcore.ingestion.handlers.handle_finra_short_interest",
        status=ProviderStatus.ACTIVE,
        evidence="FINRA bi-monthly short-interest; 60d window covers the "
                 "latest ~3 settlement periods.",
    ),
    ProviderBinding(
        feed="apewisdom_social_sentiment", provider="apewisdom",
        adapter_module="tpcore.ingestion.handlers.handle_apewisdom_social_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="ApeWisdom API; ~23% measured coverage ceiling (floor "
                 "set at 15% from that evidence).",
    ),
    ProviderBinding(
        feed="iborrowdesk_borrow_rates", provider="iborrowdesk",
        adapter_module="tpcore.ingestion.handlers.handle_iborrowdesk_borrow_rates",
        status=ProviderStatus.ACTIVE,
        evidence="IBorrowDesk scrape (per-ticker); source-side blocks "
                 "degrade gracefully → escalation, not silent green.",
    ),
    ProviderBinding(
        feed="aaii_sentiment", provider="aaii",
        adapter_module="tpcore.ingestion.handlers.handle_aaii_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="AAII weekly sentiment workbook (full-history, "
                 "idempotent); vendor-anchored freshness (Thu publish).",
    ),
    ProviderBinding(
        feed="finnhub_insider_sentiment", provider="finnhub",
        adapter_module="tpcore.ingestion.handlers.handle_finnhub_insider_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="Finnhub insider-sentiment, full T1/T2 stock universe "
                 "loop; monthly cadence.",
    ),
    ProviderBinding(
        feed="greeks_max_pain", provider="tradier",
        adapter_module="tpcore.ingestion.handlers.handle_greeks_max_pain",
        status=ProviderStatus.ACTIVE,
        evidence="Max-pain computed from platform.tradier_options_chains "
                 "(Tradier options chains); SPY only.",
    ),
    ProviderBinding(
        feed="ticker_classifications", provider="alpaca",
        adapter_module="tpcore.data.classify_tickers.classify_all_tickers",
        status=ProviderStatus.ACTIVE,
        evidence="Derived from the Alpaca assets list (asset_class via "
                 "name/symbol heuristics) — no separate classifier vendor.",
    ),
    ProviderBinding(
        feed="liquidity_tiers", provider="internal",
        adapter_module="scripts.ops._stage_tier_refresh",
        status=ProviderStatus.ACTIVE,
        evidence="DERIVED internally from prices_daily (price/volume) + "
                 "spread_observations — no external vendor.",
    ),
    ProviderBinding(
        feed="fear_greed", provider="internal",
        adapter_module="tpcore.ingestion.handlers.handle_fear_greed",
        status=ProviderStatus.ACTIVE,
        evidence="DERIVED internally from macro_indicators (VIX/hy/yield) "
                 "+ prices_daily (SPY) — no external vendor; depends_on "
                 "those feeds (see HealSpec).",
    ),
    ProviderBinding(
        feed='fundamentals_quarterly', provider='fmp',
        adapter_module='tpcore.ingestion.handlers.handle_fundamentals_refresh',
        status=ProviderStatus.ACTIVE,
        evidence='financial fundamentals (pb/de/revenue/net_income/fcf/etc) for value-engine setup detection — already ingested via FMP for months; formal ProviderBinding registration was missing (surfaced 2026-05-20 by the autonomous-self-heal P0 completeness invariant work).',
    ),
    ProviderBinding(
        feed='corporate_actions', provider='alpaca',
        adapter_module='tpcore.ingestion.handlers.handle_corporate_actions',
        status=ProviderStatus.ACTIVE,
        evidence='splits + dividends from Alpaca corporate-actions API — already ingested for months; formal ProviderBinding registration was missing (surfaced 2026-05-20 by the autonomous-self-heal P0 completeness invariant work, mirroring the fundamentals_quarterly gap).',
    ),
)


PROVIDER_BINDINGS: dict[str, list[ProviderBinding]] = defaultdict(list)
for _b in _BINDINGS:
    PROVIDER_BINDINGS[_b.feed].append(_b)


def bindings_for(feed: str) -> list[ProviderBinding]:
    """All bindings for ``feed`` (any status). Empty if none."""
    return list(PROVIDER_BINDINGS.get(feed, []))


def active_provider(feed: str) -> ProviderBinding | None:
    """The single ACTIVE binding for ``feed`` (None if unbound)."""
    for b in PROVIDER_BINDINGS.get(feed, []):
        if b.status is ProviderStatus.ACTIVE:
            return b
    return None


def all_feeds() -> set[str]:
    """Every feed with at least one binding."""
    return set(PROVIDER_BINDINGS)


# ────────────────────────────────────────────────────────────────────────
# CUTOVER — automated, deterministic (spec §10: NOT operator-confirmed).
#
# `plan_cutover` is the PURE legality guard (re-landed from the
# unmerged PR #15; that PR's contradicted part was only the
# operator-confirmed *runbook*, not this guard). The runtime overlay
# (`provider_binding_state`) + `apply_cutover` + the ops.cutover_agent
# are what make it automated. `_BINDINGS` stays the frozen declared SoT
# (defaults + which providers exist + parity-verified fallbacks); the
# overlay holds only the live ACTIVE selection.
# ────────────────────────────────────────────────────────────────────────


class CutoverChange(BaseModel):
    """One binding's status transition within a cutover plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    from_status: ProviderStatus
    to_status: ProviderStatus


class CutoverPlan(BaseModel):
    """The validated (or blocked) result of a proposed cutover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feed: str
    to_provider: str
    allowed: bool
    block_reason: str | None = None
    changes: tuple[CutoverChange, ...] = ()

    @property
    def summary(self) -> str:
        if not self.allowed:
            return f"BLOCKED {self.feed}→{self.to_provider}: {self.block_reason}"
        parts = [f"{c.provider}:{c.from_status}→{c.to_status}" for c in self.changes]
        return f"OK {self.feed}→{self.to_provider}: " + "; ".join(parts)


def plan_cutover(
    feed: str,
    to_provider: str,
    *,
    retire_incumbent: bool = False,
) -> CutoverPlan:
    """Validate promoting ``to_provider`` to ACTIVE for ``feed``.

    Eligibility (mirrors EVALUATE): target must be a bound ``FALLBACK``
    (parity-verified — the model enforces a FALLBACK has
    ``parity_verified_at``). ``CANDIDATE`` is NOT eligible (must pass
    EVALUATE → FALLBACK first; skipping parity is the silent-
    degradation class). Exactly-one-ACTIVE preserved; incumbent demoted
    to ``FALLBACK`` (reversible) or ``RETIRED`` (only via the separate
    RETIRE gate). Pure — never mutates, never trades.
    """
    bindings = PROVIDER_BINDINGS.get(feed, [])
    if not bindings:
        return CutoverPlan(feed=feed, to_provider=to_provider, allowed=False,
                           block_reason=f"unknown feed {feed!r} (no bindings)")
    by_provider = {b.provider: b for b in bindings}
    target = by_provider.get(to_provider)
    if target is None:
        return CutoverPlan(
            feed=feed, to_provider=to_provider, allowed=False,
            block_reason=f"{to_provider!r} is not a bound provider for "
                         f"{feed} (have: {sorted(by_provider)})")
    if target.status is ProviderStatus.ACTIVE:
        return CutoverPlan(feed=feed, to_provider=to_provider, allowed=False,
                           block_reason=f"{to_provider} is already ACTIVE for {feed}")
    if target.status is not ProviderStatus.FALLBACK:
        return CutoverPlan(
            feed=feed, to_provider=to_provider, allowed=False,
            block_reason=(
                f"{to_provider} is {target.status} — only a FALLBACK "
                f"(parity-verified) is cutover-eligible. Run EVALUATE to "
                f"promote it first; skipping the parity gate is the "
                f"silent-degradation class the lifecycle prevents."))
    incumbent = active_provider(feed)
    changes: list[CutoverChange] = [
        CutoverChange(provider=to_provider,
                      from_status=ProviderStatus.FALLBACK,
                      to_status=ProviderStatus.ACTIVE)
    ]
    if incumbent is not None:
        changes.append(CutoverChange(
            provider=incumbent.provider, from_status=ProviderStatus.ACTIVE,
            to_status=(ProviderStatus.RETIRED if retire_incumbent
                       else ProviderStatus.FALLBACK)))
    return CutoverPlan(feed=feed, to_provider=to_provider, allowed=True,
                       changes=tuple(changes))


_STATE_SELECT = (
    "SELECT active_provider FROM platform.provider_binding_state "
    "WHERE feed = $1"
)
_STATE_UPSERT = """
    INSERT INTO platform.provider_binding_state (feed, active_provider, reason)
    VALUES ($1, $2, $3)
    ON CONFLICT (feed) DO UPDATE SET
        active_provider = EXCLUDED.active_provider,
        reason          = EXCLUDED.reason,
        updated_at      = now()
"""
_AUDIT_INSERT = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def resolve_active_provider(pool: object, feed: str) -> ProviderBinding | None:
    """Runtime-resolved ACTIVE: the overlay selection if a
    ``provider_binding_state`` row exists and names a known binding,
    else the code-declared ACTIVE. The overlay is how an automated
    cutover takes effect without a code PR."""
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(_STATE_SELECT, feed)
    if row and row["active_provider"]:
        for b in PROVIDER_BINDINGS.get(feed, []):
            if b.provider == row["active_provider"]:
                return b
        logger.warning(
            "providers.overlay_unknown_provider",
            feed=feed, overlay=row["active_provider"],
        )
    return active_provider(feed)


async def apply_cutover(
    pool: object, plan: CutoverPlan, *, run_id: str | None = None
) -> None:
    """Apply an ALLOWED plan: flip the live overlay to the new ACTIVE
    and emit a ``PROVIDER_CUTOVER`` audit event. Idempotent (overlay
    upsert). Raises on a blocked plan — callers must never apply one."""
    import json
    import uuid as _uuid

    if not plan.allowed:
        raise ValueError(f"refusing to apply a blocked cutover: {plan.summary}")
    rid = run_id or str(_uuid.uuid4())
    reason = plan.summary
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(_STATE_UPSERT, plan.feed, plan.to_provider, reason)
        await conn.execute(
            _AUDIT_INSERT, "cutover-agent", rid, "PROVIDER_CUTOVER", "WARNING",
            f"cutover {plan.feed} → {plan.to_provider}",
            json.dumps({
                "schema": 1, "feed": plan.feed,
                "to_provider": plan.to_provider,
                "changes": [c.model_dump() for c in plan.changes],
            }, default=str),
        )
    logger.info("providers.cutover_applied", feed=plan.feed,
                to_provider=plan.to_provider)


__all__ = [
    "PROVIDER_BINDINGS",
    "CutoverChange",
    "CutoverPlan",
    "ProviderBinding",
    "ProviderStatus",
    "active_provider",
    "all_feeds",
    "apply_cutover",
    "bindings_for",
    "plan_cutover",
    "resolve_active_provider",
]
