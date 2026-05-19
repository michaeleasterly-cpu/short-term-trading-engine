"""SP-B — the engine-FREE Lab targeting contract.

A runnable engine's ``<engine>.backtest`` exports ONE module-level
``LAB_TARGET = LabTarget(...)`` carrying its parameter-range dict + its
four already-uniform dispatch callables. ``ops.lab.run`` resolves it via
the roster SoT (``tpcore.engine_profile.lab_targetable_engines``) +
``importlib`` — the engine OWNS its Lab declaration; engine add/remove
is an ``_PROFILE`` edit + the engine declaring ``LAB_TARGET``, never Lab
surgery (spec §1, §2.2).

Engine-FREE on purpose: imports only pydantic + stdlib. The dependency
flows engine→tpcore (the engine imports THIS); tpcore NEVER imports an
engine (``check_imports tpcore`` stays green). Lives next to
``tpcore/lab/{ledger,context,models}.py`` — the established engine-free
Lab contract layer (H-S2-1).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict


class LabTarget(BaseModel):
    """Frozen per-engine Lab dispatch contract.

    ``param_ranges`` maps a swept param name → ``(low, high, kind)``
    where ``kind`` is ``"float"`` | ``"int"`` | ``"choice:<csv>"`` — the
    exact ``ops.lab.run._sample_value`` contract (run.py:134-143).
    ``model_post_init`` validates this fail-loud at DECLARATION time so
    a malformed range never defers its error to sample time on a
    live-money-adjacent path (spec §2.2, §8-B5).
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    param_ranges: dict[str, tuple]
    run_for_search: Callable[..., Awaitable[Any]]
    load_window_context: Callable[..., Awaitable[Any]]
    run_with_context: Callable[..., Any]
    default_params: Callable[[], dict[str, Any]]

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        for name, spec in self.param_ranges.items():
            if not isinstance(spec, tuple) or len(spec) != 3:
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] must be a 3-tuple "
                    f"(low, high, kind); got {spec!r}"
                )
            kind = spec[2]
            if not isinstance(kind, str):
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] kind must be str; "
                    f"got {kind!r}"
                )
            if kind in ("float", "int"):
                continue
            if not kind.startswith("choice:"):
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] kind {kind!r} not "
                    f"in 'float'|'int'|'choice:<csv>'"
                )
            # choice:<csv> — _sample_value (run.py) does
            # kind.split(":",1)[1].split(",") then rng.choice(...). An
            # empty CSV ("choice:" / "choice:,") would yield [''] and
            # rng.choice would silently return an empty-string "param
            # value" — silent corruption of what the Lab fishes. Require
            # ≥1 non-empty member, fail-loud at DECLARATION time.
            members = [
                c for c in kind.split(":", 1)[1].split(",") if c.strip()
            ]
            if not members:
                raise ValueError(
                    f"LabTarget.param_ranges[{name!r}] kind {kind!r}: a "
                    f"'choice:' kind needs ≥1 non-empty member "
                    f"(e.g. 'choice:a,b'); an empty choice list would "
                    f"silently sample an empty-string parameter value"
                )


__all__ = ["LabTarget"]
