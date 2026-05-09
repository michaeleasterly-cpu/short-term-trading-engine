# Glossary

plug: One of five standardized modules inside each engine.
sigma: Range scalping engine (daily Bollinger Bands, ADX, stochastic).
reversion: Statistical mean reversion engine (Z-score, RSI extremes).
vector: Momentum swing engine (multi-day trend, catalyst overlay).
s2: Short squeeze engine (satellite, rare setups).
catalyst: Event-driven engine (post-earnings drift only).
sentinel: Macro inverse engine (reformed basket: SH, PSQ, TLT, GLD, SQQQ).
tpcore: Trading Platform Core — shared library for all engines.
allocator: Capital allocation service (equal-risk-weighted).
forensics: Trade analysis service (formerly Coroner).
settlement: Annual distribution + tax reporting service (formerly Harvester).
pit: Point-in-time — data as it was known on a specific historical date, not retroactively adjusted.
survivorship bias: The error introduced when backtests exclude delisted stocks.
parity harness: System that compares paper fills to live fills.
bracket order: A parent order with linked take-profit and stop-loss legs (Alpaca order_class=bracket).
