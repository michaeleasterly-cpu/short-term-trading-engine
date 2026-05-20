"""DFCR — frozen pydantic-v2 contract + strict parser.

The fenced ``DATA FEED CHANGE REQUEST`` block in
``docs/superpowers/checklists/data_feed_change_request.md`` is the wire
format. ``parse_dfcr`` is the single entry point: a request that does
not parse is rejected with the EXACT reason, never best-effort-
interpreted (mirrors ECR ``parse_ecr`` discipline).

This MVP covers ADD only. REMOVE / MODIFY parsing exists at the
model level for forward-compat, but the planner only routes ADD;
REMOVE/MODIFY raise NotImplementedError at apply-time with a clear
deferred-feature message.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class DFCRAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"


# Which DFCR keys are valid for which action (mirrors ECR spec §2.2).
_COMMON = {"operation", "feed"}
_ADD_KEYS = {"kind", "provider", "adapter", "derived_from", "need", "cadence"}
_REMOVE_KEYS = {"disposition", "reason"}
_MODIFY_KEYS = {"change", "reason"}
_KEYS_FOR = {
    DFCRAction.ADD: _ADD_KEYS,
    DFCRAction.REMOVE: _REMOVE_KEYS,
    DFCRAction.MODIFY: _MODIFY_KEYS,
}
_ALL_KEYS = _COMMON | _ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS


class DataFeedChangeRequest(BaseModel):
    """Frozen pydantic-v2 contract for the data-feed roster touchpoint.

    Maps the checklist's block keys to typed fields; ``model_validator``
    rejects fields outside the action-scoped key set (the multi-action
    smuggle defense).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: DFCRAction
    feed: str
    # ADD
    kind: Literal["external", "derived"] | None = None
    provider: str | None = None
    adapter: str | None = None
    derived_from: list[str] | None = None
    need: str | None = None
    cadence: str | None = None
    # REMOVE
    disposition: str | None = None
    reason: str | None = None
    # MODIFY
    change: str | None = None

    @model_validator(mode="after")
    def _exactly_the_selected_action_fields(self) -> DataFeedChangeRequest:
        present = {
            k for k in (_ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS)
            if getattr(self, k) is not None
        }
        allowed = _KEYS_FOR[self.operation]
        stray = present - allowed
        if stray:
            raise ValueError(
                f"field(s) {sorted(stray)} not valid for operation "
                f"{self.operation.name}")
        if self.operation is DFCRAction.ADD:
            if not self.kind:
                raise ValueError("ADD requires `kind` (external | derived)")
            if not self.need:
                raise ValueError("ADD requires `need` (why this feed exists)")
            if self.kind == "external":
                if not self.provider:
                    raise ValueError(
                        "ADD kind=external requires `provider`")
                if not self.adapter:
                    raise ValueError(
                        "ADD kind=external requires `adapter` (importable "
                        "dotted path)")
            elif self.kind == "derived":
                if not self.derived_from:
                    raise ValueError(
                        "ADD kind=derived requires `derived_from` "
                        "(upstream feed list)")
        return self


def _parse_block(text: str) -> dict[str, str]:
    """Extract the ``DATA FEED CHANGE REQUEST`` ... key:value block.
    Lines beginning ``#`` or blank are comments. A duplicate key is a
    hard error. Unknown keys are a hard error (strict, not best-effort)."""
    lines = text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if ln.strip() == "DATA FEED CHANGE REQUEST"),
        None,
    )
    if start is None:
        raise ValueError(
            "no DFCR block found "
            "(expected a line `DATA FEED CHANGE REQUEST`)"
        )
    out: dict[str, str] = {}
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            raise ValueError(f"malformed DFCR line (no `key: value`): {s!r}")
        key, _, val = s.partition(":")
        key = key.strip()
        # Strip inline comments AFTER the colon's value (kept as a
        # single substring; do NOT split on `#` mid-evidence-string).
        val = val.split("#", 1)[0].strip() if "#" in val else val.strip()
        if key in out:
            raise ValueError(f"duplicate key: {key}")
        if key not in _ALL_KEYS:
            raise ValueError(f"unknown DFCR key: {key}")
        out[key] = val
    if "operation" not in out:
        raise ValueError("DFCR block missing required key: operation")
    return out


def _coerce(raw: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "derived_from":
            # Allowed forms: ``[feed_a, feed_b]`` or ``feed_a,feed_b``.
            stripped = v.strip().lstrip("[").rstrip("]")
            out[k] = [
                t.strip() for t in stripped.split(",") if t.strip()
            ]
        else:
            out[k] = v
    return out


def parse_dfcr(text: str) -> DataFeedChangeRequest:
    raw = _parse_block(text)
    try:
        operation = DFCRAction(raw["operation"].strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"invalid operation {raw['operation']!r}: must be exactly "
            f"one of ADD | REMOVE | MODIFY"
        ) from exc
    coerced = _coerce(raw)
    coerced["operation"] = operation
    return DataFeedChangeRequest(**coerced)


__all__ = ["DFCRAction", "DataFeedChangeRequest", "parse_dfcr"]
