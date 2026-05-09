"""Investment thesis model.

A thesis must answer three questions:
  1. **Mispricing** — why is the market wrong *today*?
  2. **Catalyst** — what concrete event closes the gap, and on what horizon?
  3. **Thesis-killer** — what observable would *invalidate* the thesis?

A trade without all three is incomplete and should be blocked.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Thesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    mispricing: str = Field(min_length=20, description="Why the market is wrong, in writing.")
    catalyst: str = Field(min_length=10, description="The event that closes the gap.")
    catalyst_horizon: date
    thesis_killer: str = Field(
        min_length=10,
        description="Single observable that, if seen, invalidates the trade.",
    )
    author: str
    created_at: date


def validate_thesis(thesis: Thesis) -> bool:
    """Return True iff the thesis has substantive content in all three required fields.

    Pydantic enforces minimum lengths; this hook is for richer rules
    (e.g. flagging vague phrases like "good company"). TODO: implement.
    """
    return all(
        [
            len(thesis.mispricing.strip()) >= 20,
            len(thesis.catalyst.strip()) >= 10,
            len(thesis.thesis_killer.strip()) >= 10,
        ]
    )
