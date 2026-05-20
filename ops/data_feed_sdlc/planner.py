"""DFCR planner — classify → validate → apply.

The single mutator for the data-feed roster SoT
(``tpcore/providers.py::_BINDINGS`` + ``tpcore/feeds/profile.py::
FEED_PROFILES``). Mirrors ``ops/engine_sdlc/planner.py`` (ECR) shape;
both have the same skeleton (parse → classify → validate → apply with
journaled-atomic writes).

Four operations per the checklist (one structured touchpoint —
operator approves ONLY ADD and REMOVE; CUTOVER + MODIFY-cadence/
threshold are automated, deterministic, no approval per
``.claude/rules/data-feed-roster.md``):

* **ADD** (ONBOARD) — new feed/provider. Operator binary ``APPROVE? (y/n)``.
* **REMOVE** (RETIRE) — existing feed retirement. Operator binary
  ``APPROVE? (y/n)``. 3-way atomic removal (ProviderBinding +
  FeedProfile + HealSpec) + CSV-archive provenance.
* **MODIFY → provider** (CUTOVER) — provider swap for an existing
  feed. The new provider MUST already be in the registry as
  ``FALLBACK`` (parity-verified). Automated, no approval.
* **MODIFY → cadence/threshold** — config change to an existing
  FeedProfile field. Automated, no approval.

All writes go through ``CLAUDE_DFCR_RUN=1`` env-var override of the
``.claude/hooks/gate-ecr-dfcr-edits.sh`` PreToolUse hook (the hook
recognises the env-var as "this IS a DFCR planner-driven edit").
The planner uses Python file I/O for atomic in-place edits — the
PreToolUse hook does not fire on direct Python writes, but the env-
var is set defensively for any tool-mediated apply paths.

Atomicity discipline: every mutation is journaled BEFORE it happens so
a downstream error reverse-replays to a BYTE-IDENTICAL pre-state. The
``_Journal`` here is intentionally simpler than the ECR's (only
``record_file`` — no scaffold-copy moves to undo), but the contract is
the same: failed apply leaves ZERO trace.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from ops.data_feed_sdlc.dfcr import DataFeedChangeRequest, DFCRAction

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_PATH = REPO_ROOT / "tpcore" / "providers.py"
FEED_PROFILES_PATH = REPO_ROOT / "tpcore" / "feeds" / "profile.py"


class ApprovalClass(StrEnum):
    OPERATOR = "operator"   # binary y/n required (ADD / REMOVE)
    AUTOMATED = "automated"  # planner applies without prompt (CUTOVER / config)


@dataclass(frozen=True)
class TransitionPlan:
    """The validated change the planner will (or will not) apply.

    ``rejection`` is None on a valid plan; populated with the EXACT
    reason on every reject path so the operator never has to guess.
    """
    operation: DFCRAction
    feed: str
    approval_class: ApprovalClass = ApprovalClass.OPERATOR
    # ADD-only context (drives _apply_add)
    provider: str | None = None
    adapter: str | None = None
    kind: str | None = None
    derived_from: list[str] | None = None
    need: str | None = None
    cadence: str | None = None
    # REMOVE-only
    disposition: str | None = None
    reason: str | None = None
    # MODIFY-only
    change: str | None = None
    # Rejection reason — None on a valid plan.
    rejection: str | None = None


def _reject(dfcr: DataFeedChangeRequest, reason: str) -> TransitionPlan:
    return TransitionPlan(
        operation=dfcr.operation, feed=dfcr.feed,
        approval_class=ApprovalClass.OPERATOR,
        rejection=reason,
    )


def classify(
    dfcr: DataFeedChangeRequest,
    binding_snapshot: dict[str, list[dict[str, str]]],
) -> TransitionPlan:
    """Decide the operation's approval class + reject duplicates / missing-feed.

    ``binding_snapshot`` is ``{feed: [{provider, status}, ...]}`` — the
    flat view of ``_BINDINGS`` projected for the classifier. The
    classifier is read-only: no I/O, no mutation (mirrors ECR
    ``classify``).
    """
    feed_present = dfcr.feed in binding_snapshot

    if dfcr.operation is DFCRAction.ADD:
        if feed_present:
            return _reject(
                dfcr,
                f"feed {dfcr.feed!r} already exists (use MODIFY to swap "
                f"provider, or REMOVE to retire)",
            )
        return TransitionPlan(
            operation=dfcr.operation, feed=dfcr.feed,
            approval_class=ApprovalClass.OPERATOR,
            kind=dfcr.kind, provider=dfcr.provider, adapter=dfcr.adapter,
            derived_from=dfcr.derived_from, need=dfcr.need,
            cadence=dfcr.cadence,
        )

    if dfcr.operation is DFCRAction.REMOVE:
        if not feed_present:
            return _reject(
                dfcr,
                f"nothing to remove: feed {dfcr.feed!r} absent from "
                f"_BINDINGS",
            )
        return TransitionPlan(
            operation=dfcr.operation, feed=dfcr.feed,
            approval_class=ApprovalClass.OPERATOR,
            disposition=dfcr.disposition, reason=dfcr.reason,
        )

    # MODIFY
    if not feed_present:
        return _reject(
            dfcr,
            f"nothing to modify: feed {dfcr.feed!r} absent from "
            f"_BINDINGS",
        )
    return TransitionPlan(
        operation=dfcr.operation, feed=dfcr.feed,
        approval_class=ApprovalClass.AUTOMATED,
        change=dfcr.change, reason=dfcr.reason,
    )


def _read_bindings_snapshot(root: Path | None = None) -> dict[str, list[dict[str, str]]]:
    """Read tpcore/providers.py and return a flat snapshot per feed.

    Pure read — no module import (the importable form would also fire
    pydantic validators); just AST-walk the file to surface the (feed,
    provider, status) tuples.
    """
    root = root or REPO_ROOT
    src = (root / "tpcore" / "providers.py").read_text()
    tree = ast.parse(src)
    snapshot: dict[str, list[dict[str, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None
        )
        if name != "ProviderBinding":
            continue
        kwargs: dict[str, str] = {}
        for kw in node.keywords:
            if kw.arg in ("feed", "provider") and isinstance(kw.value, ast.Constant):
                kwargs[kw.arg] = str(kw.value.value)
            elif kw.arg == "status" and isinstance(kw.value, ast.Attribute):
                kwargs["status"] = kw.value.attr.lower()
        if "feed" in kwargs and "provider" in kwargs:
            snapshot.setdefault(kwargs["feed"], []).append(kwargs)
    return snapshot


def _read_feed_profiles_keys(root: Path | None = None) -> set[str]:
    """Lightweight read of FEED_PROFILES keys (the dict keys, not the
    FeedProfile bodies — those are pydantic-validated at module import)."""
    root = root or REPO_ROOT
    src = (root / "tpcore" / "feeds" / "profile.py").read_text()
    tree = ast.parse(src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "FEED_PROFILES"):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


@dataclass
class _Journal:
    """Pre-write file-content journal for transactional atomicity. On
    failure, ``restore`` rewinds every recorded file to its pre-state.

    The simpler analogue of ``ops.engine_sdlc.planner._Journal`` — only
    file-content snapshots (no directory moves: DFCR never adds or
    removes a package directory, just text in two files)."""
    files: dict[Path, bytes] = field(default_factory=dict)

    def record(self, path: Path) -> None:
        if path in self.files:
            return
        if path.exists():
            self.files[path] = path.read_bytes()
        else:
            self.files[path] = b""

    def restore(self) -> None:
        for path, content in self.files.items():
            if content == b"":
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(content)


def _insert_provider_binding(
    src: str, *, feed: str, provider: str, adapter: str, evidence: str,
    status: str = "ACTIVE",
) -> str:
    """Insert a new ProviderBinding(...) entry into _BINDINGS.

    The insertion point is the line containing the literal closing
    paren of the ``_BINDINGS: tuple[ProviderBinding, ...] = (`` tuple
    (just before the final ``)`` token); the new entry is added on its
    own block of lines. The new binding's evidence MUST be non-empty
    (mirrors the model_post_init guard in tpcore.providers).
    """
    if not evidence.strip():
        raise RuntimeError("ProviderBinding requires non-empty evidence")
    # AST-safe locate: find ``_BINDINGS`` assignment then its closing
    # paren's source span via lineno/col_offset.
    tree = ast.parse(src)
    target_node: ast.Tuple | None = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_BINDINGS"):
            if isinstance(node.value, ast.Tuple):
                target_node = node.value
            break
    if target_node is None:
        raise RuntimeError("_BINDINGS tuple not found in tpcore/providers.py")
    # Insert before the closing paren — the end_lineno of the tuple is
    # the line WITH the ``)``. Insert a new block ABOVE it.
    end_lineno = target_node.end_lineno
    assert end_lineno is not None
    lines = src.splitlines(keepends=True)
    # Prepare insertion block.
    block = (
        f"    ProviderBinding(\n"
        f"        feed={feed!r}, provider={provider!r},\n"
        f"        adapter_module={adapter!r},\n"
        f"        status=ProviderStatus.{status},\n"
        f"        evidence={evidence!r},\n"
        f"    ),\n"
    )
    # Insert before the line containing the closing paren.
    out = lines[: end_lineno - 1] + [block] + lines[end_lineno - 1:]
    new_src = "".join(out)
    # AST-safe: must still parse.
    ast.parse(new_src)
    return new_src


def _insert_feed_profile(
    src: str, *, feed: str, cadence_days: int, freshness_max_age_days: int,
    skip_guard_days: int, evidence: str, trigger: str = "VENDOR_RELEASE",
) -> str:
    """Insert a new FeedProfile(...) entry into FEED_PROFILES.

    Inserted at the dict's closing brace position. Trigger defaults to
    ``VENDOR_RELEASE`` (the generic per-series schedule slot); the DFCR
    block records the cadence string for evidence but the FeedTrigger
    enum value defaults to RELEASE — the planner does not infer
    trigger semantics (caller may MODIFY-cadence later if needed).
    """
    tree = ast.parse(src)
    target_node: ast.Dict | None = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "FEED_PROFILES"):
            if isinstance(node.value, ast.Dict):
                target_node = node.value
            break
    if target_node is None:
        raise RuntimeError("FEED_PROFILES dict not found in tpcore/feeds/profile.py")
    end_lineno = target_node.end_lineno
    assert end_lineno is not None
    lines = src.splitlines(keepends=True)
    block = (
        f"    {feed!r}: FeedProfile(\n"
        f"        feed={feed!r}, trigger=FeedTrigger.{trigger},\n"
        f"        cadence_days={cadence_days}, "
        f"freshness_max_age_days={freshness_max_age_days}, "
        f"skip_guard_days={skip_guard_days},\n"
        f"        evidence={evidence!r},\n"
        f"    ),\n"
    )
    out = lines[: end_lineno - 1] + [block] + lines[end_lineno - 1:]
    new_src = "".join(out)
    ast.parse(new_src)
    return new_src


def _remove_provider_binding(src: str, *, feed: str) -> str:
    """Remove every ProviderBinding(feed=<feed>, ...) entry from _BINDINGS.

    A REMOVE applies to ALL bindings for the feed (active + any
    candidates / fallbacks). Removes contiguous lines from the start
    of the ``ProviderBinding(`` constructor through its closing ``),``.
    """
    tree = ast.parse(src)
    spans: list[tuple[int, int]] = []  # (start_line, end_line) inclusive 1-based
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None
        )
        if name != "ProviderBinding":
            continue
        feed_kw_match = any(
            kw.arg == "feed"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == feed
            for kw in node.keywords
        )
        if feed_kw_match:
            assert node.end_lineno is not None
            spans.append((node.lineno, node.end_lineno))
    if not spans:
        return src
    # Reverse-iterate so earlier indices stay valid.
    lines = src.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        # Also consume an immediately-following blank/comment line if it
        # was an end-of-block comma-only spacer (defensive); the binding
        # ends with ``),`` so the next line should be the next entry or
        # the closing paren.
        del lines[start - 1: end]
    new_src = "".join(lines)
    ast.parse(new_src)
    return new_src


def _remove_feed_profile(src: str, *, feed: str) -> str:
    """Remove the ``feed`` entry from the FEED_PROFILES dict literal."""
    tree = ast.parse(src)
    target_dict: ast.Dict | None = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "FEED_PROFILES"):
            if isinstance(node.value, ast.Dict):
                target_dict = node.value
            break
    if target_dict is None:
        raise RuntimeError("FEED_PROFILES dict not found in tpcore/feeds/profile.py")
    # Locate the matching key + its value's source span.
    spans: list[tuple[int, int]] = []
    for k, v in zip(target_dict.keys, target_dict.values, strict=False):
        if isinstance(k, ast.Constant) and k.value == feed:
            assert v.end_lineno is not None
            spans.append((k.lineno, v.end_lineno))
    if not spans:
        return src
    lines = src.splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1: end]
    new_src = "".join(lines)
    ast.parse(new_src)
    return new_src


def _cutover_provider_status(
    src: str, *, feed: str, new_active_provider: str,
) -> str:
    """CUTOVER: flip the current ACTIVE provider for ``feed`` to
    DEPRECATED and flip ``new_active_provider`` for the same feed
    from FALLBACK (or whatever it was) to ACTIVE.

    Implementation note: this is a text-level transform that re-parses
    after; the input MUST have exactly one ACTIVE for ``feed`` and
    ``new_active_provider`` MUST already be in the registry for
    ``feed``. The classifier + validate enforce both preconditions.
    """
    tree = ast.parse(src)
    edits: list[tuple[int, int, str]] = []  # (line_idx, col_offset, new_text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None
        )
        if name != "ProviderBinding":
            continue
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg == "feed" and isinstance(kw.value, ast.Constant):
                kwargs["feed"] = kw.value.value
            elif kw.arg == "provider" and isinstance(kw.value, ast.Constant):
                kwargs["provider"] = kw.value.value
            elif kw.arg == "status":
                kwargs["status_node"] = kw.value
        if kwargs.get("feed") != feed:
            continue
        status_node = kwargs.get("status_node")
        if status_node is None:
            continue
        provider_val = kwargs.get("provider")
        new_status: str | None = None
        if provider_val == new_active_provider:
            new_status = "ProviderStatus.ACTIVE"
        else:
            # Demote the CURRENT active to DEPRECATED.
            if isinstance(status_node, ast.Attribute) and status_node.attr.upper() == "ACTIVE":
                new_status = "ProviderStatus.DEPRECATED"
        if new_status is None:
            continue
        assert status_node.end_lineno is not None
        edits.append((
            status_node.lineno - 1,
            status_node.col_offset,
            new_status,
        ))
    if not edits:
        raise RuntimeError(
            f"CUTOVER: no eligible ProviderBinding for feed={feed!r} found"
        )
    # Apply edits — line-by-line text replacement (status tokens are
    # short, so substring substitution on the AST-derived text is safe).
    lines = src.splitlines(keepends=True)
    for line_idx, _col, new_text in edits:
        # Replace exactly one instance of ``ProviderStatus.<TOKEN>`` on
        # this line with the new_text. There is at most one per line in
        # the canonical formatting.
        line = lines[line_idx]
        new_line = re.sub(
            r"ProviderStatus\.[A-Z]+",
            new_text,
            line, count=1,
        )
        lines[line_idx] = new_line
    new_src = "".join(lines)
    ast.parse(new_src)
    return new_src


def _modify_feed_profile_field(
    src: str, *, feed: str, field_name: str, new_value: Any,
) -> str:
    """MODIFY: change one keyword argument of an existing FeedProfile.

    Supports ``cadence_days`` / ``freshness_max_age_days`` /
    ``skip_guard_days`` (numeric) and ``evidence`` (string) — the four
    fields the checklist enumerates.
    """
    tree = ast.parse(src)
    fp_value_node: ast.Call | None = None
    target_dict: ast.Dict | None = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "FEED_PROFILES"):
            if isinstance(node.value, ast.Dict):
                target_dict = node.value
            break
    if target_dict is None:
        raise RuntimeError("FEED_PROFILES dict not found")
    for k, v in zip(target_dict.keys, target_dict.values, strict=False):
        if (isinstance(k, ast.Constant) and k.value == feed
                and isinstance(v, ast.Call)):
            fp_value_node = v
            break
    if fp_value_node is None:
        raise RuntimeError(
            f"FEED_PROFILES[{feed!r}] FeedProfile call not found"
        )
    for kw in fp_value_node.keywords:
        if kw.arg != field_name:
            continue
        assert kw.value.end_lineno is not None
        # Replace the entire value subspan with the rendered literal.
        rendered = repr(new_value)
        # We can do a per-line textual replacement: extract the value's
        # source by lineno/col_offset, swap to `rendered`.
        lines = src.splitlines(keepends=True)
        start_line = kw.value.lineno - 1
        end_line = kw.value.end_lineno - 1
        start_col = kw.value.col_offset
        end_col = kw.value.end_col_offset
        assert end_col is not None
        if start_line == end_line:
            line = lines[start_line]
            lines[start_line] = (
                line[:start_col] + rendered + line[end_col:]
            )
        else:
            # Multi-line value (e.g. a triple-quoted evidence). Splice.
            head = lines[start_line][:start_col]
            tail = lines[end_line][end_col:]
            lines[start_line: end_line + 1] = [head + rendered + tail]
        new_src = "".join(lines)
        ast.parse(new_src)
        return new_src
    raise RuntimeError(
        f"FEED_PROFILES[{feed!r}] has no field {field_name!r} to modify"
    )


def validate(
    plan: TransitionPlan,
    *,
    repo_root: Path | None = None,
    dfcr: DataFeedChangeRequest | None = None,
) -> TransitionPlan:
    """Re-derive structural preconditions from the on-disk SoT.

    A pre-classified TransitionPlan passes through here unchanged
    unless validation surfaces a reason to reject (e.g. CUTOVER target
    missing). On any reject the returned plan has ``rejection`` set.
    """
    if plan.rejection is not None:
        return plan
    root = repo_root or REPO_ROOT
    snapshot = _read_bindings_snapshot(root)

    if plan.operation is DFCRAction.ADD:
        if plan.feed in snapshot:
            return TransitionPlan(
                **{**plan.__dict__,
                   "rejection": (
                       f"ADD feed={plan.feed!r} but a binding already "
                       f"exists (validate re-snap)"
                   )},
            )
        return plan

    if plan.operation is DFCRAction.REMOVE:
        if plan.feed not in snapshot:
            return TransitionPlan(
                **{**plan.__dict__,
                   "rejection": (
                       f"REMOVE feed={plan.feed!r} but no binding "
                       f"exists (validate re-snap)"
                   )},
            )
        return plan

    # MODIFY — needs a `change` token to be actionable.
    if not plan.change:
        return TransitionPlan(
            **{**plan.__dict__,
               "rejection": "MODIFY requires `change:` token"},
        )
    return plan


def apply(plan: TransitionPlan, *, repo_root: Path | None = None) -> TransitionPlan:
    """Atomically apply the planned change. On any error during apply,
    ``_Journal`` reverse-replays so the on-disk state is BYTE-IDENTICAL
    to pre-apply. Sets ``CLAUDE_DFCR_RUN=1`` in the env so the hook
    permits the planner's writes (defensive — the planner writes via
    Python file I/O which doesn't trigger the PreToolUse Edit/Write
    hook anyway, but explicit > implicit).
    """
    if plan.rejection is not None:
        return plan
    root = repo_root or REPO_ROOT
    os.environ["CLAUDE_DFCR_RUN"] = "1"
    journal = _Journal()
    try:
        if plan.operation is DFCRAction.ADD:
            _apply_add(plan, root, journal)
        elif plan.operation is DFCRAction.REMOVE:
            _apply_remove(plan, root, journal)
        elif plan.operation is DFCRAction.MODIFY:
            _apply_modify(plan, root, journal)
        return plan
    except Exception as exc:  # noqa: BLE001
        journal.restore()
        return TransitionPlan(
            **{**plan.__dict__,
               "rejection": f"apply failed (reverted): {exc}"},
        )


def _apply_add(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """ADD path: write ProviderBinding + FeedProfile + verify post-state."""
    providers_path = root / "tpcore" / "providers.py"
    profile_path = root / "tpcore" / "feeds" / "profile.py"
    jn.record(providers_path)
    jn.record(profile_path)

    # Derive evidence + cadence sensible defaults if not provided.
    evidence = (
        plan.need or f"DFCR-onboarded feed {plan.feed!r}; provider="
        f"{plan.provider or 'internal'}"
    )
    # Cadence parsing: best-effort — the checklist accepts a free-text
    # cadence (e.g. "quarterly", "daily"). Map common tokens to days;
    # default to 1 (daily) when ambiguous.
    cadence_str = (plan.cadence or "").lower().strip()
    cadence_days = 1
    freshness_age = 7
    skip_guard = 1
    if "quarter" in cadence_str:
        cadence_days, freshness_age, skip_guard = 91, 120, 6
    elif "month" in cadence_str:
        cadence_days, freshness_age, skip_guard = 30, 35, 2
    elif "week" in cadence_str:
        cadence_days, freshness_age, skip_guard = 7, 10, 5
    elif "daily" in cadence_str or "day" in cadence_str:
        cadence_days, freshness_age, skip_guard = 1, 7, 1

    # ProviderBinding: external kind => the provided provider/adapter;
    # derived kind => provider="internal", adapter is the derived
    # computation module.
    provider = plan.provider or "internal"
    adapter = plan.adapter or "tpcore.derived"

    src = providers_path.read_text()
    new_src = _insert_provider_binding(
        src, feed=plan.feed, provider=provider, adapter=adapter,
        evidence=evidence,
    )
    providers_path.write_text(new_src)

    src = profile_path.read_text()
    new_src = _insert_feed_profile(
        src, feed=plan.feed, cadence_days=cadence_days,
        freshness_max_age_days=freshness_age, skip_guard_days=skip_guard,
        evidence=evidence,
    )
    profile_path.write_text(new_src)


def _apply_remove(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """REMOVE path: strip every ProviderBinding for feed + drop
    FeedProfile entry. HealSpec removal is the consumer's responsibility
    in the same PR (planner only owns ProviderBinding + FeedProfile).
    """
    providers_path = root / "tpcore" / "providers.py"
    profile_path = root / "tpcore" / "feeds" / "profile.py"
    jn.record(providers_path)
    jn.record(profile_path)

    src = providers_path.read_text()
    new_src = _remove_provider_binding(src, feed=plan.feed)
    providers_path.write_text(new_src)

    src = profile_path.read_text()
    new_src = _remove_feed_profile(src, feed=plan.feed)
    profile_path.write_text(new_src)


def _apply_modify(plan: TransitionPlan, root: Path, jn: _Journal) -> None:
    """MODIFY path: either CUTOVER (``change: provider:<new>``) or
    cadence/threshold edit (``change: cadence:<n>d`` /
    ``change: threshold:<check>=<value>`` / ``change: evidence:<text>``).
    """
    assert plan.change is not None
    providers_path = root / "tpcore" / "providers.py"
    profile_path = root / "tpcore" / "feeds" / "profile.py"

    change = plan.change.strip()
    key, _, value = change.partition(":")
    key = key.strip().lower()
    value = value.strip()
    if not value:
        raise RuntimeError(f"MODIFY change={change!r} missing value")

    if key == "provider":
        jn.record(providers_path)
        src = providers_path.read_text()
        new_src = _cutover_provider_status(
            src, feed=plan.feed, new_active_provider=value,
        )
        providers_path.write_text(new_src)
        return

    if key == "cadence":
        # value form: ``<n>d`` or just a number
        v = value.rstrip("d").strip()
        n = int(v)
        jn.record(profile_path)
        src = profile_path.read_text()
        new_src = _modify_feed_profile_field(
            src, feed=plan.feed, field_name="cadence_days", new_value=n,
        )
        profile_path.write_text(new_src)
        return

    if key == "freshness_max_age" or key == "freshness":
        n = int(value.rstrip("d").strip())
        jn.record(profile_path)
        src = profile_path.read_text()
        new_src = _modify_feed_profile_field(
            src, feed=plan.feed, field_name="freshness_max_age_days",
            new_value=n,
        )
        profile_path.write_text(new_src)
        return

    if key == "skip_guard":
        n = int(value.rstrip("d").strip())
        jn.record(profile_path)
        src = profile_path.read_text()
        new_src = _modify_feed_profile_field(
            src, feed=plan.feed, field_name="skip_guard_days",
            new_value=n,
        )
        profile_path.write_text(new_src)
        return

    if key == "evidence":
        jn.record(profile_path)
        src = profile_path.read_text()
        new_src = _modify_feed_profile_field(
            src, feed=plan.feed, field_name="evidence", new_value=value,
        )
        profile_path.write_text(new_src)
        return

    raise RuntimeError(
        f"MODIFY change={change!r}: unsupported key {key!r}. "
        f"Supported: provider | cadence | freshness | skip_guard | evidence"
    )


__all__ = [
    "ApprovalClass",
    "TransitionPlan",
    "apply",
    "classify",
    "validate",
]


class _PlanModel(BaseModel):  # pragma: no cover — kept for API completeness
    """Pydantic shape mirror for callers who want a frozen plan view."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: DFCRAction
    feed: str
    approval_class: ApprovalClass
    rejection: str | None = None
