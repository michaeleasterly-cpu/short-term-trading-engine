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
    """H-S3-11e: existing_code is the third valid source value for ADD.
    Per spec §7.1 (2026-05-20 follow-up), existing_code REQUIRES a
    non-empty data_dependencies declaration — the operator-shipped engine
    code already reads from specific platform tables and the per-engine
    data gate needs them declared up front."""
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    txt = (
        "ECR\n"
        "action:        ADD\n"
        "engine:        existing\n"
        "source:        existing_code\n"
        "cadence:       daily\n"
        "allocator:     true\n"
        "dispatch_order: 7\n"
        "data_dependencies: prices_daily\n"
        "need:          engine shipped via a separate scaffolding PR\n"
    )
    ecr = parse_ecr(txt)
    assert ecr.action is ECRAction.ADD
    assert ecr.source == "existing_code"
    assert ecr.engine == "existing"
    # gate fields must not be carried for existing_code (same as new_scaffold)
    assert ecr.gate_dsr is None and ecr.gate_cred is None
    # spec §7.1: data_dependencies required and parsed to frozenset
    assert ecr.data_dependencies == frozenset({"prices_daily"})


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


# ─── Spec §7 follow-up: data_dependencies field (2026-05-20) ───
# Source-kind-aware validation: required for source: existing_code,
# optional for new_scaffold, inheritable for lab_candidate.

_ADD_EXISTING_CODE_WITH_DEPS = """\
ECR
action:        ADD
engine:        newengine
source:        existing_code
cadence:       daily
allocator:     true
dispatch_order: 9
data_dependencies: prices_daily, fundamentals_quarterly
need:          post-hoc roster registration with declared data reads
"""


def test_data_dependencies_parses_comma_separated():
    """The data_dependencies ECR text key is a comma-separated list of
    platform.<table> names. The parser converts it to frozenset[str]."""
    from ops.engine_sdlc.ecr import parse_ecr
    ecr = parse_ecr(_ADD_EXISTING_CODE_WITH_DEPS)
    assert ecr.data_dependencies == frozenset(
        {"prices_daily", "fundamentals_quarterly"})


def test_data_dependencies_single_value_parses():
    """Single value (no comma) still produces a frozenset of size 1."""
    from ops.engine_sdlc.ecr import parse_ecr
    txt = _ADD_EXISTING_CODE_WITH_DEPS.replace(
        "data_dependencies: prices_daily, fundamentals_quarterly",
        "data_dependencies: prices_daily")
    ecr = parse_ecr(txt)
    assert ecr.data_dependencies == frozenset({"prices_daily"})


def test_data_dependencies_required_for_existing_code():
    """Spec §7.1: source: existing_code REQUIRES a non-empty
    data_dependencies set. The operator-shipped engine code already reads
    from specific platform.<table>s; the ECR must declare them."""
    from ops.engine_sdlc.ecr import parse_ecr
    bad = (
        "ECR\n"
        "action:        ADD\n"
        "engine:        ec_no_deps\n"
        "source:        existing_code\n"
        "cadence:       daily\n"
        "allocator:     true\n"
        "dispatch_order: 9\n"
        "need:          missing data_dependencies\n"
    )
    with pytest.raises(ValueError, match="data_dependencies.*required.*existing_code"):
        parse_ecr(bad)


def test_data_dependencies_optional_for_new_scaffold():
    """Spec §7.1: source: new_scaffold makes data_dependencies OPTIONAL.
    A fresh scaffold may have no validation-gated reads yet (the operator
    extends it via a later MODIFY once the engine is wired to data)."""
    from ops.engine_sdlc.ecr import parse_ecr
    # No data_dependencies — must parse cleanly.
    ecr = parse_ecr(_VALID_ADD)  # new_scaffold, no data_dependencies
    assert ecr.source == "new_scaffold"
    assert ecr.data_dependencies is None


def test_data_dependencies_optional_for_new_scaffold_explicit():
    """new_scaffold MAY also explicitly carry data_dependencies (the
    operator already knows what tables the engine will read)."""
    from ops.engine_sdlc.ecr import parse_ecr
    txt = _VALID_ADD + "data_dependencies: prices_daily\n"
    ecr = parse_ecr(txt)
    assert ecr.source == "new_scaffold"
    assert ecr.data_dependencies == frozenset({"prices_daily"})


def test_data_dependencies_inheritable_for_lab_candidate():
    """Spec §7.1: source: lab_candidate INHERITS from the dossier OR is
    explicitly provided in the ECR. Today's LabResult schema does not yet
    carry data_dependencies, so for now the lab_candidate path accepts an
    optional ECR-provided value (empty default is fine — no hard reject)."""
    from ops.engine_sdlc.ecr import parse_ecr
    txt = (
        "ECR\n"
        "action:        ADD\n"
        "engine:        lab_cand\n"
        "source:        lab_candidate\n"
        "lab_dossier:   docs/lab/2026-05-18-foo-SURVIVED-seed0.md\n"
        "cadence:       daily\n"
        "allocator:     false\n"
        "dispatch_order: 9\n"
        "need:          x\n"
    )
    # No data_dependencies — must parse cleanly (inheritable).
    ecr = parse_ecr(txt)
    assert ecr.source == "lab_candidate"
    assert ecr.data_dependencies is None
    # With explicit data_dependencies — must parse and carry the set.
    ecr2 = parse_ecr(txt + "data_dependencies: prices_daily\n")
    assert ecr2.data_dependencies == frozenset({"prices_daily"})


def test_data_dependencies_rejected_for_existing_code_when_empty_str():
    """An empty string after the colon means no values — same fail-closed
    treatment as a missing key for source: existing_code."""
    from ops.engine_sdlc.ecr import parse_ecr
    bad = _ADD_EXISTING_CODE_WITH_DEPS.replace(
        "data_dependencies: prices_daily, fundamentals_quarterly",
        "data_dependencies:")
    with pytest.raises(ValueError, match="data_dependencies.*required.*existing_code"):
        parse_ecr(bad)


def test_data_dependencies_rejected_on_remove():
    """``data_dependencies`` is valid on ADD (declares the engine's
    initial per-engine ``platform.<table>`` reads) and on MODIFY (the
    in-place accuracy-correction path — see the 2026-05-20 audit
    follow-up + ``_apply_modify``'s _PROFILE rewriter). It is NEVER
    valid on REMOVE — retiring an engine doesn't need a data-deps
    declaration."""
    from ops.engine_sdlc.ecr import parse_ecr
    bad_remove = _VALID_REMOVE + "data_dependencies: prices_daily\n"
    with pytest.raises(ValueError, match=r"data_dependencies|not valid for action REMOVE"):
        parse_ecr(bad_remove)


def test_data_dependencies_accepted_on_modify():
    """Spec §7 follow-up (2026-05-21, audit
    docs/superpowers/audits/2026-05-20-engine-data-dependencies-
    accuracy.md): ``data_dependencies`` is now valid on MODIFY (the
    in-place accuracy correction path — catalyst + momentum
    earnings_events). The CSV value is coerced to a frozenset[str] by
    the same _coerce path the ADD branch uses."""
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    good = _VALID_MODIFY + "data_dependencies: prices_daily, earnings_events\n"
    ecr = parse_ecr(good)
    assert ecr.action is ECRAction.MODIFY
    assert ecr.data_dependencies == frozenset(
        {"prices_daily", "earnings_events"})


def test_need_accepted_on_modify():
    """Spec §7 follow-up (2026-05-21): ``need`` is now valid on MODIFY
    so an in-place change can carry the operator-readable rationale
    free-text symmetrically with ADD (the staged ECRs in the 2026-05-20
    audit already use it). It remains forbidden on REMOVE."""
    from ops.engine_sdlc.ecr import ECRAction, parse_ecr
    good = _VALID_MODIFY + "need: accuracy correction per audit\n"
    ecr = parse_ecr(good)
    assert ecr.action is ECRAction.MODIFY
    assert ecr.need == "accuracy correction per audit"
    bad_remove = _VALID_REMOVE + "need: should be rejected on REMOVE\n"
    with pytest.raises(ValueError, match=r"need|not valid for action REMOVE"):
        parse_ecr(bad_remove)
