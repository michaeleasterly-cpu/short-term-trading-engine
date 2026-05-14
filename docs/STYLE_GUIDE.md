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

## Imports

Order: stdlib → third-party → `tpcore` → local engine modules. Within each group, alphabetical (ruff's `I` rule enforces this).

Forbidden direct vendor SDK imports — these are blocked by `tpcore.scripts.check_imports`:
`alpaca_trade_api`, `yfinance`, `fmp_python_sdk`, `praw`, `iborrowdesk`. Reach external services through `tpcore.interfaces.broker.BrokerExecutionInterface` and `tpcore.interfaces.data.DataProviderInterface`.

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
