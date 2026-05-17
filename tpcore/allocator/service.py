"""AllocatorService — weekly capital rebalance across engines.

Reads per-engine equity history from ``platform.aar_events`` (paper or
live fills both count), computes inverse-volatility weights, classifies
drawdown state, writes an audit row to ``platform.allocations``, and
updates ``platform.risk_state.engine_equity``.

Engines consume ``engine_equity`` via ``RiskStateStore.get`` — no
engine-side code change needed once this runs.

Key design choices (per 2026-05-13 expert review):

* **Inverse-vol weighting** with floor 0.10 and ceiling 0.50. Expected
  return never enters the formula — only dispersion does.
* **Bootstrap** = equal weight per engine until each has ≥20 completed
  AARs (the minimum sample for realized σ to mean something).
* **Soft freeze** at drawdown ≥ 15% over last 60 sessions; weight → 0,
  redistributed across non-frozen engines. Lifts when DD ≤ 7.5%
  (hysteresis prevents flapping).
* **Hard freeze** at drawdown ≥ 25% OR soft-frozen state ≥ 30 sessions.
  Sets ``risk_state.kill_switch_active = TRUE`` **only when**
  ``enforce_freeze=True`` (live mode). In paper mode the freeze is
  recorded to ``allocations`` for review only.
* **Atomicity**: allocations + risk_state writes are wrapped in a
  single transaction so a partial failure doesn't leave engines reading
  half-updated capital.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from tpcore.aar import AARReader
from tpcore.indicators.chop import (
    CHOP_SIDEWAYS_STRONG,
    CHOP_SIDEWAYS_WEAK,
    compute_chop,
)
from tpcore.logging.db_handler import DBLogHandler

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Parameters — single source of truth so the spec doc and the code agree
# ────────────────────────────────────────────────────────────────────────

VOL_LOOKBACK_SESSIONS = 60        # trailing window for realized σ
MIN_AARS_FOR_VOL = 20             # below this, bootstrap (equal weight) wins
WEIGHT_FLOOR = Decimal("0.10")
WEIGHT_CEILING = Decimal("0.50")
SOFT_FREEZE_DD = Decimal("0.15")   # 15% drawdown → soft freeze
SOFT_FREEZE_RECOVERY_DD = Decimal("0.075")  # lift when DD ≤ 7.5%
HARD_FREEZE_DD = Decimal("0.25")   # 25% drawdown → hard freeze
HARD_FREEZE_SOFT_SESSIONS = 30     # ≥ 30 sessions in soft state → hard

# ── Rebalance gating (audit items 44 + 45, added 2026-05-14) ──────────
# Soft band: drift between SOFT and HARD only triggers a rebalance when
# the market regime is favorable (not transitional). Below SOFT: skip
# (changes too small to justify the round-trip cost). Above HARD:
# force-rebalance regardless of regime.
SOFT_BAND_DRIFT_PCT = Decimal("0.25")  # 25%
HARD_BAND_DRIFT_PCT = Decimal("0.50")  # 50%
# How many trailing SPY sessions to compute CHOP on. 60 matches
# VOL_LOOKBACK_SESSIONS for symmetry and is comfortably > CHOP_PERIOD=14.
REGIME_CHOP_LOOKBACK_SESSIONS = 60

# Engines that have been ARCHIVED — their platform.risk_state row is
# stale and must be pruned. This is an explicit allowlist (NOT derived
# from self._engines) so the prune can ONLY ever delete known-dead
# engines and can never delete a live engine's risk state, regardless
# of how the allocator's managed-engine set is configured. Add an
# engine here only when it is archived (see archive/<engine>/EULOGY.md).
_ARCHIVED_ENGINES: tuple[str, ...] = ("sigma",)


class AllocationDecision(BaseModel):
    """Per-engine output of one rebalance run. Maps 1:1 to a row in
    ``platform.allocations`` plus a write to ``risk_state.engine_equity``."""

    model_config = ConfigDict(extra="forbid")

    engine: str
    weight: Decimal = Field(description="Normalized inverse-vol weight in [0, 1]")
    allocated_capital: Decimal
    prior_equity: Decimal
    realized_vol: Decimal | None = Field(default=None, description="None during bootstrap")
    freeze_state: str = Field(default="active", pattern="^(active|soft_frozen|hard_frozen)$")
    freeze_reason: str | None = None
    drawdown_pct: Decimal | None = None


@dataclass
class _EngineHistory:
    engine: str
    aar_count: int
    daily_pnls: list[float]   # per-session sums; len ≤ VOL_LOOKBACK_SESSIONS
    equity_curve: list[float]  # cumulative equity per session
    peak_equity: float
    current_equity: float
    soft_frozen_sessions: int  # consecutive sessions in soft-frozen state

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal("0")
        return Decimal(str(max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity)))

    @property
    def has_enough_for_vol(self) -> bool:
        return self.aar_count >= MIN_AARS_FOR_VOL and len(self.daily_pnls) >= 2

    @property
    def realized_vol(self) -> Decimal | None:
        if not self.has_enough_for_vol:
            return None
        return Decimal(str(statistics.pstdev(self.daily_pnls)))


class AllocatorService:
    """One rebalance per call to ``run_once``. Idempotent on
    ``(engine, allocation_date)`` via the table's unique constraint."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        # Default managed set for the weekly inverse-vol pool. Production
        # constructs AllocatorService WITHOUT engines= (scripts/ops.py
        # cmd_allocate, scripts/run_allocator.py), so this default IS the
        # live roster. sigma archived 2026-05-16 (removed — keeping it
        # made the per-engine upsert loop resurrect a stale sigma
        # risk_state row every run); sentinel intentionally excluded —
        # defensive macro overlay budgeted by SentinelCapitalGate
        # (fixed 10–20% cap), not the inverse-vol pool.
        engines: tuple[str, ...] = ("reversion", "vector", "momentum"),
        platform_capital: Decimal = Decimal("40000"),
        enforce_freeze: bool = False,
        as_of: date | None = None,
        run_id: uuid.UUID | None = None,
    ) -> None:
        self._pool = pool
        self._engines = engines
        self._platform_capital = platform_capital
        self._enforce_freeze = enforce_freeze
        self._as_of = as_of or datetime.now(UTC).date()
        self._run_id = run_id or uuid.uuid4()
        # DBLogHandler requires a real pool; existing tests that exercise
        # _decide() in isolation pass pool=None. Lazy-init lets those
        # tests keep working without forcing every caller to provide a
        # DB. run_once() needs a real pool anyway.
        self._db_log: DBLogHandler | None = (
            DBLogHandler(pool, engine="allocator", run_id=self._run_id)
            if pool is not None else None
        )

    async def run_once(self) -> list[AllocationDecision]:
        histories = await self._load_histories()
        decisions = self._decide(histories)

        # ── Rebalance gating — items 44 + 45 (2026-05-14) ───────────────
        # 1. Compute drift per active engine (frozen engines bypass).
        max_drift, drift_per_engine = await self._compute_drift(decisions)
        # 2. Fetch SPY-based market regime.
        regime, chop_value = await self._fetch_market_regime()
        # 3. Classify rebalance decision.
        skip_reason, rebalance_reason = self._classify_rebalance(max_drift, regime)
        # 4. Persist (always for frozen rows; conditional for active rows).
        if skip_reason is not None:
            pruned_engines = await self._persist(decisions, active_skip=True)
            if self._db_log is not None:
                await self._db_log.log(
                event_type="ALLOCATOR_SKIPPED",
                message=f"rebalance skipped — {skip_reason}",
                severity="INFO",
                data={
                    "as_of": self._as_of.isoformat(),
                    "reason": skip_reason,
                    "max_drift_pct": float(max_drift),
                    "drift_per_engine": {k: float(v) for k, v in drift_per_engine.items()},
                    "regime": regime,
                    "chop_value": float(chop_value) if chop_value is not None else None,
                    "frozen_engines_persisted": [
                        d.engine for d in decisions if d.freeze_state != "active"
                    ],
                },
            )
        else:
            pruned_engines = await self._persist(decisions, active_skip=False)
            if self._db_log is not None:
                await self._db_log.log(
                event_type="ALLOCATOR_REBALANCED",
                message=f"rebalanced — {rebalance_reason}",
                severity="INFO",
                data={
                    "as_of": self._as_of.isoformat(),
                    "reason": rebalance_reason,
                    "max_drift_pct": float(max_drift),
                    "drift_per_engine": {k: float(v) for k, v in drift_per_engine.items()},
                    "regime": regime,
                    "chop_value": float(chop_value) if chop_value is not None else None,
                    "new_weights": {d.engine: float(d.weight) for d in decisions},
                },
            )

        # ── Prune audit (only when something was actually pruned) ────────
        # Mirrors the ALLOCATOR_REBALANCED / ALLOCATOR_SKIPPED emission
        # above exactly: same self._db_log handler, same (engine, run_id)
        # binding, same INSERT INTO platform.application_log path. One row
        # per prune; zero rows when nothing stale (idempotent — a second
        # run finds a clean table, prunes nothing, logs nothing).
        if pruned_engines and self._db_log is not None:
            await self._db_log.log(
                event_type="ALLOCATOR_PRUNED_RISK_STATE",
                message=f"pruned {len(pruned_engines)} stale risk_state row(s)",
                severity="INFO",
                data={
                    "as_of": self._as_of.isoformat(),
                    "pruned_engines": pruned_engines,
                    "live_engines": list(self._engines),
                },
            )

        logger.info(
            "tpcore.allocator.rebalance",
            as_of=self._as_of.isoformat(),
            platform_capital=str(self._platform_capital),
            decisions={d.engine: str(d.allocated_capital) for d in decisions},
            frozen=[d.engine for d in decisions if d.freeze_state != "active"],
            enforce_freeze=self._enforce_freeze,
            max_drift_pct=float(max_drift),
            regime=regime,
            skipped=skip_reason is not None,
            decision_reason=skip_reason or rebalance_reason,
        )
        return decisions

    # ── Rebalance-gating helpers (items 44 + 45) ────────────────────────

    async def _fetch_market_regime(self) -> tuple[str, float | None]:
        """Query SPY bars + classify CHOP regime.

        Returns ``(regime, chop_value)`` where regime is one of
        ``trending`` / ``transitional`` / ``choppy``. If SPY data is
        missing or CHOP can't be computed (insufficient lookback),
        defaults to ``trending`` — that's the most permissive regime
        and ensures the gate doesn't silently block rebalances when
        the data layer has a temporary issue.
        """
        import pandas as pd  # local import — pandas already in deps

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date, high, low, close
                FROM platform.prices_daily
                WHERE ticker = 'SPY'
                  AND date >= CURRENT_DATE - INTERVAL '120 days'
                ORDER BY date
                """
            )
        if not rows:
            logger.warning("tpcore.allocator.regime.no_spy_data")
            return ("trending", None)
        high = pd.Series([float(r["high"]) for r in rows])
        low = pd.Series([float(r["low"]) for r in rows])
        close = pd.Series([float(r["close"]) for r in rows])
        chop_series = compute_chop(high, low, close)
        if chop_series.empty or pd.isna(chop_series.iloc[-1]):
            logger.warning("tpcore.allocator.regime.chop_undefined", n_rows=len(rows))
            return ("trending", None)
        chop_now = float(chop_series.iloc[-1])
        if chop_now > CHOP_SIDEWAYS_STRONG:
            return ("choppy", chop_now)
        if chop_now >= CHOP_SIDEWAYS_WEAK:
            return ("transitional", chop_now)
        return ("trending", chop_now)

    async def _compute_drift(
        self, decisions: list[AllocationDecision],
    ) -> tuple[Decimal, dict[str, Decimal]]:
        """Compare new vs. prior weights per active engine.

        Returns ``(max_drift, per_engine_drift)``. Frozen engines are
        excluded — drift gating applies to active engines only. If an
        active engine has no prior allocation row (first run), drift
        is 1.0 (force rebalance).
        """
        per_engine: dict[str, Decimal] = {}
        max_drift = Decimal("0")
        async with self._pool.acquire() as conn:
            for d in decisions:
                if d.freeze_state != "active":
                    continue
                prior = await conn.fetchval(
                    """
                    SELECT weight FROM platform.allocations
                    WHERE engine = $1
                    ORDER BY allocation_date DESC
                    LIMIT 1
                    """,
                    d.engine,
                )
                if prior is None:
                    per_engine[d.engine] = Decimal("1")  # first run → force
                else:
                    prior_w = Decimal(str(prior))
                    if prior_w == 0:
                        # Prior was frozen; any active weight is "infinite"
                        # drift. Force rebalance.
                        per_engine[d.engine] = Decimal("1") if d.weight > 0 else Decimal("0")
                    else:
                        per_engine[d.engine] = abs(d.weight - prior_w) / prior_w
                if per_engine[d.engine] > max_drift:
                    max_drift = per_engine[d.engine]
        return max_drift, per_engine

    @staticmethod
    def _classify_rebalance(
        max_drift: Decimal, regime: str,
    ) -> tuple[str | None, str | None]:
        """Return ``(skip_reason, rebalance_reason)`` — exactly one is None.

        Decision tree (audit items 44 + 45):
        * drift < SOFT_BAND_DRIFT_PCT          → skip (drift_below_threshold)
        * SOFT ≤ drift < HARD AND transitional → skip (regime_transitional)
        * SOFT ≤ drift < HARD AND not transitional → rebalance (soft_band)
        * drift ≥ HARD_BAND_DRIFT_PCT          → rebalance (hard_band_override)
        """
        if max_drift < SOFT_BAND_DRIFT_PCT:
            return ("drift_below_threshold", None)
        if max_drift < HARD_BAND_DRIFT_PCT:
            if regime == "transitional":
                return ("regime_transitional", None)
            return (None, "soft_band")
        return (None, "hard_band_override")

    # ── load ────────────────────────────────────────────────────────────
    async def _load_histories(self) -> list[_EngineHistory]:
        out: list[_EngineHistory] = []
        reader = AARReader(self._pool)
        async with self._pool.acquire() as conn:
            for engine in self._engines:
                aars = await reader.fetch_by_engine(engine)
                # Bucket trades into sessions (by exit date) and sum PnL.
                by_session: dict[date, float] = {}
                for aar in aars:
                    d = aar.exit_ts.date()
                    by_session[d] = by_session.get(d, 0.0) + float(aar.pnl_net)
                aar_count = len(aars)

                sessions = sorted(by_session.keys())
                daily_pnls = [by_session[d] for d in sessions[-VOL_LOOKBACK_SESSIONS:]]

                # Reconstruct equity curve. Bootstrap equity from risk_state.
                seed = await conn.fetchval(
                    "SELECT engine_equity FROM platform.risk_state WHERE engine = $1",
                    engine,
                )
                seed = float(seed or 10_000)
                # Pure additive curve over trailing window — used for
                # peak/drawdown only, NOT for engine_equity.
                eq = seed - sum(daily_pnls)  # equity at start of window
                curve: list[float] = []
                running = eq
                for p in daily_pnls:
                    running += p
                    curve.append(running)
                peak = max(curve) if curve else seed
                current = curve[-1] if curve else seed

                # Soft-frozen-sessions counter: count consecutive trailing
                # sessions where intra-window drawdown was ≥ SOFT_FREEZE_DD.
                soft_streak = 0
                for c in reversed(curve):
                    if peak > 0 and (peak - c) / peak >= float(SOFT_FREEZE_DD):
                        soft_streak += 1
                    else:
                        break

                out.append(_EngineHistory(
                    engine=engine,
                    aar_count=aar_count,
                    daily_pnls=daily_pnls,
                    equity_curve=curve,
                    peak_equity=peak,
                    current_equity=current,
                    soft_frozen_sessions=soft_streak,
                ))
        return out

    # ── decide ──────────────────────────────────────────────────────────
    def _decide(self, histories: list[_EngineHistory]) -> list[AllocationDecision]:
        # Step 1: classify freeze state per engine.
        freeze_state: dict[str, tuple[str, str | None]] = {}
        for h in histories:
            dd = h.drawdown
            if dd >= HARD_FREEZE_DD:
                freeze_state[h.engine] = ("hard_frozen", f"drawdown {dd:.1%} ≥ {HARD_FREEZE_DD:.0%}")
            elif h.soft_frozen_sessions >= HARD_FREEZE_SOFT_SESSIONS:
                freeze_state[h.engine] = ("hard_frozen", f"soft-frozen {h.soft_frozen_sessions} sessions")
            elif dd >= SOFT_FREEZE_DD:
                freeze_state[h.engine] = ("soft_frozen", f"drawdown {dd:.1%} ≥ {SOFT_FREEZE_DD:.0%}")
            else:
                freeze_state[h.engine] = ("active", None)

        # Step 2: bootstrap or inverse-vol per engine.
        eligible = [h for h in histories if freeze_state[h.engine][0] == "active"]
        raw_weights: dict[str, Decimal] = {}
        for h in eligible:
            if h.has_enough_for_vol and h.realized_vol and h.realized_vol > 0:
                raw_weights[h.engine] = Decimal("1") / h.realized_vol
            else:
                raw_weights[h.engine] = Decimal("1")  # bootstrap → equal

        # Step 3: normalize + apply [floor, ceiling] caps. Iterate until
        # caps converge (post-cap renormalization can push another
        # engine outside the band, so we re-cap until stable).
        weights = self._normalize_and_cap(raw_weights)

        # Step 4: build decisions for every engine — frozen engines get
        # weight=0, allocation=0 but the row is still written for audit.
        decisions: list[AllocationDecision] = []
        for h in histories:
            state, reason = freeze_state[h.engine]
            if state != "active":
                w = Decimal("0")
                cap = Decimal("0")
            else:
                w = weights.get(h.engine, Decimal("0"))
                cap = (w * self._platform_capital).quantize(Decimal("0.01"))
            decisions.append(AllocationDecision(
                engine=h.engine,
                weight=w,
                allocated_capital=cap,
                prior_equity=Decimal(str(h.current_equity)).quantize(Decimal("0.0001")),
                realized_vol=h.realized_vol,
                freeze_state=state,
                freeze_reason=reason,
                drawdown_pct=h.drawdown.quantize(Decimal("0.0001")),
            ))
        return decisions

    @staticmethod
    def _normalize_and_cap(raw: dict[str, Decimal]) -> dict[str, Decimal]:
        if not raw:
            return {}
        # Up to ~10 iterations is more than enough for 4 engines; bail
        # out if caps stabilize earlier.
        weights = dict(raw)
        for _ in range(10):
            total = sum(weights.values())
            if total == 0:
                return {k: Decimal("0") for k in weights}
            scaled = {k: v / total for k, v in weights.items()}
            capped = False
            for k, v in scaled.items():
                if v > WEIGHT_CEILING:
                    scaled[k] = WEIGHT_CEILING
                    capped = True
                elif v < WEIGHT_FLOOR:
                    scaled[k] = WEIGHT_FLOOR
                    capped = True
            # Renormalize uncapped engines to fill what's left.
            cap_total = sum(scaled.values())
            if cap_total == 0:
                return {k: Decimal("0") for k in scaled}
            weights = {k: v / cap_total for k, v in scaled.items()}
            if not capped:
                break
        # Final pass — quantize to avoid Decimal precision drift on persist.
        q = Decimal("0.00001")
        return {k: v.quantize(q) for k, v in weights.items()}

    # ── persist ─────────────────────────────────────────────────────────
    async def _persist(
        self,
        decisions: list[AllocationDecision],
        *,
        active_skip: bool = False,
    ) -> list[str]:
        """Persist decisions to ``platform.allocations`` + ``risk_state``.

        When ``active_skip=True`` (drift/regime gate said skip), only
        frozen-engine rows are written. Frozen-engine state changes
        must always land so engines reading ``risk_state.engine_equity``
        see weight=0 promptly. Active engines keep their prior rows
        and prior ``engine_equity`` until the next rebalance fires.

        Returns the sorted list of engine names pruned from
        ``platform.risk_state`` (rows for engines no longer managed by
        this allocator — e.g. an archived engine). Empty when nothing
        was stale; the prune runs inside the same transaction as the
        upserts so engines never observe a half-updated risk state.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for d in decisions:
                    if active_skip and d.freeze_state == "active":
                        continue
                    await conn.execute(
                        """
                        INSERT INTO platform.allocations
                            (engine, allocated_capital, allocation_date,
                             weight, prior_equity, realized_vol,
                             freeze_state, freeze_reason, drawdown_pct,
                             decided_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                        ON CONFLICT (engine, allocation_date) DO UPDATE SET
                            allocated_capital = EXCLUDED.allocated_capital,
                            weight = EXCLUDED.weight,
                            prior_equity = EXCLUDED.prior_equity,
                            realized_vol = EXCLUDED.realized_vol,
                            freeze_state = EXCLUDED.freeze_state,
                            freeze_reason = EXCLUDED.freeze_reason,
                            drawdown_pct = EXCLUDED.drawdown_pct,
                            decided_at = now()
                        """,
                        d.engine, d.allocated_capital, self._as_of,
                        d.weight, d.prior_equity, d.realized_vol,
                        d.freeze_state, d.freeze_reason, d.drawdown_pct,
                    )
                    # engine_equity is THE seam engines read. Always update,
                    # even for frozen engines (engine_equity → 0 means the
                    # cost-model gate refuses every new trade).
                    await conn.execute(
                        """
                        INSERT INTO platform.risk_state (engine, engine_equity, daily_reset_at, weekly_reset_at, updated_at)
                        VALUES ($1, $2, now(), now() + INTERVAL '7 days', now())
                        ON CONFLICT (engine) DO UPDATE SET
                            engine_equity = EXCLUDED.engine_equity,
                            updated_at = now()
                        """,
                        d.engine, d.allocated_capital,
                    )
                    # Kill-switch enforcement is gated on enforce_freeze.
                    if self._enforce_freeze and d.freeze_state == "hard_frozen":
                        await conn.execute(
                            "UPDATE platform.risk_state SET kill_switch_active=TRUE, kill_switch_reason=$2 WHERE engine=$1",
                            d.engine, f"allocator: {d.freeze_reason}",
                        )
                    elif self._enforce_freeze and d.freeze_state == "active":
                        # Auto-clear when the allocator says "active" again.
                        await conn.execute(
                            "UPDATE platform.risk_state SET kill_switch_active=FALSE, kill_switch_reason=NULL WHERE engine=$1 AND kill_switch_active=TRUE AND kill_switch_reason LIKE 'allocator:%'",
                            d.engine,
                        )

                # ── Prune stale risk_state rows (archived engines) ────────
                # The allocator is the canonical owner of risk_state. An
                # archived engine's row (e.g. sigma after its 2026-05-16
                # archival) is stale and must not linger: engines read
                # risk_state, and a dead engine's row is a silent landmine.
                #
                # CRITICAL: the prune targets the explicit
                # _ARCHIVED_ENGINES allowlist via `engine = ANY($1)`, NOT
                # `engine <> ALL(self._engines)`. Keying off self._engines
                # is fail-DANGEROUS: production constructs AllocatorService
                # without engines=, so self._engines is the __init__
                # default ("reversion","vector","momentum") which would
                # DELETE live sentinel's risk_state row (sentinel is not
                # in the default set). An explicit archived allowlist can
                # only ever delete known-dead engines, regardless of the
                # managed set.
                #
                # Ordering: this DELETE runs AFTER the per-engine upsert
                # loop above, inside the SAME conn.transaction(). The
                # __init__ default no longer includes sigma (removed
                # 2026-05-16, commit f0c78c4), so the upsert loop does
                # NOT recreate a sigma risk_state row; this prune still
                # removes any pre-existing/legacy sigma row via the
                # explicit _ARCHIVED_ENGINES allowlist, independent of
                # the managed set, so the net committed effect is "any
                # stale sigma row removed" — correct.
                # Idempotent: a clean table deletes zero rows.
                # Parameterised (never string-interpolated).
                pruned_rows = await conn.fetch(
                    """
                    DELETE FROM platform.risk_state
                    WHERE engine = ANY($1::text[])
                    RETURNING engine
                    """,
                    list(_ARCHIVED_ENGINES),
                )
                pruned_engines = sorted(r["engine"] for r in pruned_rows)
        return pruned_engines
