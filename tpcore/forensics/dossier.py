"""Sprint Dossier generation.

When Forensics fires a trigger, this module renders a markdown postmortem
template prefilled with the trigger payload. The operator opens the file,
fills in the **Hypothesis** + **Fix** sections, ships the code change, and
clicks "Mark resolved" on the dashboard to close out the trigger.

Per MASTER_PLAN §5: the dossier is the contract between the automated
detector and the human-driven sprint that converts a loss pattern into a
platform improvement.

File layout: ``docs/sprints/<YYYY-MM-DD>-<trigger_kind>-<engine>-<id>.md``.
Idempotent — re-running for the same trigger overwrites the file (the
trigger fingerprint guarantees one trigger maps to one dossier).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .service import ForensicsTrigger, TriggerKind

SPRINTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "sprints"


_HYPOTHESIS_PROMPTS: dict[TriggerKind, list[str]] = {
    TriggerKind.OUTLIER_LOSS: [
        "Was this trade an anomaly the engine could have filtered (e.g., earnings within N days, news event)?",
        "Did the setup gate fail to enforce something the spec requires?",
        "Should the stop loss have been tighter, given the entry context?",
    ],
    TriggerKind.LOSS_CLUSTER: [
        "Is the engine running into a regime it wasn't designed for (e.g., trending market for a mean-reversion engine)?",
        "Are these all the same sector / factor exposure?",
        "Did a recent code/parameter change introduce the regression?",
    ],
    TriggerKind.DRAWDOWN_PERIOD: [
        "Is the position-sizing too aggressive for the engine's realized volatility?",
        "Has the engine's edge eroded — does the latest credibility-rubric run still pass?",
        "Should the allocator's soft-freeze threshold trip sooner?",
    ],
}


def _fmt_payload_block(payload: dict) -> str:
    rows = []
    for k in sorted(payload):
        if k == "fingerprint":
            continue
        v = payload[k]
        if isinstance(v, list):
            v_str = ", ".join(str(x) for x in v) if v else "—"
        else:
            v_str = str(v)
        rows.append(f"| {k} | {v_str} |")
    return "\n".join(rows)


def render_dossier(
    *,
    trigger: ForensicsTrigger,
    trigger_id: int | str,
    fired_at: datetime,
) -> str:
    """Return the markdown body for a Sprint Dossier."""
    prompts = _HYPOTHESIS_PROMPTS.get(trigger.trigger_kind, [])
    payload_table = _fmt_payload_block(trigger.payload)
    prompt_md = "\n".join(f"- [ ] {p}" for p in prompts) or "- [ ] (no prompts for this kind)"

    return f"""# Sprint Dossier — {trigger.trigger_kind.value} / {trigger.engine}

**Status:** open
**Trigger id:** {trigger_id}
**Fingerprint:** `{trigger.fingerprint}`
**Fired at:** {fired_at.isoformat()}

## 1. What fired

Forensics detected a **{trigger.trigger_kind.value}** pattern in the **{trigger.engine}** engine's AAR history.

### Payload

| Field | Value |
| --- | --- |
{payload_table}

## 2. Hypothesis (operator fills in)

Pick the most likely cause and develop the test:

{prompt_md}

**Working hypothesis:**

> _<one paragraph — what you think caused this and what evidence would confirm it>_

## 3. Investigation log

- [ ] Pull the underlying AAR rows; sanity-check entry/exit prices vs broker fills.
- [ ] Cross-reference against `platform.application_log` for the same trade_ids.
- [ ] Re-run the parameter search if credibility was last green > 30 days ago.
- [ ] Consult `docs/EDGE_VALIDATION_PLAN.md` for the engine's gating thresholds.

## 4. Fix (operator fills in)

**Proposed code change:**

> _<file path + brief description; link the PR when ready>_

**Verification:**

- [ ] Unit tests cover the new behavior
- [ ] Re-running the search shows credibility unchanged or improved
- [ ] Dry-run on the last 90 days shows the trigger condition no longer fires

## 5. Close-out

When the fix ships:

1. Resolve the trigger via the dashboard (Health tab → Forensics expander → "Mark resolved").
2. Update this dossier's status from `open` → `resolved` at the top.
3. Add a one-line entry to `docs/OPERATIONS.md` "Lessons learned" if the insight generalizes.
"""


def dossier_path(*, trigger: ForensicsTrigger, trigger_id: int | str, fired_at: datetime) -> Path:
    """Deterministic file path for this trigger's dossier."""
    SPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    day = fired_at.strftime("%Y-%m-%d")
    name = f"{day}-{trigger.trigger_kind.value}-{trigger.engine}-{trigger_id}.md"
    return SPRINTS_DIR / name


def write_dossier(
    *,
    trigger: ForensicsTrigger,
    trigger_id: int | str,
    fired_at: datetime,
) -> Path:
    """Render and write the dossier file. Returns the absolute path."""
    path = dossier_path(trigger=trigger, trigger_id=trigger_id, fired_at=fired_at)
    path.write_text(render_dossier(trigger=trigger, trigger_id=trigger_id, fired_at=fired_at))
    return path


__all__ = ["SPRINTS_DIR", "dossier_path", "render_dossier", "write_dossier"]
