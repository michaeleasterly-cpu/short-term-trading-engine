"""Canary predicate — pure list filtering, no DB.

Determines whether an LLM-proposed spec has been promoted to active
(graduated from shadow mode) based on the event stream.
"""
from __future__ import annotations


def is_promoted(events: list[dict], spec_key: str) -> bool:
    """Return True iff any event has event_type == 'LLM_SPEC_PROMOTED'
    and data['spec_key'] == spec_key."""
    for event in events:
        if (event.get("event_type") == "LLM_SPEC_PROMOTED"
                and (event.get("data") or {}).get("spec_key") == spec_key):
            return True
    return False


def shadow_decision(spec_key: str, events: list[dict]) -> str:
    """Return 'active' if the spec_key has been promoted, else 'shadow'."""
    return "active" if is_promoted(events, spec_key) else "shadow"


__all__ = ["is_promoted", "shadow_decision"]
