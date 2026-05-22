"""After-Action Reports — structured per-trade postmortems."""

from .classifier import classify_exit_reason
from .deferred import AAR_DEFERRED_EVENT, DeferredAARWriter, replay_deferred_aars
from .models import AfterActionReport, ExitReason
from .reader import AARReader, AARRow
from .writer import AARWriter

__all__ = [
    "AAR_DEFERRED_EVENT",
    "AARReader",
    "AARRow",
    "AARWriter",
    "AfterActionReport",
    "DeferredAARWriter",
    "ExitReason",
    "classify_exit_reason",
    "replay_deferred_aars",
]
