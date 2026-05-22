"""Sentinel: the deployed ``ops.lane_service`` daemon MUST NOT import
the Anthropic SDK — directly or transitively — AND the LLM-TRIAGE
modules MUST NOT exist in the repo.

Operator directives:
  * 2026-05-21 — "we wont be deploying the llm data triage it will run
    locally with my max account" (deployed daemon must be
    Anthropic-free).
  * 2026-05-22 — "we aren't going to use the llm triage... take it
    out" (the LLM-triage stack is REMOVED ENTIRELY).

A subprocess test is the only correct shape for the import-graph check.
The pytest collection process imports ``anthropic`` via sibling tests
(SP-G + Task #25) so checking ``sys.modules['anthropic']`` from
in-process would be a false positive. We spawn a clean
``python -c "import ops.lane_service"`` subprocess and inspect ITS
modules.

If the import-graph sentinel fails on a future change, the deployed
lane-service has re-acquired a transitive Anthropic SDK dependency —
fix it by either deferring the offending import to call-time or
removing the binding from ``ops/lane_service.py``.

If the deleted-modules sentinel fails, someone has re-introduced one
of the LLM-triage modules that was DELETED on 2026-05-22. Per operator
directive these MUST stay deleted — the deterministic cascade catalog
(Waves 1-4 + sentinel) is the COMPLETE self-heal layer.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Pinned to the ops-shadow worker for sibling parity (a future change
# that loads ``ops`` package + ``scripts/ops.py`` from this subprocess
# would race the same way the other ops-shadow tests do).
pytestmark = pytest.mark.xdist_group("ops_shadow")


_REPO = Path(__file__).resolve().parents[1]

# Modules that were DELETED on 2026-05-22 (operator directive). These
# must NOT be importable anywhere in the repo — if any of them is, the
# directive has been violated.
_DELETED_LLM_TRIAGE_MODULES: tuple[str, ...] = (
    "ops.llm_data_recovery",
    "ops.llm_data_triage",
    "ops.engine_llm_triage",
    "tpcore.llm_data_triage",
    "tpcore.engine_llm_triage",
    "tpcore.llm_data_triage.fence",
    "tpcore.llm_data_triage.packet",
    "tpcore.llm_data_triage.select",
    "tpcore.llm_data_triage.canary",
    "tpcore.engine_llm_triage.fence",
    "tpcore.engine_llm_triage.packet",
    "tpcore.engine_llm_triage.select",
)


def test_lane_service_does_not_import_anthropic() -> None:
    """A clean Python subprocess imports ``ops.lane_service`` and prints
    whether ``anthropic`` is in ``sys.modules``. If it is, the deployed
    daemon has a transitive LLM-SDK leak — operator directive
    2026-05-21 says it MUST NOT.

    The script also asserts that none of the SP-G / Task #25 / triage
    modules are loaded by the deployed daemon's import graph (the
    triage modules below are also DELETED — see the sibling test —
    so any leak would be a regression on TWO directives).
    """
    script = (
        "import sys\n"
        "import ops.lane_service  # noqa: F401\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'anthropic' or m.startswith('anthropic.')\n"
        ")\n"
        "deployed_llm_modules = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m in {\n"
        "        'ops.llm_triage_service',\n"
        "        'ops.llm_data_recovery',\n"
        "        'ops.engine_llm_triage',\n"
        "        'ops.llm_lab_emitter',\n"
        "        'ops.llm_data_triage',\n"
        "        'ops.llm_edge_finder',\n"
        "        'ops.llm_edge_finder_sdk',\n"
        "        'ops.llm_finder_outcome_monitor',\n"
        "    }\n"
        ")\n"
        "print('ANTHROPIC_LEAK=' + ','.join(leaked))\n"
        "print('LLM_MODULES_LEAK=' + ','.join(deployed_llm_modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        "import ops.lane_service failed in subprocess.\n"
        f"stdout={proc.stdout!r}\n"
        f"stderr={proc.stderr!r}"
    )
    out = proc.stdout
    leak_line = next(
        (line for line in out.splitlines() if line.startswith("ANTHROPIC_LEAK=")),
        None,
    )
    llm_line = next(
        (line for line in out.splitlines() if line.startswith("LLM_MODULES_LEAK=")),
        None,
    )
    assert leak_line is not None and llm_line is not None, (
        f"sentinel echo lines missing. stdout={out!r}"
    )
    leaked = leak_line.split("=", 1)[1]
    llm_leaked = llm_line.split("=", 1)[1]

    assert leaked == "", (
        f"Anthropic SDK transitively imported by the deployed lane-service: "
        f"{leaked!r}. Per operator directive 2026-05-21 the deployed daemon "
        "must not call Anthropic at runtime."
    )
    assert llm_leaked == "", (
        f"LLM-invoking ops modules transitively loaded by lane-service: "
        f"{llm_leaked!r}. The deployed daemon's import graph must stay "
        "deterministic-only."
    )


@pytest.mark.parametrize("module_name", _DELETED_LLM_TRIAGE_MODULES)
def test_deleted_llm_triage_modules_are_not_importable(module_name: str) -> None:
    """Sentinel: each LLM-triage module DELETED on 2026-05-22 must
    raise ``ModuleNotFoundError`` when imported. Operator directive
    "we aren't going to use the llm triage... take it out" — the
    deterministic cascade catalog is the COMPLETE self-heal layer.

    A subprocess is used so a sibling test that (incorrectly) put the
    module name into ``sys.modules`` does not produce a false negative.
    """
    script = (
        f"try:\n"
        f"    __import__({module_name!r})\n"
        f"    print('IMPORTABLE')\n"
        f"except ModuleNotFoundError:\n"
        f"    print('MISSING_OK')\n"
        f"except ImportError as exc:\n"
        # Distinguish ModuleNotFoundError (good) from any other
        # ImportError (bad — partial module / broken consumer).
        f"    print('IMPORT_ERROR=' + type(exc).__name__ + ':' + str(exc))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"subprocess crashed importing {module_name!r}: stderr={proc.stderr!r}"
    )
    out = proc.stdout.strip()
    assert out == "MISSING_OK", (
        f"DELETED LLM-triage module {module_name!r} is importable again "
        f"({out!r}). Per operator directive 2026-05-22 the LLM-triage stack "
        "is REMOVED ENTIRELY; the deterministic cascade catalog is the "
        "COMPLETE self-heal layer with no LLM backstop."
    )
