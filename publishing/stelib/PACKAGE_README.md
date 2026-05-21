# stelib

**stelib** is a Python library that packages the shared trading-engine
primitives developed for the
[short-term-trading-engine](https://github.com/) research project. It
is a carve of the most reusable modules — risk governance, after-action
reports, parity harnessing, backtest scaffolding, the Lab n-trials
ledger, forensics, indicators, tax-lot tracking, and the abstract
engine/broker/data interfaces — into a standalone, pip-installable
distribution.

The carve is deliberately conservative: only modules that compose
cleanly without a live database connection, broker SDK, or LLM
provider are included. The full repo wires these together against
Postgres, an execution venue, and an Anthropic-backed research LLM;
`stelib` exposes the primitives so other systems can adopt them
piecemeal.

- **License:** Apache-2.0
- **Python:** 3.11+
- **Status:** Alpha (SemVer 0.1.0). API may change before 1.0.

## Install

```bash
pip install stelib
```

## What's in the box

| Subpackage          | Purpose                                                          |
| ------------------- | ---------------------------------------------------------------- |
| `stelib.risk`       | RiskGovernor: per-engine + platform-wide gross/net/dd/loss caps  |
| `stelib.aar`        | After-action report models, classifier, reader, writer           |
| `stelib.parity`     | Live/paper parity harness + provider data-parity comparator      |
| `stelib.backtest`   | Backtest harness, credibility rubric, CSCV-PBO overfitting, Monte Carlo, cost model, sensitivity, equivalence checks |
| `stelib.lab`        | Lab n-trials ledger contract, target metric, models, isolation contextvar |
| `stelib.forensics`  | Drawdown / loss-cluster / outlier-loss detectors over AAR streams |
| `stelib.indicators` | ADX, Bollinger Bands, Choppiness Index, Fear & Greed             |
| `stelib.order_management` | BaseOrderManager + stale-order canceller                   |
| `stelib.interfaces` | Broker, data, engine-plug abstract interfaces + Pydantic models  |
| `stelib.tax`        | Tax-lot tracker, wash-sale detector, loss harvester              |
| `stelib.calendar`   | NYSE (XNYS) calendar helpers, always UTC                         |
| `stelib.errors`, `stelib.exceptions`, `stelib.order_ids` | Cross-cutting error types and the engine-prefix client-order-id scheme |

## Why these primitives matter

- **n_trials ledger** (`stelib.lab.ledger` contract via
  `stelib.lab.target` / `stelib.lab.models`) — the deflated-Sharpe
  bookkeeping that prevents engine-spec searches from inflating
  their effective trial count across sessions. Designed for
  Lopez de Prado-style DSR / PBO discipline.
- **RiskGovernor** (`stelib.risk`) — fail-closed pre-trade gate with
  per-engine and platform-wide caps, kill-switch, batch order
  routing, and an in-memory + (pluggable) persistent state store.
- **Backtest credibility rubric + CSCV-PBO** (`stelib.backtest`) —
  reproducible scoring of a backtest's strength against trial-count
  inflation, with diagnostics suitable for graduation gating from
  paper to live.
- **Live/paper parity harness** (`stelib.parity`) — every live
  decision shadow-submitted to the paper venue and reconciled for
  drift; the daily/weekly parity report drives broker-side defect
  detection.
- **AAR + forensics** (`stelib.aar`, `stelib.forensics`) — structured
  per-trade postmortem records plus pattern detectors (drawdown
  windows, loss clusters, σ-outlier losses) for systematic incident
  triage rather than vibes-based review.

## Usage example

```python
from datetime import UTC, datetime

from stelib.risk import InMemoryRiskStateStore, RiskGovernor, RiskLimits

limits = RiskLimits(
    max_open_positions=8,
    max_daily_loss_usd=500.0,
    max_drawdown_pct=0.08,
)
governor = RiskGovernor(
    engine_name="my_engine",
    limits=limits,
    state_store=InMemoryRiskStateStore(),
)

decision = governor.check_intent(
    intent_usd=2_500.0,
    side="buy",
    now=datetime.now(UTC),
)

if decision.allowed:
    submit_order(...)
else:
    log_block(decision.reason_code, decision.detail)
```

```python
from stelib.indicators import compute_chop, CHOP_SIDEWAYS_STRONG

chop = compute_chop(bars_df)  # per-bar Choppiness Index
sideways = chop.iloc[-1] >= CHOP_SIDEWAYS_STRONG
```

```python
from stelib.calendar import next_open, is_session

# Always UTC; XNYS calendar.
session_open = next_open(datetime.now(UTC))
```

## What's intentionally *not* in the carve

- The Anthropic-backed Lab LLM spec emitter
  (`tpcore.lab.llm_emitter`) — vendor SDK coupling.
- The asyncpg pool builder (`tpcore.db.build_asyncpg_pool`) and
  the runtime-attached pieces that depend on it
  (`tpcore.lab.context.LabContext`, the asyncpg-backed
  `DataQualityWriter`/`capital_gate` validation modules) —
  runtime DB dependency. Pool-aware type annotations in the
  carved modules have been replaced with `typing.Any`, so the
  modules import cleanly without `asyncpg` installed; callers
  who want full asyncpg type hints should pull from the source
  repo.
- Engine code (the per-strategy implementations) and the
  platform/data-ingestion services.

## Dependencies

```
pydantic>=2
numpy
pandas
scipy
exchange_calendars
structlog
python-dotenv
```

## Versioning

Semantic Versioning. The library is at **0.1.0** — expect breaking
changes between minor releases until **1.0.0**.

## Contributing & support

This package is published as a static carve from a private research
monorepo. Issues and PRs go to the source repo (see Source URL).

## License

Apache License, Version 2.0. See `LICENSE` for the full text.
