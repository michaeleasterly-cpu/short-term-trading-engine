"""The Engine Change Request — frozen pydantic-v2 contract + strict parser.

The fenced ``ECR`` block in docs/superpowers/checklists/
engine_change_request.md is the wire format. parse_ecr is the single
entry point: a request that does not parse is rejected with the EXACT
reason, never best-effort-interpreted (spec §2.1).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from tpcore.engine_profile import Cadence


class ECRAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"


# Which ECR keys are valid for which action (spec §2.2). ``action`` and
# ``engine`` are always required; the rest are action-scoped.
_COMMON = {"action", "engine"}
_ADD_KEYS = {"source", "lab_dossier", "cadence", "allocator",
             "dispatch_order", "gate_dsr", "gate_cred", "need"}
_REMOVE_KEYS = {"reason", "eulogy_notes"}
_MODIFY_KEYS = {"lab_dossier", "param_change", "gate_dsr", "gate_cred"}
_KEYS_FOR = {
    ECRAction.ADD: _ADD_KEYS,
    ECRAction.REMOVE: _REMOVE_KEYS,
    ECRAction.MODIFY: _MODIFY_KEYS,
}
_ALL_KEYS = _COMMON | _ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS


class EngineChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ECRAction
    engine: str
    # ADD
    source: Literal["new_scaffold", "lab_candidate"] | None = None
    lab_dossier: str | None = None
    cadence: Cadence | None = None
    allocator: bool | None = None
    dispatch_order: int | None = None
    gate_dsr: float | None = None
    gate_cred: int | None = None
    need: str | None = None
    # REMOVE
    reason: str | None = None
    eulogy_notes: str | None = None
    # MODIFY
    param_change: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_the_selected_action_fields(self) -> EngineChangeRequest:
        present = {
            k for k in (_ADD_KEYS | _REMOVE_KEYS | _MODIFY_KEYS)
            if getattr(self, k) is not None
        }
        allowed = _KEYS_FOR[self.action]
        stray = present - allowed
        if stray:
            raise ValueError(
                f"field(s) {sorted(stray)} not valid for action "
                f"{self.action.name}")
        return self


def _parse_block(text: str) -> dict[str, str]:
    """Extract the ``ECR`` ... key:value block. Lines beginning ``#`` or
    blank are comments. A duplicate key is a hard error (catches the
    multi-action smuggle). Unknown keys are a hard error (not ignored —
    spec §2.2 strict extra=forbid at the parser, not just the model)."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "ECR"), None)
    if start is None:
        raise ValueError("no ECR block found (expected a line `ECR`)")
    out: dict[str, str] = {}
    for ln in lines[start + 1:]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            raise ValueError(f"malformed ECR line (no `key: value`): {s!r}")
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip()
        if key in out:
            raise ValueError(f"duplicate key: {key}")
        if key not in _ALL_KEYS:
            raise ValueError(f"unknown ECR key: {key}")
        out[key] = val
    if "action" not in out:
        raise ValueError("ECR block missing required key: action")
    return out


def _coerce(raw: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "allocator":
            out[k] = v.lower() == "true"
        elif k == "dispatch_order" or k == "gate_cred":
            out[k] = int(v)
        elif k == "gate_dsr":
            out[k] = float(v)
        elif k == "param_change":
            d: dict[str, str] = {}
            for pair in (p for p in v.split(",") if p.strip()):
                pk, _, pv = pair.partition("=")
                d[pk.strip()] = pv.strip()
            out[k] = d
        else:
            out[k] = v
    return out


def parse_ecr(text: str) -> EngineChangeRequest:
    raw = _parse_block(text)
    try:
        action = ECRAction(raw["action"].strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"invalid action {raw['action']!r}: must be exactly one of "
            f"ADD | REMOVE | MODIFY") from exc
    coerced = _coerce(raw)
    coerced["action"] = action
    return EngineChangeRequest(**coerced)


__all__ = ["ECRAction", "EngineChangeRequest", "parse_ecr"]
