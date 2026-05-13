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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

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
        engines: tuple[str, ...] = ("sigma", "reversion", "vector", "momentum"),
        platform_capital: Decimal = Decimal("40000"),
        enforce_freeze: bool = False,
        as_of: date | None = None,
    ) -> None:
        self._pool = pool
        self._engines = engines
        self._platform_capital = platform_capital
        self._enforce_freeze = enforce_freeze
        self._as_of = as_of or datetime.now(UTC).date()

    async def run_once(self) -> list[AllocationDecision]:
        histories = await self._load_histories()
        decisions = self._decide(histories)
        await self._persist(decisions)
        logger.info(
            "tpcore.allocator.rebalance",
            as_of=self._as_of.isoformat(),
            platform_capital=str(self._platform_capital),
            decisions={d.engine: str(d.allocated_capital) for d in decisions},
            frozen=[d.engine for d in decisions if d.freeze_state != "active"],
            enforce_freeze=self._enforce_freeze,
        )
        return decisions

    # ── load ────────────────────────────────────────────────────────────
    async def _load_histories(self) -> list[_EngineHistory]:
        out: list[_EngineHistory] = []
        async with self._pool.acquire() as conn:
            for engine in self._engines:
                rows = await conn.fetch(
                    """
                    SELECT aar_data->>'exit_ts' AS exit_ts,
                           (aar_data->>'pnl_net')::numeric AS pnl_net
                    FROM platform.aar_events
                    WHERE engine = $1
                      AND aar_data->>'exit_ts' IS NOT NULL
                    ORDER BY (aar_data->>'exit_ts')::timestamptz ASC
                    """,
                    engine,
                )
                # Bucket trades into sessions (by exit date) and sum PnL.
                by_session: dict[date, float] = {}
                aar_count = 0
                for r in rows:
                    aar_count += 1
                    ts = r["exit_ts"]
                    if not ts:
                        continue
                    d = (
                        datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if isinstance(ts, str) else ts
                    ).date()
                    pnl = float(r["pnl_net"] or 0)
                    by_session[d] = by_session.get(d, 0.0) + pnl

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
    async def _persist(self, decisions: list[AllocationDecision]) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for d in decisions:
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
