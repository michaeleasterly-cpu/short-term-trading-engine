"""Strict ECR parser contract (SP3 T0). Lazy in-body import of
ops.engine_sdlc.ecr (H-S3-10: the scripts/ops.py vs ops/ sys.modules
collision the SP2 T9/T10 bite proved — never import at module top)."""
from __future__ import annotations

import pytest

_VALID_ADD = """\
ECR
action:        ADD
engine:        edgehunter
source:        new_scaffold
cadence:       daily
allocator:     false
dispatch_order: 6
need:          captures intraday gap-fade edge
"""

_VALID_REMOVE = """\
ECR
action:        REMOVE
engine:        sentinel
reason:        macro signal lags fast crashes; single-trade history
eulogy_notes:  TLT-only basket, COVID cycle only
"""

_VALID_MODIFY = """\
ECR
action:        MODIFY
engine:        reversion
lab_dossier:   docs/lab/2026-05-18-rev2-SURVIVED-seed0.md
param_change:  z_threshold=3.1, max_hold_days=8
gate_dsr:      0.96
gate_cred:     64
"""


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_valid_add_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_ADD)
    assert ecr.action is ECRAction.ADD
    assert ecr.engine == "edgehunter"
    assert ecr.source == "new_scaffold"
    assert ecr.allocator is False
    assert ecr.dispatch_order == 6
    assert ecr.reason is None and ecr.param_change is None


def test_valid_add_existing_code_parses():
    """H-S3-11e: existing_code is the third valid source value for ADD."""
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    txt = (
        "ECR\n"
        "action:        ADD\n"
        "engine:        existing\n"
        "source:        existing_code\n"
        "cadence:       daily\n"
        "allocator:     true\n"
        "dispatch_order: 7\n"
        "need:          engine shipped via a separate scaffolding PR\n"
    )
    ecr = parse_ecr(txt)
    assert ecr.action is ECRAction.ADD
    assert ecr.source == "existing_code"
    assert ecr.engine == "existing"
    # gate fields must not be carried for existing_code (same as new_scaffold)
    assert ecr.gate_dsr is None and ecr.gate_cred is None


def test_valid_remove_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_REMOVE)
    assert ecr.action is ECRAction.REMOVE
    assert ecr.engine == "sentinel"
    assert ecr.reason.startswith("macro signal lags")
    assert ecr.source is None and ecr.dispatch_order is None


def test_valid_modify_parses():
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    ecr = parse_ecr(_VALID_MODIFY)
    assert ecr.action is ECRAction.MODIFY
    assert ecr.engine == "reversion"
    assert ecr.param_change == {"z_threshold": "3.1", "max_hold_days": "8"}
    assert ecr.gate_dsr == 0.96 and ecr.gate_cred == 64


def test_unknown_key_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD + "bogus_key:  whatever\n"
    with pytest.raises(ValueError, match="unknown ECR key: bogus_key"):
        parse_ecr(bad)


def test_cross_block_field_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD + "reason:  not allowed on an ADD\n"
    with pytest.raises(ValueError, match=r"field.*not valid for action ADD|reason"):
        parse_ecr(bad)


def test_multi_action_rejected():
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _VALID_ADD.replace(
        "action:        ADD", "action:        ADD\naction:        REMOVE")
    with pytest.raises(ValueError, match="exactly one action|duplicate key: action"):
        parse_ecr(bad)


def test_nonparsing_rejected_with_reason():
    from ops.engine_sdlc.ecr import parse_ecr
    with pytest.raises(ValueError, match="no ECR block found"):
        parse_ecr("this is not an ECR at all")
