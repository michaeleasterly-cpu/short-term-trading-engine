"""Sentinel — the bulk-before-crawl ENFORCEMENT hook must stay wired.

Anthropic guidance (code.claude.com/docs/en/memory): CLAUDE.md + memory are
context, not enforcement; a rule that must hold every time belongs in a
PreToolUse hook. The "download to CSV first, then ETL — never a per-entity
vendor API crawl, and rate-limit any fetch so it can't lock us out" rule was
violated three times despite living in CLAUDE.md + memory. `.claude/hooks/
block-unbulked-vendor-crawl.sh` is the deterministic block. This test reds CI
if the hook file goes missing, loses its executable bit, falls out of the
settings.json PreToolUse(Bash) registration, or stops blocking the crawl
pattern (regression that would let the drift recur silently).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".claude" / "hooks" / "block-unbulked-vendor-crawl.sh"
SETTINGS = REPO / ".claude" / "settings.json"


def test_hook_file_present_and_executable() -> None:
    assert HOOK.is_file(), f"enforcement hook missing: {HOOK}"
    assert os.access(HOOK, os.X_OK), f"hook not executable: {HOOK}"


def test_hook_registered_in_pretooluse_bash() -> None:
    cfg = json.loads(SETTINGS.read_text())
    pre = cfg["hooks"]["PreToolUse"]
    bash = next(m for m in pre if m.get("matcher") == "Bash")
    cmds = [h["command"] for h in bash["hooks"]]
    assert any("block-unbulked-vendor-crawl.sh" in c for c in cmds), (
        "block-unbulked-vendor-crawl.sh not registered in PreToolUse(Bash)"
    )


def _decision(command: str) -> str:
    """Run the hook with a tool_input.command; return the permissionDecision or 'allow'."""
    out = subprocess.run(
        [str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    if not out:
        return "allow"
    return json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def test_blocks_per_entity_vendor_crawl() -> None:
    crawl = ('for t in $(cat tickers.txt); do '
             'curl "https://financialmodelingprep.com/api/v3/historical-price-full/$t?apikey=K"; done')
    assert _decision(crawl) == "deny", "per-ticker FMP crawl must be DENIED"


def test_allows_single_call_bulk_and_guarded_loop() -> None:
    # single call (no loop) — allowed
    assert _decision('curl "https://financialmodelingprep.com/stable/quote?symbol=AAPL&apikey=K"') == "allow"
    # bulk batch-EOD loop over DATES — allowed (the correct path)
    assert _decision('for d in 2026-06-04 2026-06-05; do curl ".../stable/batch-eod?date=$d&apikey=K"; done') == "allow"
    # rate-guarded per-ticker loop — allowed
    assert _decision('for t in AAPL MSFT; do curl "https://data.alpaca.markets/v2/stocks/$t/bars"; sleep 1; done') == "allow"
    # ordinary commands — allowed
    assert _decision("git status") == "allow"
    assert _decision("python scripts/ops.py --stage daily_bars") == "allow"


def test_kill_switch_allows_deliberate_override() -> None:
    crawl = ('for t in $(cat tickers.txt); do '
             'curl "https://financialmodelingprep.com/api/v3/historical-price-full/$t?apikey=K"; done')
    out = subprocess.run(
        [str(HOOK)],
        input=json.dumps({"tool_input": {"command": crawl}}),
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "STE_ALLOW_VENDOR_CRAWL": "1"},
    ).stdout.strip()
    assert out == "", "STE_ALLOW_VENDOR_CRAWL=1 must let a deliberate one-off through"
