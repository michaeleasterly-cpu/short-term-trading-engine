"""Allocator service — cross-engine capital allocation.

Per MASTER_PLAN §5: equal-risk-weighted (inverse realized volatility),
performance-chasing rejected, primary value is freezing engines in
persistent drawdown. Spec hardened 2026-05-13 via expert architecture
review.

Cadence: weekly, Monday pre-open (~13:00 UTC).
Bootstrap: 25% per engine until each has ≥20 AARs.
Weighting: w_i ∝ 1/σ_i, normalized over non-frozen engines, clipped to [0.10, 0.50].
Freeze: soft (DD ≥ 15%), hard (DD ≥ 25% OR 30 sessions soft).
Paper mode: freeze state recorded to allocations, kill_switch NOT enforced.
"""

from tpcore.allocator.service import AllocationDecision, AllocatorService

__all__ = ["AllocatorService", "AllocationDecision"]
