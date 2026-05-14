"""Plug 3: Execution & Risk — size positions + build order payloads.

Builds Alpaca order payloads (bracket for per-trade engines, plain
market for momentum-style batch engines). Use ``tpcore.order_ids.build_cid``
to construct the canonical ``<prefix>_<TICKER>_<TS>[_tierN]`` client_order_id
so downstream attribution + reconciliation works correctly.

Raises :class:`tpcore.exceptions.SizingError` when the entry price is
non-positive (rare but possible during corp actions).
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from tpcore.exceptions import SizingError  # noqa: F401 — re-raised in real engines.
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.order_ids import build_cid  # noqa: F401 — used in real engines.

logger = structlog.get_logger(__name__)

DEFAULT_ACCOUNT_CAPITAL = Decimal("10000")


class EngineNameExecutionRisk(BaseEnginePlug):
    """Plug 3 — position sizing + order payload construction."""

    engine_name = "ENGINE_NAME"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {},
        }

    def decide(self, *args, **kwargs):
        """Return ``ExecutionDecision | None`` (``None`` = gate blocked)."""
        raise NotImplementedError
