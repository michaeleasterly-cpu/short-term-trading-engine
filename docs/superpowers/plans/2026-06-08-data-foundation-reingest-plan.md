# Data-Foundation Re-Ingest — Implementation Plan

> Executes `docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-design.md` §0 (re-ingest-from-scratch, contract-first). Heavy-lane. Only ONE destructive step (Phase C1 wipe) — operator-confirmed. "The next run works" = guaranteed by the Phase-A completeness gate (dry-runs the resolver against the real bars) + the log-only→hard reject contract.

**Key architectural fact:** `ticker_history` is a 1:1 projection of `ticker_classifications.lifetime_start/lifetime_end` (`derive_ticker_history` `tpcore/identity/ticker_history_reuse_build.py:79`). So the spine is fixed at the SOURCE columns (`universe_build.py::resolve_lifetime_start:134`), not by patching `ticker_history`. Reused tickers (FB→Meta) require MULTIPLE classifications → multiple windows, + a new ticker-scoped GIST exclusion (today's exclusion is classification_id-scoped only and does NOT prevent cross-entity same-ticker overlap).

## Phase A — Spine build + verify (NON-destructive)
- **A0** Full SEC `submissions.zip` ingest (bulk-first) → `data/sec_submissions/` (today only 922 partial JSONs; `sec_document_type_primary` covers only 2,474/19,004 — must be complete for asset_class authority + reuse splitting).
- **A1** Revise `resolve_lifetime_start` (`tpcore/identity/universe_build.py:134`): SEC FPFD primary; Jan-1-placeholder degeneracy refined by first-price-bar/FMP real-day evidence; SEC wins on real-day conflict. Retires the 4,933 synthetic Jan-1 starts.
- **A2** `universe_build` reused-ticker → MULTIPLE classifications (from SEC CIK-eras + `formerNames` rename boundaries); `lifetime_end` from SEC Form-25/15 → FMP delisting → `KNOWN_DELISTINGS`.
- **A3** New stage `_stage_asset_class_sec_authority`: `sec_document_type_primary` (10-Q/10-K ⇒ stock; 20-F ⇒ adr; SIC 6798 ⇒ reit) overrides the Alpaca guess; set `metadata_source='sec_submissions'`.
- **A4** Run the build chain: `universe_build → issuers_build (writes sec_document_type_primary back) → asset_class_sec_authority → ticker_history_reuse_build → issuer_securities_build`.
- **A5** Extend `evaluate_identity_gate` (`tpcore/identity/identity_gate.py`) with probes P1–P5 and require GREEN:
  - P1 every ticker-bearing classification has ≥1 window (live 0 violators).
  - P2 no cross-entity same-ticker window overlap.
  - **P3 (the make-it-work gate)** every (ticker) with price bars has window coverage spanning its min+max bar date — dry-runs the resolver against the exact bars the re-ingest will write.
  - P4 no synthetic Jan-1 lifetime_start on a ticker-bearing entity with real-day corroboration.
  - P5 FK `ticker_history.classification_id` validated (lands in Phase B).

## Phase B — Contract migrations (ADDITIVE; log-mode = no behavior change)
- `20260608_0100_identity_contract_resolver.py`: `platform.resolve_classification_id(ticker, as_of, supplied, table)` (window-validated; supplied-id branch closes the 2,761 out-of-window writer-bypass) + `identity_contract_log` table + GUC default `platform.identity_contract_mode='log'` (fail-closed to 'hard' if unset).
- `20260608_0200_identity_contract_triggers_logmode.py`: rewrite all 16 `tg_set_classification_id_*` to one-line resolver calls (each passes its own ticker-col + as_of-expr). In 'log' mode they behave exactly as today (NULL-soften) — zero behavior change to legacy data.
- `20260608_0300_ticker_history_fk_and_ticker_exclusion.py`: FK `ticker_history.classification_id` + `ticker_lifecycle_events.classification_id` (ON DELETE RESTRICT); NEW ticker-scoped GIST exclusion `EXCLUDE (ticker WITH =, daterange(valid_from, coalesce(valid_to,'infinity'), '[)') WITH &&)`. Docstrings carry orphan-audit (expected 0) + overlap-audit (expected 0 post-A; if >0, STOP).

## Phase C — Wipe + re-ingest in log-only
- **C1 🔴 DESTRUCTIVE (operator confirm + `--param confirm=WIPE`)** new stage `_stage_wipe_child_tables`: TRUNCATE child tables (prices_daily, fundamentals_quarterly, corporate_actions, earnings_events, sec_periodic_filings, short_interest, insider_transactions, sec_material_events, borrow_rates, liquidity_tiers, spread_observations, social_sentiment, insider_sentiment, universe_candidates, aar_events, prices_daily_staging). **Identity spine + issuer graph + etf_attributes PRESERVED.**
- **C2** Re-ingest each child source through the contract in log-mode (prices from survivorship-free snapshot; fundamentals SEC-scoped to stock/reit; etc.).
- **C3 gate** `SELECT count(*) FROM platform.identity_contract_log` must be **0**. Any row ⇒ spine gap ⇒ fix Phase A + re-run C1–C3. Never lower the contract.

## Phase D — Flip hard + NOT NULL + validate
- `20260608_0400_identity_contract_hard_mode.py`: GUC → 'hard'.
- `20260608_0500_classification_id_not_null.py`: SET NOT NULL on the 7 nullable tables (FKs already present).
- `20260608_0600_data_wired_validation.py`: the "DB is wired" check set (FKs valid, 0 NULL cls everywhere, ticker_history complete, 0 out-of-window, scope holds, row-count floors so empty tables fail not pass) → part of `DATA_OPERATIONS_COMPLETE`.
- D-final: §5 validation + identity gate green.

## Gates / discipline
Each phase = gated heavy-lane PR (split-review + silent-failure-hunter + whole-suite + order-flip). Migrations round-trip (upgrade/downgrade/upgrade) before push. Only C1 is destructive.
