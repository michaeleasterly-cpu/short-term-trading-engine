---
name: worktree-isolation-doesnt-cover-shared-infra
description: "Operator 2026-05-23: worktree isolation isolates the CODE CHECKOUT only. The live Supabase DB, the .venv, the FMP API rate-limit, the Alembic-state-vs-main-state alignment — all SHARED across sessions. Treating subagent dispatch with `isolation: worktree` as a moat is false; the DB is the actual blast radius."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 013d8715-40e7-4815-8ac8-ff2d985a3888
---

**Rule:** Worktree isolation isolates the CODE CHECKOUT. It does NOT isolate the SHARED INFRASTRUCTURE that any code touches when it runs. Stop treating `isolation: worktree` as a moat against affecting Carver's session.

**Why (operator 2026-05-23 — failure derived):** I dispatched ~10 subagents today with `isolation: worktree` and told myself I wasn't touching Carver's checkout. Wrong. The shared resources I actually mutated:

1. **Live Supabase database** — every subagent that ran ops stages wrote rows / dropped tables / applied migrations against the same DB Carver's engines read from. Today's mutations:
   - `platform.insider_filings` DROPPED entirely (by killed subagent's migration `20260522_0200_drop_insider_filings_add_sec_mspr.py`, applied LIVE but NOT committed to main)
   - `platform.insider_mspr_daily` CREATED (~130K rows derived from sec_insider_transactions)
   - `platform.prices_daily.source` values silently changed: 2.78M rows re-tagged from tradier→alpaca→fmp across rebuild cycles
   - `platform.ticker_classifications` refreshed (+10 rows, drift accumulated to +46)
   - `platform.macro_indicators` advanced from 2026-05-20 → 2026-05-21 (multiple feeds)
   - `platform.sec_insider_transactions` reloaded (674K rows from bulk Form-345 ETL; idempotent ON CONFLICT but DB-touching)
   - `platform.fundamentals_quarterly` backfilled
   - `platform.earnings_events` advanced 31K → 35K rows
   - `platform.application_log` written to by every stage execution

2. **`.venv` at `/Users/michael/short-term-trading-engine/.venv`** — rebuilt twice during the self-symlink-loop incident. Shared between sessions; rebuilding it disrupts whatever process was using the prior venv (Carver's PID 75510 catalyst probe was running at the time).

3. **FMP API rate-limit budget** — 300 req/min shared across both sessions. Heavy rebuilds (3600s daily_bars, 2400s sec_filings bulk) consumed the operator's daily FMP budget; Carver's catalyst PEAD probe rate-limit-shared with my rebuild.

4. **Alembic state vs main state** — the killed subagent applied migration `20260522_0200_drop_insider_filings_add_sec_mspr.py` to live DB but the file was left UNTRACKED in my worktree. DB Alembic head ≠ main Alembic head. Future `alembic upgrade head` on a fresh checkout will be inconsistent until the migration is either committed or reverted.

5. **GitHub Actions quota** — every PR I pushed runs CI against shared org budget. Operator burned this earlier (2026-05-21 incident → repo public). I added 5+ PRs today.

## How to apply

Before any subagent dispatch or in-thread DB write, ask:
- Does this WRITE to live DB? → Document in `/handoffs/` so Carver sees
- Does this CREATE/DROP a table or APPLY a migration? → That's a schema-altering act; commit the migration FIRST, then run it
- Does this rebuild `.venv` or shared dependencies? → Coordinate / notify
- Does this consume rate-limited shared API budget? → Sequence with the other session's work

**The DB is the blast radius.** Worktree-create only isolates the filesystem; the DB connection from both worktrees points at the SAME `DATABASE_URL`. Treat every DB mutation as cross-session.

**Specific anti-pattern from today:** running a subagent migration against live DB before the migration file is committed/pushed. The DB diverges from main → future restore-from-clean-checkout broken.

## Related

- [[database-architecture-state-2026-05-23]] — the shared DB layout being mutated
- [[never-touch-shared-main-checkout]] — narrower version (filesystem only); this entry is the broader infrastructure-blast-radius rule
- [[git-workflow-commit-push-ci]] — commit-the-migration-before-applying-it

## Worktree isolation OFF when no parallel session (operator 2026-05-23)

After Carver session shutdown, operator 2026-05-23: *"no need for worktrees right now its just you"*. The shared checkout `/Users/michael/short-term-trading-engine/` IS the working tree. Dispatched subagents should NOT use `isolation: worktree` — they run directly in the shared checkout.

**Rule of thumb (going forward):**
- Single-session (current state): NO `isolation: worktree` on Agent dispatches
- Multi-session (if a parallel Carver session restarts): RE-ENABLE worktree isolation per the cross-session rule

**Why not always worktree:**
- `.venv` rebuild cost on every dispatch (~30s + disk space)
- Worktree teardown discipline overhead
- The DB blast-radius issue (per [[worktree-isolation-doesnt-cover-shared-infra]]) means worktree wasn't isolating the actual shared resource anyway

**What worktree IS still good for:** if a subagent's work could interfere with my own in-progress edits in the shared checkout (mid-edit file collision). For pure spec/plan/doc work where the subagent writes NEW files, worktree adds nothing.
