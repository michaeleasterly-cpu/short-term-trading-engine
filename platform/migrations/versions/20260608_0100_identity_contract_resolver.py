"""Identity contract resolver + log table + 15 trigger rewrites (Phase B).

Spec: ``docs/superpowers/specs/2026-06-08-data-foundation-systemic-fix-
design.md`` §0 / §3.1-§3.2 / §6 / §7-A. Plan:
``docs/superpowers/plans/2026-06-08-data-foundation-reingest-plan.md`` Phase B.

WHAT THIS DOES (additive; log-mode default = behavior-neutral on existing data):

  1. Creates ``platform.identity_contract_log`` — the monitored would-reject
     ledger (a row per write that the resolver could not window-resolve while
     in 'log' mode). This is NOT a sidecar/quarantine for *data*: it stores no
     business rows, only a diagnostic trail of unresolved (table, ticker,
     as_of) attempts so a re-ingest can prove zero-gap (Phase C3 gate:
     ``SELECT count(*) FROM platform.identity_contract_log == 0``). The rows it
     describes are NEVER created with a guessed id — in 'log' mode the legacy
     NULL-soften behavior is preserved (NULL passes through, identical to
     today); in 'hard' mode the INSERT is rejected outright.

  2. Creates ``platform.resolve_classification_id(p_ticker, p_as_of,
     p_supplied, p_table)`` — ONE window-validated half-open SCD-2 lookup that
     replaces the 15 copy-pasted trigger bodies. Behavior:
       * If a supplied (caller-stamped) classification_id is non-NULL, it is
         VALIDATED against the ticker's ``ticker_history`` window for ``p_as_of``
         — an out-of-window supplied id is treated as a no-match (closes the
         2,761-row writer-bypass the audit named). A supplied id that IS in the
         window is returned verbatim.
       * Else the resolved id is the ``ticker_history`` window covering
         ``p_as_of`` under the half-open ``[valid_from, valid_to)`` predicate
         (matching the EXCLUDE constraint + invariant D2).
       * On no-match the GUC ``platform.identity_contract_mode`` decides:
           - 'log'  : log to ``identity_contract_log`` + RETURN the supplied
                      value (NULL for an unstamped write) — i.e. exactly the
                      legacy NULL-soften, plus a diagnostic row.
           - 'hard' : RAISE check_violation (the loud reject).
         The GUC is read fail-CLOSED: if unset/empty it defaults to 'hard'.

  3. Rewrites all 15 ``tg_set_classification_id_*`` trigger functions to a
     one-line ``NEW.classification_id := platform.resolve_classification_id(
     <ticker-col>, <as_of-expr>, NEW.classification_id, '<table>')``. Each
     function passes its OWN ticker column + as-of expression (enumerated from
     the LIVE ``pg_proc`` bodies — see ``TRIGGER_FUNCS`` below). The 15 are the
     complete live set (the plan's "16" was an estimate; ``pg_trigger`` shows
     15).

  4. GUC default — SUPABASE CONSTRAINT. The plan called for ``ALTER DATABASE
     ... SET platform.identity_contract_mode = 'log'`` so a normal connection
     inherits log-mode. On the managed Supabase Postgres the connection role
     (``postgres``) is NOT a superuser (``rolsuper=false``) and the
     ``platform.*`` custom variable class is not pre-registered, so
     ``ALTER DATABASE/ROLE ... SET platform.*`` raises
     ``InsufficientPrivilegeError`` (verified 2026-06-08). A persistent
     database-level default is therefore impossible on this platform.

     The resolver FAIL-CLOSES to 'hard' when the GUC is unset (the spec §7-A
     safety invariant), so we do NOT default it to 'log' in code (that would
     silently weaken the contract). Instead log-mode is established
     PER-SESSION by the operating connection with a plain
     ``SET platform.identity_contract_mode = 'log'`` (session GUC writes are
     permitted — verified) at the start of the Phase-C re-ingest. The
     migration documents this; it does not attempt the blocked ALTER (a
     swallowed-failure ALTER would be fake-green). The hard-mode flip
     (Phase D) is likewise session-scoped: simply stop setting the GUC, and
     the fail-closed default takes over.

DOWNGRADE restores the prior per-table NULL-soften bodies verbatim (the live
half-open form), drops the resolver fn + log table, and resets the GUC default.

NO new business table; ``identity_contract_log`` is a diagnostic ledger, not a
data home (migrations-rule schema-rationale: readers = the Phase C3 gate +
ops dashboards; writer = the resolver fn ONLY; the existing identity substrate
[ticker_history SCD-2 + the 15 triggers] is EXTENDED here, not duplicated —
this migration consolidates 15 bodies into 1 resolver).

## Schema rationale (controls-audit §13 #11)

Readers (named code paths that will query the new table):
  - Phase C3 gate: ``SELECT count(*) FROM platform.identity_contract_log`` must
    be 0 after a log-mode re-ingest (plan Phase C3).
  - ops/dashboard would-reject monitoring (the diagnostic trail).

Writers (canonical writer; single-writer unless justified):
  - ``platform.resolve_classification_id`` ONLY (the resolver INSERTs the
    would-reject row in 'log' mode). No application code writes it directly.

Existing-table alternative considered:
  - ``data_quality_log``: rejected — that is the validation-layer detector
    substrate (per-check/per-phase), not a write-time identity-resolution
    trail. The contract log is written synchronously inside the BEFORE INSERT
    resolver, keyed on (table, ticker, as_of, supplied_cid), to gate the
    re-ingest. Conflating the two would couple the write path to the
    validation schema.

Why not extend the existing identity / lifecycle substrate?
  - This migration DOES extend it — the 15 ``tg_set_classification_id_*``
    triggers + the ``ticker_history`` SCD-2 windows are consolidated into ONE
    resolver. The log table is the ONLY new object, and it is a diagnostic
    ledger (no classification_id FK, no business rows), required because the
    half-open window lookup itself has nowhere to record a would-reject.

Revision ID: 20260608_0100
Revises: 20260607_0300
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "20260608_0100"
down_revision = "20260607_0300"
branch_labels = None
depends_on = None


# (table_suffix, ticker_col, as_of_expr) — the COMPLETE live set of 15
# classification_id trigger functions, enumerated from the live pg_proc
# bodies (2026-06-08). Each tuple is the function's own ticker column + as-of
# expression. insider_sentiment uses NEW.symbol + make_date(year,month,1).
TRIGGER_FUNCS: tuple[tuple[str, str, str], ...] = (
    ("aar_events", "ticker", "NEW.recorded_at::date"),
    ("borrow_rates", "ticker", "NEW.date"),
    ("corporate_actions", "ticker", "NEW.action_date"),
    ("earnings_events", "ticker", "NEW.event_date"),
    ("fundamentals_quarterly", "ticker", "NEW.period_end_date"),
    ("insider_sentiment", "symbol", "make_date(NEW.year, NEW.month, 1)"),
    ("insider_transactions", "ticker", "NEW.filing_date"),
    ("liquidity_tiers", "ticker", "NEW.last_updated::date"),
    ("prices_daily", "ticker", "NEW.date"),
    ("sec_material_events", "ticker", "NEW.filing_date"),
    ("sec_periodic_filings", "ticker", "NEW.filing_date"),
    ("short_interest", "ticker", "NEW.release_date"),
    ("social_sentiment", "ticker", "NEW.date"),
    ("spread_observations", "ticker", "NEW.observed_at::date"),
    ("universe_candidates", "ticker", "NEW.as_of_date"),
)


_RESOLVER_FN = r"""
CREATE OR REPLACE FUNCTION platform.resolve_classification_id(
    p_ticker   text,
    p_as_of    date,
    p_supplied text,
    p_table    text
) RETURNS text
LANGUAGE plpgsql
AS $resolver$
DECLARE
    v_mode    text := lower(coalesce(
                   nullif(current_setting('platform.identity_contract_mode', true), ''),
                   'hard'));  -- fail-CLOSED: unset GUC => hard
    v_match   text;
BEGIN
    -- 1. Resolve the window-valid classification_id for (ticker, as_of) under
    --    the half-open [valid_from, valid_to) predicate.
    SELECT classification_id INTO v_match
    FROM platform.ticker_history
    WHERE ticker = p_ticker
      AND valid_from <= p_as_of
      AND (valid_to IS NULL OR p_as_of < valid_to)
    ORDER BY valid_from DESC
    LIMIT 1;

    -- 2. Supplied-id branch: a caller-stamped id is honored ONLY if it equals
    --    the window-valid id (validates against the window; rejects an
    --    out-of-window supplied id — the writer-bypass close).
    IF p_supplied IS NOT NULL THEN
        IF v_match IS NOT NULL AND v_match = p_supplied THEN
            RETURN p_supplied;
        END IF;
        -- supplied id does not match the window => fall through to no-match
    ELSIF v_match IS NOT NULL THEN
        RETURN v_match;     -- unstamped write, window resolved it
    END IF;

    -- 3. No window-valid id (no-match, OR supplied id out-of-window).
    IF v_mode = 'hard' THEN
        RAISE EXCEPTION
            'identity contract: no ticker_history window for ticker=% as_of=% (table=%, supplied=%)',
            p_ticker, p_as_of, p_table, p_supplied
            USING ERRCODE = 'check_violation';
    END IF;

    -- 'log' mode: record the would-reject, then preserve legacy NULL-soften
    -- (return the supplied value — NULL for an unstamped write).
    INSERT INTO platform.identity_contract_log
        (tbl, ticker, as_of, supplied_cid, reason)
    VALUES (
        p_table, p_ticker, p_as_of, p_supplied,
        CASE WHEN p_supplied IS NULL THEN 'no_window_match'
             ELSE 'supplied_id_out_of_window' END
    );
    RETURN p_supplied;
END;
$resolver$;
"""


def _trigger_fn_sql(suffix: str, ticker_col: str, as_of_expr: str) -> str:
    """The one-line resolver-call trigger body (the Phase-B rewrite)."""
    return f"""
        CREATE OR REPLACE FUNCTION platform.tg_set_classification_id_{suffix}()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.classification_id := platform.resolve_classification_id(
                NEW.{ticker_col}, {as_of_expr}, NEW.classification_id, '{suffix}');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


def _legacy_fn_sql(suffix: str, ticker_col: str, as_of_expr: str) -> str:
    """The prior LIVE half-open NULL-soften body (the downgrade restore).

    Verbatim shape from the live pg_proc dump (2026-06-08): no-op-if-NULL
    guard, bare column names, ORDER BY valid_from DESC LIMIT 1, half-open
    upper bound ``as_of < valid_to``.
    """
    return f"""
        CREATE OR REPLACE FUNCTION platform.tg_set_classification_id_{suffix}()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.classification_id IS NULL THEN
                SELECT classification_id INTO NEW.classification_id
                FROM platform.ticker_history
                WHERE ticker = NEW.{ticker_col}
                  AND valid_from <= {as_of_expr}
                  AND (valid_to IS NULL OR {as_of_expr} < valid_to)
                ORDER BY valid_from DESC
                LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


def upgrade() -> None:
    # 1. Diagnostic ledger (would-reject trail; written ONLY by the resolver).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.identity_contract_log (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tbl           text NOT NULL,
            ticker        text NOT NULL,
            as_of         date,
            supplied_cid  text,
            reason        text NOT NULL,
            logged_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS identity_contract_log_tbl_logged_at_idx "
        "ON platform.identity_contract_log (tbl, logged_at)"
    )

    # 2. The unified resolver.
    op.execute(_RESOLVER_FN)

    # 3. Rewrite all 15 trigger functions to the one-line resolver call.
    for suffix, ticker_col, as_of_expr in TRIGGER_FUNCS:
        op.execute(_trigger_fn_sql(suffix, ticker_col, as_of_expr))

    # 4. GUC default — NOT persisted. See the module docstring: the managed
    #    Supabase role cannot ALTER DATABASE/ROLE a custom (`platform.*`)
    #    variable class. The resolver fail-closes to 'hard'; log-mode is
    #    established per-session via `SET platform.identity_contract_mode='log'`
    #    by the Phase-C re-ingest connection. Setting it here would be a no-op
    #    (the migration runs in its own connection) AND is blocked by the
    #    platform; we deliberately do NOT attempt-and-swallow (fake-green).


def downgrade() -> None:
    # No persistent GUC default was set (see upgrade step 4), so nothing to
    # reset. Restore the prior per-table half-open NULL-soften bodies.
    for suffix, ticker_col, as_of_expr in TRIGGER_FUNCS:
        op.execute(_legacy_fn_sql(suffix, ticker_col, as_of_expr))
    # Drop the resolver + log table.
    op.execute(
        "DROP FUNCTION IF EXISTS platform.resolve_classification_id("
        "text, date, text, text)"
    )
    op.execute("DROP TABLE IF EXISTS platform.identity_contract_log")
