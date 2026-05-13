"""After-Action Reports — structured per-trade postmortems."""

from .classifier import classify_exit_reason
from .models import AfterActionReport, ExitReason
from .reader import AARReader, AARRow
from .writer import AARWriter

__all__ = [
    "AARReader",
    "AARRow",
    "AARWriter",
    "AfterActionReport",
    "ExitReason",
    "classify_exit_reason",
]
