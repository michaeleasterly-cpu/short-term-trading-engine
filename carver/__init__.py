"""Carver — Carver-method multi-forecast vol-targeted monthly equity portfolio engine.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` for the design,
``docs/superpowers/plans/2026-05-20-carver.md`` for the build plan, and
``docs/superpowers/checklists/engine_readiness.md`` for the gate.

Lifecycle: registered in ``tpcore.engine_profile._PROFILE`` as
``LifecycleState.LAB``. Graduation to PAPER is an automated ECR-MODIFY
once ``python -m ops.lab --target-engine carver`` produces a Lab Dossier
that clears DSR >= 0.95 and credibility >= 60.

Topology: 5-plug batch-monthly engine (the closest parity engine is
``momentum``). No per-name TP/SL between rebalances; risk is managed via
volatility-target sizing + IDM-bounded diversification + a 12-flips-per-
year speed limit. Order layer is day-market; engine prefix ``cv_``.
"""
from __future__ import annotations
