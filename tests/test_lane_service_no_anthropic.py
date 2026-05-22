"""Sentinel: the deployed ``ops.lane_service`` daemon MUST NOT import
the Anthropic SDK — directly or transitively (2026-05-22, operator
directive "we wont be deploying the llm data triage it will run locally
with my max account").

A subprocess test is the only correct shape here. The pytest collection
process has already imported ``anthropic`` via sibling tests (e.g.
``tests/test_llm_triage_service.py``) so checking
``sys.modules['anthropic']`` from in-process would be a false positive.
We spawn a clean ``python -c "import ops.lane_service"`` subprocess and
inspect ITS modules.

If this test fails on a future change, the deployed lane-service has
re-acquired a transitive Anthropic SDK dependency — fix it by either
deferring the offending import to call-time or removing the binding
from ``ops/lane_service.py`` (the import-graph entrypoint of the
deployed daemon). See
``docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md``
for the architectural reasoning.
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


def test_lane_service_does_not_import_anthropic() -> None:
    """A clean Python subprocess imports ``ops.lane_service`` and prints
    whether ``anthropic`` is in ``sys.modules``. If it is, the deployed
    daemon has a transitive LLM-SDK leak — operator directive
    2026-05-21 says it MUST NOT.

    The script also asserts ``ops.llm_triage_service`` / the LLM
    modules are NOT loaded by the deployed daemon's import graph.
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
    # Parse the two echo lines.
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
        "must not call Anthropic at runtime. See "
        "docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md"
    )
    assert llm_leaked == "", (
        f"LLM-invoking ops modules transitively loaded by lane-service: "
        f"{llm_leaked!r}. The deployed daemon's import graph must stay "
        "deterministic-only."
    )
