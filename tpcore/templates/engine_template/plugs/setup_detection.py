"""Plug 1: Setup Detection — scan the universe for setups.

Populates a :class:`tpcore.backtest.filter_diagnostics.FilterDiagnostics`
instance so SIGNAL events carry per-gate pass/block counters. Reuses
shared indicators from :mod:`tpcore.indicators` rather than rolling its
own (see `tpcore/indicators/__init__.py`).

Compliance contract (STYLE_GUIDE.md "Engine plug compliance"):

* subclasses :class:`BaseEnginePlug` and implements both
  ``validate_dependencies`` + ``healthcheck``;
* builds a :class:`FilterDiagnostics` covering every gate and either
  returns it alongside the candidates or attaches it to each candidate
  (see ``momentum/plugs/setup_detection.py:90`` for the canonical
  pattern). The scheduler lifts this onto ``db_log.signal(..., extra_data=...)``.
"""
from __future__ import annotations

import structlog

from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class EngineNameSetupDetection(BaseEnginePlug):
    """Plug 1 — scan universe + emit PhaseAssessments."""

    engine_name = "ENGINE_NAME"

    def validate_dependencies(self) -> bool:
        # TODO: assert required tpcore.indicators / data sources are reachable.
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {},
        }

    def detect(self, *args, **kwargs):
        """Return list of ``PhaseAssessment`` for tickers passing all gates.

        Implementation must build a FilterDiagnostics::

            diag = FilterDiagnostics(universe_total=len(universe))
            for ticker in universe:
                if not _gate1_ok(...):
                    diag.gate1_blocked = (diag.gate1_blocked or 0) + 1
                    continue
                ...
                diag.candidates_passed += 1

        and either attach it to each candidate (Momentum pattern) or
        return it as a second return value so the scheduler can lift it
        onto every ``db_log.signal(...)`` call.
        """
        _diag = FilterDiagnostics(universe_total=0)  # noqa: F841 — template placeholder
        raise NotImplementedError("wire setup_detection.detect for this engine")
