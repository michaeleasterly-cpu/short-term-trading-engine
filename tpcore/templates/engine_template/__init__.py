"""Engine template — copy this directory to ``<engine_name>/`` and rewire.

See ``docs/superpowers/checklists/engine_readiness.md`` for the pre-merge
checklist. See the module docstring in ``order_manager.py`` for the
high-level flow.

Search-and-replace the following placeholders project-wide after copying:

    ENGINE_NAME    →  <engine_name>           (e.g. ``catalyst``)
    EngineName     →  <EngineName>            (e.g. ``Catalyst``)
    ENGINE_PREFIX  →  <2-char prefix>          (e.g. ``ca``)

Then register ``ENGINE_PREFIX`` in :mod:`tpcore.order_ids` so the order
manager and downstream consumers can attribute orders to this engine.
"""
from __future__ import annotations
