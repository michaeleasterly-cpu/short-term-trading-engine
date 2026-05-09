"""tpcore — shared platform primitives for STE engines.

Submodules:
    calendar         NYSE (XNYS) calendar helpers (UTC).
    interfaces       Broker, data, and engine plug ABCs + Pydantic models.
    risk             Risk governor (per-engine + platform-wide caps, kill switch).
    aar              After-action report models and writer.
    quality          Data and execution quality scoring + writers.
    parity           Live/paper parity harness.
    backtest         Provider-agnostic backtest harness, credibility rubric, cost model.
    fundamentals     Earnings quality, FCF trend, insider, comps, moat scorecard.
    valuation        DCF, owner earnings, buy bands.
    analysis         Thesis model and validation.
    tax              Lot tracker, wash sale, loss harvester.
    outage           3-tier outage policy.
    scripts          CLI utilities (e.g. check_imports).
"""

__version__ = "0.0.1"
