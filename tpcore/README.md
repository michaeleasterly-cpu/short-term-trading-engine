# tpcore

Shared platform primitives consumed by every STE engine.

## Modules

| Path | Purpose |
| --- | --- |
| `calendar.py` | XNYS calendar wrapper (`is_trading_day`, `next_open`, `next_close`, `session_contains`, `next_monday_open`). All UTC. |
| `interfaces/engine_plug.py` | `BaseEnginePlug` ABC — engine name, dependency validation, healthcheck. |
| `interfaces/broker.py` | `BrokerExecutionInterface` ABC + `Order`, `Position`, `AccountInfo` Pydantic models. |
| `interfaces/data.py` | `DataProviderInterface` ABC + `Bar`, `Quote`, `Fundamentals`, `EarningsEvent`. |
| `risk/governor.py` | Per-engine and platform-wide risk caps; emergency kill switch; calendar-aware reset. |
| `aar/` | After-Action Report model + idempotent writer (`platform.aar_events`). |
| `quality/` | Data + execution quality scoring and writers. |
| `parity/harness.py` | Live/paper parity harness — submits identical orders, records drift. |
| `backtest/harness.py` | Provider-agnostic backtest harness. |
| `backtest/credibility.py` | Backtest credibility rubric (0–100). Score < 60 blocks live promotion. |
| `backtest/cost_model.py` | Default 5 bps per-side slippage cost model. |
| `fundamentals/` | Earnings quality, FCF trend, insider, comps, moat scorecard. |
| `valuation/` | DCF (with sensitivity table), owner earnings, buy bands. |
| `analysis/thesis.py` | Thesis model + `validate_thesis` (mispricing, catalyst, thesis-killer). |
| `tax/` | FIFO lot tracker, cross-engine wash sale tracker, tax-loss harvester (Q4 auto-execute). |
| `outage/policy.py` | 3-tier outage policy: INFORMATIONAL / AVAILABILITY / KILL_SWITCH. |
| `scripts/check_imports.py` | AST-based forbidden-import scanner for engine directories. |
| `data/ingest_alpaca_bars.py` | Bootstrap script for the daily-bars universe (active + delisted). |

## Ground rules

- All datetimes UTC. All session dates are NYSE.
- Engines depend only on `tpcore.interfaces.*` — never on a vendor SDK.
- `check_imports` blocks `alpaca_trade_api`, `yfinance`, `fmp_python_sdk`, `praw`, `iborrowdesk`.
