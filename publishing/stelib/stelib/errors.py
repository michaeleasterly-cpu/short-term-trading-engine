"""Universal engine error types.

Cross-cutting errors raised by shared tpcore primitives (batched
fetchers, recovery middleware) so every engine can catch + handle
them the same way. Engine-specific exceptions stay in their own
packages.
"""

from __future__ import annotations


class TpcoreError(Exception):
    """Base for every tpcore-raised error."""


class UniverseTooLargeError(TpcoreError):
    """Raised when a batched fetch can't complete even after the
    recovery decorator's auto-shrink retry.

    The platform's contract: shrink the universe at the caller and try
    again, OR raise so the scheduler exits non-zero (so a daemon /
    cron run sees the failure rather than producing partial data).

    Don't suppress — the operator needs to see this in the dashboard's
    "Last ops --update" red row, not have it silently masked.
    """

    def __init__(self, *, ticker_count: int, attempt: int, original: BaseException) -> None:
        self.ticker_count = ticker_count
        self.attempt = attempt
        self.original = original
        super().__init__(
            f"batched fetch failed after {attempt} attempt(s) at "
            f"ticker_count={ticker_count}: {type(original).__name__}: {original}"
        )


__all__ = ["TpcoreError", "UniverseTooLargeError"]
