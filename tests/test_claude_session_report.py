"""C0.5 (2026-06-01) — Claude session/cost report sentinels.

Stdlib-only contract tests for ``scripts/claude_session_report.py``.
Generates synthetic ``*.jsonl`` fixtures under ``tmp_path`` and runs
the script as a subprocess so the test sees exactly what an
operator would see.

Pins:
  * no network / API / memstore imports in the source
  * no Anthropic API URL or memstore endpoint substrings
  * fake raw prompt content NEVER reaches the report
  * tool ``input`` / ``result`` payloads NEVER reach the report
  * ``attachment`` and ``file-history-snapshot`` payloads excluded
  * secret-shape redaction works on whitelisted fields
  * dry-run writes no file
  * default output dir is gitignored
  * JSON top-level fields are exactly the whitelist
  * unknown event type fails closed without ``--best-effort``
  * ``--best-effort`` permits unknown events with a warning
  * ``--max-redactions`` enforces a fail-stop ceiling
  * markdown output contains no raw transcript content
  * ``aiTitle`` excluded by default; opt-in via flag
  * doc references the forbidden-data list + output dir + wrapper
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "claude_session_report.py"
_WRAPPER = _REPO / "scripts" / "run_claude_session_report.sh"
_DOC = _REPO / "docs" / "CLAUDE_SESSION_OBSERVABILITY.md"


# Token markers a sentinel hunts for; if any of these strings show
# up in any report output, the script leaked transcript content.
_PROMPT_MARKER = "PROMPT_BODY_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_RESPONSE_MARKER = "ASSISTANT_RESPONSE_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_TOOL_INPUT_MARKER = "TOOL_INPUT_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_TOOL_RESULT_MARKER = "TOOL_RESULT_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_ATTACHMENT_MARKER = "ATTACHMENT_PAYLOAD_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_SNAPSHOT_MARKER = "FILE_HISTORY_SNAPSHOT_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_QUEUE_MARKER = "QUEUE_OPERATION_CONTENT_MUST_NOT_LEAK_INTO_REPORT_C0_5"
_AI_TITLE_MARKER = "AI_TITLE_BODY_MUST_NOT_LEAK_INTO_REPORT_C0_5"


def _make_fixture_jsonl(
    dir_: Path,
    *,
    include_attachment: bool = True,
    include_snapshot: bool = True,
    include_queue: bool = True,
    include_ai_title: bool = True,
    include_secret_in_model: bool = False,
    include_unknown_event: bool = False,
    extra_redactions: int = 0,
) -> Path:
    """Generate a single synthetic session jsonl. Every event carries
    a marker string in a forbidden field so the test can prove the
    marker is excluded from the report."""
    f = dir_ / "session-fixture.jsonl"
    events: list[dict] = [
        {
            "type": "user",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": _PROMPT_MARKER}],
            },
        },
        {
            "type": "assistant",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:01Z",
            "message": {
                "model": (
                    "claude-opus-4-7-with-token-sk-"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
                ) if include_secret_in_model else "claude-opus-4-7",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 25,
                },
                "content": [
                    {"type": "text", "text": _RESPONSE_MARKER},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "tool_1",
                        "input": {"command": _TOOL_INPUT_MARKER},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "tool_2",
                        "input": {"file_path": _TOOL_INPUT_MARKER},
                    },
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_1",
                        "content": _TOOL_RESULT_MARKER,
                    },
                ],
            },
        },
    ]
    if include_attachment:
        events.append({
            "type": "attachment",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:03Z",
            "attachment": _ATTACHMENT_MARKER,
        })
    if include_snapshot:
        events.append({
            "type": "file-history-snapshot",
            "messageId": "fhs-1",
            "snapshot": _SNAPSHOT_MARKER,
        })
    if include_queue:
        events.append({
            "type": "queue-operation",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:04Z",
            "operation": "enqueue",
            "content": _QUEUE_MARKER,
        })
    if include_ai_title:
        events.append({
            "type": "ai-title",
            "sessionId": "synthetic-session-1",
            "aiTitle": _AI_TITLE_MARKER,
        })
    if include_unknown_event:
        events.append({
            "type": "synthetic-unknown-event-type",
            "sessionId": "synthetic-session-1",
            "timestamp": "2026-06-01T00:00:05Z",
        })
    # Extra redactable assistant events to drive --max-redactions.
    for i in range(extra_redactions):
        events.append({
            "type": "assistant",
            "sessionId": f"synthetic-session-r-{i}",
            "timestamp": f"2026-06-01T00:01:{i:02d}Z",
            "message": {
                "model": (
                    f"claude-opus-4-7 sk-AAAAAAAAAAAAAAAAAAAAAAAAA{i:02d}"
                ),
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [],
            },
        })
    f.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    return f


def _run(args: list[str], *, expect_rc: int = 0) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_SCRIPT), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == expect_rc, (
        f"unexpected rc={proc.returncode} for cmd={cmd}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


# ─────────────────────────────────────────────────────────────────────
# Static-import audit (no need to run the script)
# ─────────────────────────────────────────────────────────────────────

def test_script_present_and_runnable() -> None:
    assert _SCRIPT.is_file(), f"missing {_SCRIPT.relative_to(_REPO)}"
    src = _SCRIPT.read_text(encoding="utf-8")
    assert src.startswith("#!"), "script must have a shebang on line 1"
    mode = _SCRIPT.stat().st_mode
    assert mode & 0o111, "script must be executable (chmod +x)"


def test_wrapper_present_and_runnable() -> None:
    assert _WRAPPER.is_file(), f"missing {_WRAPPER.relative_to(_REPO)}"
    src = _WRAPPER.read_text(encoding="utf-8")
    assert src.startswith("#!"), "wrapper must have a shebang"
    mode = _WRAPPER.stat().st_mode
    assert mode & 0o111, "wrapper must be executable"


def test_script_has_no_network_or_api_imports() -> None:
    """AST-scan the script. Forbid any import that could call out to
    a network or to the Anthropic API."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    forbidden = {
        "requests", "httpx", "urllib3", "aiohttp", "anthropic",
        "asyncpg", "sqlalchemy", "psycopg", "psycopg2",
        "tpcore", "ops",
    }
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_names.add(node.module.split(".")[0])
    leaks = sorted(imported_names & forbidden)
    assert not leaks, (
        f"script imports forbidden modules: {leaks} — the report tool "
        "is stdlib-only by design"
    )


def test_script_has_no_anthropic_api_or_memstore_substrings() -> None:
    """Defense-in-depth: scan source text for the Anthropic API URL
    or memstore endpoint shapes."""
    src = _SCRIPT.read_text(encoding="utf-8")
    forbidden_substrings = (
        "api.anthropic.com",
        "/v1/memory_stores/",
        "/v1/messages",
        "/v1/complete",
    )
    leaks = [s for s in forbidden_substrings if s in src]
    assert not leaks, (
        f"script body references forbidden network endpoints: {leaks}"
    )


# ─────────────────────────────────────────────────────────────────────
# Content-leak prevention
# ─────────────────────────────────────────────────────────────────────

def test_fake_prompt_content_excluded_from_report(tmp_path: Path) -> None:
    """A synthetic transcript with a prompt body marker must produce
    a report that never contains the marker."""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "json",
    ])
    json_files = sorted(out_dir.glob("*.json"))
    assert json_files, "no report written"
    body = json_files[0].read_text(encoding="utf-8")
    for marker in (
        _PROMPT_MARKER, _RESPONSE_MARKER, _TOOL_INPUT_MARKER,
        _TOOL_RESULT_MARKER, _ATTACHMENT_MARKER, _SNAPSHOT_MARKER,
        _QUEUE_MARKER, _AI_TITLE_MARKER,
    ):
        assert marker not in body, (
            f"report leaked forbidden marker {marker!r}"
        )


def test_tool_arguments_and_results_excluded_from_report(tmp_path: Path) -> None:
    """The report records tool *names* (Bash, Read, etc.) but never
    tool inputs or tool results."""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "json",
    ])
    json_files = sorted(out_dir.glob("*.json"))
    report = json.loads(json_files[0].read_text(encoding="utf-8"))
    tool_counts = report["tool_call_counts_by_tool_name"]
    # Names show up; inputs/results don't (already covered by the
    # marker test). Verify counts are correct.
    assert tool_counts.get("Bash") == 1
    assert tool_counts.get("Read") == 1
    # And the counts dict shape must NOT include any value-typed
    # subfield (anything beyond {name: int} is a defect).
    assert all(isinstance(v, int) for v in tool_counts.values())


def test_attachment_and_snapshot_payloads_excluded(tmp_path: Path) -> None:
    """Even though the script counts these event types via timestamp
    and sessionId, the report must never copy their bodies."""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "json",
    ])
    body = (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8")
    assert _ATTACHMENT_MARKER not in body
    assert _SNAPSHOT_MARKER not in body
    assert _QUEUE_MARKER not in body


# ─────────────────────────────────────────────────────────────────────
# Secret-shape redaction
# ─────────────────────────────────────────────────────────────────────

def test_secret_shaped_value_in_whitelisted_field_is_redacted(
    tmp_path: Path,
) -> None:
    """A secret-shaped value in a whitelisted field (here, the
    assistant ``message.model`` string) must be redacted in the
    report and increment ``redaction_count``."""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, include_secret_in_model=True)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "json",
        "--max-redactions", "100",
    ])
    body = (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8")
    assert "sk-ABCDEFGH" not in body, (
        "raw secret-shape leaked into report"
    )
    assert "<REDACTED:" in body, (
        "redaction marker not present despite secret-shaped input"
    )
    report = json.loads(body)
    assert report["redaction_count"] >= 1


def test_max_redactions_fail_stop(tmp_path: Path) -> None:
    """When the redaction count exceeds ``--max-redactions``, the
    script must exit non-zero (defensive fail-stop)."""
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, extra_redactions=5)
    out_dir = tmp_path / "output"
    _run(
        [
            "--input-dir", str(in_dir),
            "--output-dir", str(out_dir),
            "--max-redactions", "2",
        ],
        expect_rc=1,
    )


# ─────────────────────────────────────────────────────────────────────
# Dry-run + output-path + format
# ─────────────────────────────────────────────────────────────────────

def test_dry_run_writes_no_report(tmp_path: Path) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    proc = _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--dry-run",
    ])
    assert "dry-run: no file written" in proc.stdout
    assert not out_dir.exists() or not list(out_dir.glob("*.json")), (
        "dry-run wrote a report file; it must not"
    )


def test_default_output_dir_is_gitignored() -> None:
    """The script's default output directory must be under a path
    that ``.gitignore`` excludes."""
    gitignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".operator/reports/claude/" in gitignore, (
        ".gitignore must exclude .operator/reports/claude/"
    )
    # Defense in depth — the bare report filename glob is also
    # gitignored so an --output-dir . invocation still doesn't leak.
    assert "claude-session-report*.json" in gitignore
    assert "claude-session-report*.md" in gitignore


def test_markdown_format_writes_markdown_without_raw_content(
    tmp_path: Path,
) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "markdown",
    ])
    md_files = sorted(out_dir.glob("*.md"))
    assert md_files, "no markdown report written"
    body = md_files[0].read_text(encoding="utf-8")
    for marker in (
        _PROMPT_MARKER, _RESPONSE_MARKER, _TOOL_INPUT_MARKER,
        _TOOL_RESULT_MARKER, _ATTACHMENT_MARKER, _SNAPSHOT_MARKER,
        _QUEUE_MARKER, _AI_TITLE_MARKER,
    ):
        assert marker not in body, (
            f"markdown report leaked forbidden marker {marker!r}"
        )
    # Sanity: the markdown does contain canonical section headers.
    assert "Claude session report" in body
    assert "Token counts" in body
    assert "Estimated cost" in body


# ─────────────────────────────────────────────────────────────────────
# JSON schema whitelist
# ─────────────────────────────────────────────────────────────────────

_REPORT_TOP_LEVEL_FIELDS = (
    "report_generated_at",
    "repo",
    "git_branch",
    "git_commit",
    "session_file_count",
    "session_date_range",
    "estimated_total_sessions",
    "tool_call_counts_by_tool_name",
    "model_names",
    "token_counts",
    "estimated_cost_usd",
    "cost_rate_snapshot",
    "redaction_count",
    "warnings",
    "input_source_paths",
    "output_report_path",
    "version_label",
    "script_sha256",
)


def test_json_top_level_fields_are_whitelisted(tmp_path: Path) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--format", "json",
    ])
    report = json.loads(
        (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8"),
    )
    actual = set(report.keys())
    expected = set(_REPORT_TOP_LEVEL_FIELDS)
    extra = actual - expected
    missing = expected - actual
    assert not extra, (
        f"report contains unwhitelisted top-level fields: {sorted(extra)}"
    )
    assert not missing, (
        f"report missing required top-level fields: {sorted(missing)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Schema-drift fail-closed + best-effort
# ─────────────────────────────────────────────────────────────────────

def test_unknown_event_type_fails_closed_without_best_effort(
    tmp_path: Path,
) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, include_unknown_event=True)
    out_dir = tmp_path / "output"
    _run(
        [
            "--input-dir", str(in_dir),
            "--output-dir", str(out_dir),
        ],
        expect_rc=1,
    )


def test_best_effort_permits_unknown_event_with_warning(tmp_path: Path) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, include_unknown_event=True)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--best-effort",
    ])
    report = json.loads(
        (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8"),
    )
    assert any(
        "synthetic-unknown-event-type" in w for w in report["warnings"]
    ), "unknown event type was not surfaced as a warning under --best-effort"


# ─────────────────────────────────────────────────────────────────────
# aiTitle policy
# ─────────────────────────────────────────────────────────────────────

def test_ai_title_excluded_by_default(tmp_path: Path) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, include_ai_title=True)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
    ])
    body = (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8")
    # The aiTitle body must NOT be in the report.
    assert _AI_TITLE_MARKER not in body
    # And there must be no warning about --include-ai-titles being
    # active.
    report = json.loads(body)
    assert not any(
        "include-ai-titles" in w for w in report["warnings"]
    ), "--include-ai-titles warning fired without the flag being passed"


def test_ai_title_opt_in_emits_warning(tmp_path: Path) -> None:
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    _make_fixture_jsonl(in_dir, include_ai_title=True)
    out_dir = tmp_path / "output"
    _run([
        "--input-dir", str(in_dir),
        "--output-dir", str(out_dir),
        "--include-ai-titles",
    ])
    body = (sorted(out_dir.glob("*.json"))[0]).read_text(encoding="utf-8")
    # The body is STILL not in the report (we never store transcript
    # text, even for opt-in event types).
    assert _AI_TITLE_MARKER not in body
    # But the operator opt-in is recorded as a warning so the report
    # carries a footprint of the decision.
    report = json.loads(body)
    assert any(
        "include-ai-titles" in w for w in report["warnings"]
    ), "opt-in flag did not emit the operator-opt-in warning"


# ─────────────────────────────────────────────────────────────────────
# Doc presence + content
# ─────────────────────────────────────────────────────────────────────

def test_doc_present_and_names_forbidden_data_model() -> None:
    assert _DOC.is_file(), f"missing {_DOC.relative_to(_REPO)}"
    text = _DOC.read_text(encoding="utf-8")
    assert text.strip(), "observability doc is empty"
    # Forbidden data list — the doc must name the dangerous shapes.
    for marker in (
        "raw transcript",
        "API key",
        "Postgres",
        "broker credential",
    ):
        assert marker.lower() in text.lower(), (
            f"observability doc must mention {marker!r}"
        )


def test_doc_references_output_dir_and_wrapper_script() -> None:
    text = _DOC.read_text(encoding="utf-8")
    assert ".operator/reports/claude/" in text, (
        "doc must reference the gitignored output dir"
    )
    assert "scripts/run_claude_session_report.sh" in text, (
        "doc must reference the wrapper script"
    )
    assert "scripts/claude_session_report.py" in text, (
        "doc must reference the underlying report script"
    )
