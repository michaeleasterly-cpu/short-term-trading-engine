"""Engine SDLC SP3 — the Engine Change Request planner/executor.

Engine-touching orchestration (reads tpcore.engine_profile, rewrites the
_PROFILE literal, moves/scaffolds engine packages, reads Lab dossiers):
LEGAL only in ops/ — exempt from the check_imports tpcore∌engine scan
(SP2 H-S2-1 precedent, parity with ops/lab/__init__.py). NEVER wired
into any daemon/dispatch — a one-shot operator tool, like ops.lab.
"""
