"""Regression test for the AllocatorService default engine set.

Production constructs ``AllocatorService`` WITHOUT passing ``engines=``
(verified: ``scripts/ops.py`` ``cmd_allocate`` and
``scripts/run_allocator.py`` both omit it), so it runs on the
``__init__`` default. That default must reflect the live engine roster
for the weekly inverse-vol pool:

* archived ``sigma`` (archived 2026-05-16) must be ABSENT — otherwise
  the per-engine upsert loop keeps re-inserting a stale ``sigma``
  ``platform.risk_state`` row every run.
* ``sentinel`` must also be ABSENT — it is a defensive macro overlay
  budgeted by ``SentinelCapitalGate`` (fixed 10–20% cap), not the
  inverse-vol pool; it has near-zero AAR history and is scoped OUTSIDE
  the weekly rebalance by CLAUDE.md / MASTER_PLAN.

So the correct default is exactly ``("reversion", "vector",
"momentum")``.

Pure unit test — no DB, no pool needed (``pool=None`` is supported for
the ``__init__``-only path).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tpcore.allocator.service import AllocatorService

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_production_default_engine_set_is_reversion_vector_momentum_catalyst() -> None:
    """Construct EXACTLY as production does (no ``engines=`` kwarg) and
    assert the default managed set is ``("reversion", "vector",
    "momentum", "catalyst")`` — archived ``sigma`` absent, ``sentinel``
    absent. Catalyst joined 2026-05-20 as allocator_eligible=True via
    the autonomous Lab criteria activation (H-S3-12)."""
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]

    assert svc._engines == ("reversion", "vector", "momentum", "catalyst")  # noqa: SLF001


def test_production_default_excludes_archived_sigma() -> None:
    """``sigma`` (archived 2026-05-16) must NOT be in the default managed
    set — its presence is the sole reason a stale ``sigma``
    ``risk_state`` row keeps getting re-upserted every allocator run."""
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]

    assert "sigma" not in svc._engines, (  # noqa: SLF001
        "archived sigma must not be in the allocator's managed set — "
        "the per-engine upsert loop would resurrect its risk_state row"
    )


def test_production_default_excludes_sentinel_by_design() -> None:
    """``sentinel`` is intentionally excluded — defensive overlay
    budgeted by SentinelCapitalGate, not the inverse-vol pool."""
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]

    assert "sentinel" not in svc._engines, (  # noqa: SLF001
        "sentinel is intentionally OUT of the inverse-vol pool "
        "(SentinelCapitalGate owns its budget)"
    )
