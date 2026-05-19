"""Catalyst engine — insider-cluster swing engine (SP-F: Lab front-half proof case).

This engine is the SP-F validation case for SP-B (roster-driven Lab
targeting) and SP-C (Lab Candidate Readiness checklist): a brand-new
engine stood up via ``tpcore/templates/engine_template/`` + ECR-ADD
``promote_new``, targeted by the roster-driven Lab and gated by the
canonical readiness checklist.

Strategy (data-ready leg only — insider-cluster):
    A cluster of distinct Form-4 insider BUYs (≥3 distinct insiders in
    the most recent ``CATALYST_CLUSTER_WINDOW_DAYS`` calendar-day
    window) is the primary signal. Cluster-density is scored by the
    aggregate dollar value of the BUY transactions in the window; the
    top-scoring candidates that also clear the universe-liquidity gate
    + a basic trend filter (close > 50-SMA) are submitted as day-market
    bracket entries.

Scope caveat: the 8-K (material-event) leg is data-gated on item-level
parsing and is OUT OF SCOPE for SP-F. The catalyst engine ships with
the insider-cluster leg only; the 8-K leg can be added later as a
fold_existing Lab candidate once the data layer ships item-level
parsing (a separate, future ECR-MODIFY path).

See:
    * ``docs/superpowers/specs/2026-05-19-lab-front-half-epic.md`` §SP-F
    * ``docs/superpowers/checklists/engine_readiness.md``
    * ``docs/superpowers/checklists/lab_candidate_readiness.md``
"""
from __future__ import annotations
