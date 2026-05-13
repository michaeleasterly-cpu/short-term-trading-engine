"""Unit tests for Sprint Dossier rendering + file writes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tpcore.forensics.dossier import (
    SPRINTS_DIR,
    dossier_path,
    render_dossier,
    write_dossier,
)
from tpcore.forensics.service import ForensicsTrigger, TriggerKind


def _trigger(kind: TriggerKind = TriggerKind.OUTLIER_LOSS) -> ForensicsTrigger:
    return ForensicsTrigger(
        trigger_kind=kind,
        engine="sigma",
        fingerprint="sigma|test|fp",
        payload={
            "engine": "sigma",
            "trade_id": "YUMC_1778582356",
            "ticker": "YUMC",
            "pnl_net": "-6.72",
            "fingerprint": "sigma|test|fp",
        },
    )


def test_render_dossier_contains_required_sections() -> None:
    fired_at = datetime(2026, 5, 14, tzinfo=UTC)
    md = render_dossier(trigger=_trigger(), trigger_id=42, fired_at=fired_at)
    # Every operator-driven section header must appear so the dossier
    # is self-contained for the next person who opens it.
    for required in (
        "# Sprint Dossier",
        "**Status:** open",
        "**Trigger id:** 42",
        "Hypothesis (operator fills in)",
        "Investigation log",
        "Fix (operator fills in)",
        "Close-out",
        "YUMC",  # payload bled through
    ):
        assert required in md, f"missing section: {required}"


def test_render_dossier_omits_fingerprint_from_payload_table() -> None:
    # Fingerprint is a deduplication key; not useful in the operator-facing table.
    md = render_dossier(
        trigger=_trigger(), trigger_id=1, fired_at=datetime(2026, 5, 14, tzinfo=UTC)
    )
    assert "| fingerprint |" not in md


def test_render_dossier_handles_list_payload_field() -> None:
    trigger = ForensicsTrigger(
        trigger_kind=TriggerKind.LOSS_CLUSTER,
        engine="sigma",
        fingerprint="sigma|cluster|fp",
        payload={
            "engine": "sigma",
            "streak_length": 3,
            "trade_ids": ["T1", "T2", "T3"],
            "fingerprint": "sigma|cluster|fp",
        },
    )
    md = render_dossier(trigger=trigger, trigger_id=99, fired_at=datetime(2026, 5, 14, tzinfo=UTC))
    assert "T1, T2, T3" in md


def test_dossier_path_is_deterministic(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tpcore.forensics.dossier.SPRINTS_DIR", tmp_path)
    fired = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    p1 = dossier_path(trigger=_trigger(), trigger_id=42, fired_at=fired)
    p2 = dossier_path(trigger=_trigger(), trigger_id=42, fired_at=fired)
    assert p1 == p2
    assert p1.name == "2026-05-14-outlier_loss-sigma-42.md"
    assert p1.parent == tmp_path


def test_write_dossier_creates_file_idempotently(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tpcore.forensics.dossier.SPRINTS_DIR", tmp_path)
    fired = datetime(2026, 5, 14, tzinfo=UTC)
    p1 = write_dossier(trigger=_trigger(), trigger_id=1, fired_at=fired)
    assert p1.exists()
    content = p1.read_text()
    assert "YUMC" in content
    # Re-running overwrites the same file (one trigger = one dossier).
    p2 = write_dossier(trigger=_trigger(), trigger_id=1, fired_at=fired)
    assert p2 == p1
