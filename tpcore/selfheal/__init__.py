"""Generic data self-heal — one engine, N declarative per-feed specs.

Architecture mandate (TODO.md "Autonomous self-heal — EVERY data
source"): self-heal is a GENERIC tpcore capability, not per-source
bash. The validation suite is the detector; this package is the
healer, in the same layer. ``run_data_operations.sh`` is a thin caller
of :mod:`tpcore.selfheal.__main__`.

Adding a data feed = register one :class:`HealSpec` (plus its
validation check + the stage's bounded repair mode). Zero bash edits,
zero new branches — see ``docs/superpowers/checklists/data_feed_readiness.md``.
"""
from __future__ import annotations

from .orchestrator import SelfHealOutcome, run_self_heal
from .registry import HEAL_SPECS, spec_for
from .spec import HealSpec

__all__ = [
    "HEAL_SPECS",
    "HealSpec",
    "SelfHealOutcome",
    "run_self_heal",
    "spec_for",
]
