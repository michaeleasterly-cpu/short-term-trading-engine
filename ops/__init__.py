"""Ops daemons + orchestration entrypoints (engine_service,

engine_dispatch, ingestion_engine, platform_pipeline, cron_*).

Regular package (not a namespace dir) so ``from ops.<mod> import …``
resolves DETERMINISTICALLY: a real-package hit at the repo root
short-circuits import resolution before the unrelated ``scripts/ops.py``
data-ops CLI (which shares the top-level name ``ops``) can be picked up.
Also keeps ``python -m ops.<mod>`` working for the launchd daemons.
"""
