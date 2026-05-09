"""Live/paper parity harness — submits identical orders to both endpoints and logs drift."""

from .harness import LivePaperParityHarness, ParityDriftRecord

__all__ = ["LivePaperParityHarness", "ParityDriftRecord"]
