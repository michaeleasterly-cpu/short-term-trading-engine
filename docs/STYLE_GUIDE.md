# Style Guide

This guide exists so every Claude session writes code that looks like every other session wrote it.

For terms (engine names, score names, service names) the source of truth is `docs/glossary.md`. For architecture and the build order, the source of truth is `docs/MASTER_PLAN.md`. This file covers code style only.

## Language and tooling

- **Python:** 3.11+. Use modern syntax (`X | None`, `list[T]`, `match`/`case`, structural type hints). No `from typing import Optional, List, Dict, Tuple` — use builtins.
- **Type hints:** required on every function signature, every method, every public attribute. `mypy` should pass cleanly.
- **Formatting & linting:** `ruff` (config below). Run `ruff check --fix` and `ruff format` before committing. No black, no isort, no flake8.

## Data models

- **Pydantic v2 only** for any struct that crosses a plug boundary, gets logged, persisted, or returned from a public function.
- Every model declares `model_config = ConfigDict(extra="forbid")`. Frozen iff truly immutable.
- No `@dataclass` for inter-plug structs. Plain dataclasses are acceptable only for purely internal, never-serialized helpers.
- Money is `decimal.Decimal`. Quantities are `int`. Never `float` for prices, sizes, or P&L.
- **Exception:** Backtest research scripts may use `float` for prices and P&L within NumPy/pandas pipelines. Production engine code must use `Decimal`.

## Logging

- **`structlog` only.** No `print()`. No `import logging` directly.
- `logger = structlog.get_logger(__name__)` at the top of each module.
- Event names are dotted, lowercase, namespaced by engine and plug, e.g. `sigma.exec.position_cap_hit`. All other context goes in keyword args, not the event string.
- **Exceptions:**
  - Standalone one-shot scripts under `scripts/` may use the stdlib `logging` module.
  - `tpcore.trade_monitor` may import stdlib `logging` directly. Reason: this module is run as `python -m tpcore.trade_monitor` (not as a file) because invoking the file directly puts `tpcore/` on `sys.path`, which then makes the bootstrap `import logging` resolve to the project's `tpcore.logging` package and crash with a circular-import error. The `-m` invocation sidesteps that. The module-level docstring documents the workaround.

## Time

- All timestamps are timezone-aware UTC. `datetime.now(UTC)`, never `datetime.utcnow()` (deprecated and naive).
- Market open/close, trading sessions, holidays: `tpcore.calendar` (which wraps `exchange_calendars` NYSE). No hardcoded `"America/New_York"` strings, no manual holiday lists.
- Persisted timestamps in Postgres are `TIMESTAMPTZ` (see `platform/README.md`).

## Engine layout

Every engine is a top-level package with this exact shape:

```
<engine>/
    __init__.py          # re-exports the five plugs and the models
    models.py            # Pydantic v2 models shared between plugs
    plugs/
        __init__.py
        setup_detection.py
        lifecycle_analysis.py
        execution_risk.py
        aar_logging.py
        capital_gate.py
    tests/
        __init__.py
        test_<engine>_plugs.py
```

Five plugs, no more, no fewer. Each inherits from `tpcore.interfaces.engine_plug.BaseEnginePlug` and exposes `validate_dependencies()` and `healthcheck()`.

**Shared tpcore reuse (no duplication).** Engines must source the following from `tpcore/` rather than reimplementing them locally:

- Indicators — `tpcore.indicators.{compute_adx, compute_bbands, compute_chop}`. No engine-local indicator code.
- OrderManager scaffolding — per-trade engines inherit from `tpcore.order_management.BaseOrderManager` (provides `__init__`, `_persist_tier1_to_open_orders`, `_fetch_recent_orders`). Each subclass sets `ENGINE_ID` and implements `submit_decision` + `reconcile`.
- Sizing exception — `tpcore.exceptions.SizingError`. Don't redeclare in engine code.
- Per-trade graduation stats — `tpcore.models.graduation.PerTradeGraduationStats`. Subclass to add engine-specific fields (e.g. Reversion adds `profit_factor`).
- Client-order-id construction — `tpcore.order_ids.build_cid`. Parsing — `tpcore.order_ids.parse_cid`. The engine's prefix must be registered in `tpcore.order_ids.ENGINE_PREFIX`.

**Engine template + checklist.** New engines start from `tpcore/templates/engine_template/` and must satisfy `docs/superpowers/checklists/engine_readiness.md` before merging. The checklist enforces the five plugs, the shared tpcore reuse rules above, the risk/capital gating composition, the AAR + filter-diagnostics wiring, and the daemon integration contract.

## Imports

Order: stdlib → third-party → `tpcore` → local engine modules. Within each group, alphabetical (ruff's `I` rule enforces this).

Forbidden direct vendor SDK imports — these are blocked by `tpcore.scripts.check_imports`:
`alpaca_trade_api`, `yfinance`, `fmp_python_sdk`, `praw`, `iborrowdesk`. Reach external services through `tpcore.interfaces.broker.BrokerExecutionInterface` and `tpcore.interfaces.data.DataProviderInterface`.

## Engine plug compliance (recurring-gap prevention, 2026-05-15)

The six rules below were broken by the Sentinel engine's first build and
caught by the 2026-05-15 compliance audit. They're now non-negotiable
for every engine — present in the engine readiness checklist (§10),
verified by `grep` at PR review, and seeded into
`tpcore/templates/engine_template/` so a copy-paste start satisfies them
by construction.

- **Every plug subclasses `BaseEnginePlug`.** Import from
  `tpcore.interfaces.engine_plug`. Implement `validate_dependencies() ->
  bool` and `healthcheck() -> dict` on every one of the five plugs —
  not just AARLogging. The dashboard and `ops/engine_service`
  health-probe both call these.
- **Populate `FilterDiagnostics` and attach to SIGNAL events.** Build a
  `tpcore.backtest.filter_diagnostics.FilterDiagnostics` in Setup
  Detection covering every gate the engine uses; pass
  `extra_data={"filter_diagnostics": diag.model_dump(exclude_none=True)}`
  to `DBLogHandler.signal(...)` in the scheduler. "Why didn't a signal
  fire?" depends on these counters.
- **Backtest persists the credibility rubric.** Call
  `tpcore.backtest.statistical_validation.write_credibility_score(pool,
  engine_name=..., score=result.credibility_rubric)` at the end of every
  backtest run. Without the row in `platform.data_quality_log`,
  `tpcore.backtest.credibility.graduation_ready` returns False — the
  capital gate can never approve graduation regardless of trade
  performance.
- **Scheduler gates on the trading calendar.** Call
  `tpcore.calendar.is_trading_day(as_of_dt)` near the top of `run_once`
  (after kill-switch, before any DB work). Return early on weekends and
  market holidays — do not query DB or submit orders.
- **AAR uses `classify_exit_reason`, never a hardcoded `ExitReason`.**
  Import `tpcore.aar.classify_exit_reason`; use it as the default value
  when an explicit `exit_reason` isn't passed. Even portfolio-allocation
  engines (no TP/SL on positions) should default through the classifier
  — it returns `TIME_STOP` for missing brackets, the canonical fallback.
- **Cancel your own stale orders before submitting new ones.** Mirror
  `MomentumScheduler._cancel_stale_momentum_orders` — filter by your
  engine's client_order_id prefix from
  `tpcore.order_ids.ENGINE_PREFIX`, cancel any still-open ones, then
  submit. Skipping this leaves positions `held_for_orders` and the next
  rebalance's sells are rejected.
- **Add the engine to `scripts/run_smoke_test.sh`.** That script's step
  3 is the canonical per-engine scheduler-dry-run gate before paper-
  trading. New engines go into the `for engine in ...; do` loop at
  build time — not after the operator asks. Without this line a
  cross-engine refactor that breaks the engine's scheduler won't
  surface in the smoke check.
- **Add the engine to `scripts/run_all_engines.sh`** AND update
  `ops/platform_pipeline.py`'s docstring engine list. That script is
  what the `engine-service` daemon invokes after
  `DATA_OPERATIONS_COMPLETE`; an engine omitted from it never runs
  live. Update both the script loop and the platform-pipeline
  docstring's enumerated engine roster in the same commit.
- **Scheduler emits STARTUP + SHUTDOWN events.** Call
  `await db_log.startup()` right after the `try:` block opens and
  `await db_log.shutdown(duration_ms=..., exit_code=...)` from the
  `finally:` block. `DBLogHandler` exposes both helpers (see
  `tpcore/logging/db_handler.py`). Without these, daemon liveness
  probes and the dashboard's "recent runs" panel can't see the
  engine.
- **`scripts/pipeline_smoke_test.py` is Tier-2-cascade-specific.** It
  asserts the trade-monitor's OCO bracket cascade works for per-trade
  engines (sigma/reversion/vector). Portfolio-allocation engines
  (momentum / sentinel, no Tier 2 cascade) do NOT belong in that
  smoke. The engine readiness checklist asks you to confirm which
  category applies.
- **Register critical tickers in
  ``tpcore/quality/validation/checks/prices_daily_freshness.py``.** Any
  ticker the engine *requires* (regime gates, basket members,
  market-context proxies) must be in ``CRITICAL_TICKERS``. The check
  fires on any registered ticker stale > 5 days. The general
  ``row_integrity`` and ``delistings`` checks do NOT catch per-ticker
  silent refresh drops — that's what motivated the dedicated check
  (SPY incident, 2026-05-15). Without registration, an engine can
  silently break when its key data feed stops refreshing while the
  rest of the universe stays current.

When a new engine surfaces a compliance pattern the checklist doesn't
cover yet, extend this section and §10 of the checklist together.

## Private-attribute access on tpcore classes

**Never access private attributes (`._store`, `._pool`, etc.) on tpcore classes from engine code.** Use the public accessors. If a public accessor doesn't exist for what you need, extend the tpcore class with one — don't add a `# noqa: SLF001` marker.

Canonical examples (added 2026-05-14 as the reference pattern):

- `RiskGovernor.state_for(engine_id) -> RiskState | None` — async read-only peek. Replaces `governor._store.get(engine_id)`.
- `AARWriter.pool -> asyncpg.Pool | None` — read-only property. Replaces `aar_writer._pool`.

The audit that motivated these (`docs/superpowers/pipelines/data_adapter_pipeline.md` cross-references it) found 12 `# noqa: SLF001` sites across order managers + schedulers all reaching into the same two internals. Every one of those sites had a clear public-API gap as the root cause. The fix was the public accessor, not the noqa.

When you find a similar pattern in future audits: extend the tpcore class with a public accessor, replace every consumer site, drop the noqa.

## Naming

Engine, score, and service names must match `docs/glossary.md`. Never re-introduce deprecated names (Creeper, Swinger, Grifter, Fader, Easterly, Grift Score, Creep Score, Commander, Coroner, Harvester).

The class name `TaxLossHarvester` (in `tpcore/tax/`) is allowed: "tax-loss harvesting" is industry-standard finance terminology for the feature, and is distinct from the deprecated *service* called Harvester (now Settlement). See `docs/glossary.md`.

## Docstrings

Google style. One-line summary on the first line, blank line, then optional `Args:`, `Returns:`, `Raises:` sections. Triple double-quotes. Keep them short — explain *why* this exists, not *what* the code obviously does.

## Error handling

- **Fail loud.** Never `except Exception: pass`. Catching is a deliberate decision, always with a structlog event explaining what was swallowed and why.
- For data-source outages, route through `tpcore.outage.classify_outage` and act on the returned `OutageTier`: `INFORMATIONAL` → log only; `AVAILABILITY` → degrade (skip the feed); `KILL_SWITCH` → call `RiskGovernor.emergency_kill()`. Don't invent ad-hoc severity levels.
- **External-API retries: use `@with_retry` from `tpcore.outage`.** Never write a local retry loop (`await asyncio.sleep(...)` inside a `while True` is a code smell). The decorator handles exponential backoff, `Retry-After` headers, jitter, and 429/5xx classification. 4xx-not-429 is permanent — don't retry it.
- Engine-local error types (e.g. `SizingError`) live next to the code that raises them. Don't leak vendor SDK exceptions across the plug boundary.
- **Every trade path** must call `tpcore.risk.RiskGovernor.check_trade()` before submitting. There are no exceptions to this rule.

## New data adapters

Every new external-API adapter starts from `tpcore/templates/adapter_template.py` and must satisfy the five-stage [`Data Adapter Pipeline`](superpowers/pipelines/data_adapter_pipeline.md) (ingest, test, validate, dashboard, schedule) before merging. The pipeline contract is enforced by the pre-merge [`adapter_readiness.md`](superpowers/checklists/adapter_readiness.md) checklist, which covers retry, logging, configuration, interface compliance, testing, and rate limiting. Existing adapters that need refactoring should be brought into compliance one at a time, not piecemeal — see the compliance matrix in the pipeline doc.

## Testing

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).
- One `test_<plug>.py` per plug, plus an end-to-end pipeline test per engine.
- Mock external APIs via the `DataProviderInterface` / `BrokerExecutionInterface` ABCs. Never hit live Alpaca or FMP from tests or CI.
- Deterministic fixtures: synthetic bars, frozen dates, fixed seeds. No "flaky tests are fine" — fix or delete.

## Git hygiene

This repo recurrently accumulates stale remote-tracking refs, leaked local branches (`[gone]` upstream), and orphaned worktree admin entries. The method below is durable + reviewed (replaces one-off `git config --local` per clone):

- **Navigate with `git switch`, never `git checkout <sha|branch>`.** Use `git switch <branch>` to move and `git switch -c <branch>` to create+switch. `git switch` refuses to silently detach HEAD; a subagent's `git checkout <sha>` once produced a detached-HEAD incident. `git checkout` for *file restore* (`git checkout -- <path>`) is still fine; it's branch/commit *navigation* that is banned.
- **Never run real `git`/`gh` against the working repo from tests or code.** A test that ran real `git`/`gh` once leaked branches into a live daemon's `llm-triage/<ref>` namespace (PR #61). Two sanctioned patterns: (a) fabricate a throwaway repo entirely in `tmp_path` and drive the code there via `subprocess` with `cwd=` that throwaway (see `scripts/tests/test_git_hygiene.py`); or (b) inject a fake runner AND add a host-repo guard that **fails loud** — it must be incapable of a silent false-negative (if git can't run it must ERROR, never return `[]`; see `tests/test_llm_data_triage_agent.py::test_leak_guard_fails_loud_when_git_absent`).
- **`scripts/git_hygiene.sh` is the only canonical cleanup — no ad-hoc destructive git.** `--init` idempotently sets `fetch.prune true` + `gc.worktreePruneExpire 3.days.ago` (it is the reproducible source of that config across clones); `--dry-run` (the safe default) shows what would be pruned/deleted and changes nothing; `--apply` runs `git fetch --prune`, deletes ONLY local branches that are BOTH `[gone]` upstream AND merged into `main` (`git branch -d`, never `-D`; never `main`, never the current branch, never an unmerged branch), and `git worktree prune -v`. No `git remote prune`/branch-delete by hand.

## Ruff configuration

This is what `pyproject.toml` already has — keep it in sync if you change it:

```toml
[tool.ruff]
line-length = 110
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
ignore = ["E501"]
```

`E` = pycodestyle errors, `F` = pyflakes, `I` = isort, `B` = bugbear, `UP` = pyupgrade. `E501` (line length) is ignored because `ruff format` already enforces the 110 limit and occasional overruns in long log strings are acceptable.
