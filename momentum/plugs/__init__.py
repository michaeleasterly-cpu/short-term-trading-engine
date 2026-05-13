"""Momentum engine plugs — 5-plug architecture.

Pipeline at monthly rebalance:
    SetupDetection (rank top decile)
      → LifecycleAnalysis (is today a rebalance day?)
      → ExecutionRisk (size + diff vs current → order batch)
      → CapitalGate (capital limits + graduation status)
      → AARLogging (one AAR per ticker per rebalance)
"""
