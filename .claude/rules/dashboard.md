---
name: dashboard
paths:
  - "dashboard.py"
  - "dashboard_components/**"
description: "Path-scoped rule: never import dashboard.py in a CI test (streamlit not in CI venv); pure dashboard_components only."
---

# Dashboard

Canonical SoT: `dashboard.py` (top-level Streamlit entry) + `dashboard_components/` (pure render-only panels).
Authoritative external: <https://code.claude.com/docs/en/extend>.

Hard rule:

- **NEVER `import dashboard.py` from a CI test.** `streamlit` is not in `pip install -e .[dev]`; importing it breaks CI collection. The memory `feedback_ops_package_shadow_full_suite_gate` is explicit.
- **Tests target `dashboard_components/` ONLY** (pure render functions; no Streamlit runtime).
- **`scripts/run_dashboard.sh`** is the canonical launcher.
- **Health-tab panels are read-only** (e.g. `render_defect_register` recomputes nothing; no write buttons — per #254 spec §5 OUT).
- The escalation & integrity audit panel (Health tab, 2026-05-18) surfaces the existing SoT (Ladder / Data Supervisor holds / `cross_table_audit.*` state / recent data escalations) — read-only, no predicate recomputation.

The `--check` mode of the dashboard carries 19 probes (incl. `missed_data_operations`, `supabase_backup`, `disk_space`, `trade_monitor_heartbeat`, `macro_indicators_freshness`); changes to it follow the heavy lane only when the underlying `tpcore/quality/validation/` is touched, otherwise default lane.

#189 dashboard refactor: input is `design_handoff_trading_console/` (Claude-design handoff); follows full brainstorm→spec→plan→build because it's a UX rewrite. Sequenced DEAD LAST in the master program.
