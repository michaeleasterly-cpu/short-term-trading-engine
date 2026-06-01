#!/usr/bin/env python3
"""C0.5 (2026-06-01) — Claude session/cost report (manual, metadata-only).

Reads Claude Code session ``*.jsonl`` files in a local input
directory and emits a redacted metadata-only summary report. Never
copies transcript content, prompts, tool arguments, tool results,
attachment payloads, or file-history snapshots into the report.

Stdlib only. No network. No Anthropic API. No memstore writes. No
DB writes. No daemon. Manual invocation only. See
``docs/CLAUDE_SESSION_OBSERVABILITY.md`` for the full policy.

Run via the wrapper:

    ./scripts/run_claude_session_report.sh

Or directly:

    python scripts/claude_session_report.py [--dry-run]
                                            [--format json|markdown]
                                            [--best-effort]
                                            [--input-dir <path>]
                                            [--output-dir <path>]
                                            [--max-files <N>]
                                            [--max-redactions <N>]
                                            [--include-ai-titles]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

_VERSION_LABEL = "c0.5"

# Hardcoded Claude API list-published rate snapshot (USD per 1 M
# tokens). Refresh deliberately when Anthropic publishes new rates;
# the script records ``rate_snapshot_date`` in every report so an
# operator can audit which rate table produced a given number.
#
# Rates as of 2026-06-01 (USD / 1M tokens):
#   * Opus 4.x:    input $15.00 / output $75.00 / cache-write $18.75 / cache-read $1.50
#   * Sonnet 4.x:  input  $3.00 / output $15.00 / cache-write  $3.75 / cache-read $0.30
#   * Haiku 4.x:   input  $1.00 / output  $5.00 / cache-write  $1.25 / cache-read $0.10
#
# Unknown model name => the per-model estimated_cost_usd is null +
# a warning. Aggregate cost is the sum of known-model costs only.
_RATE_SNAPSHOT_DATE = "2026-06-01"
_COST_RATE_SNAPSHOT: dict[str, dict[str, float]] = {
    # Family-level entries — matched as case-insensitive substring
    # against the assistant ``message.model`` string. Order matters:
    # the most-specific match wins.
    "claude-opus-4": {
        "input": 15.00, "output": 75.00,
        "cache_creation_input": 18.75, "cache_read_input": 1.50,
    },
    "claude-sonnet-4": {
        "input": 3.00, "output": 15.00,
        "cache_creation_input": 3.75, "cache_read_input": 0.30,
    },
    "claude-haiku-4": {
        "input": 1.00, "output": 5.00,
        "cache_creation_input": 1.25, "cache_read_input": 0.10,
    },
}

# Whitelist: every top-level report field. Anything else surfaces a
# defect. The sentinel test
# ``test_json_top_level_fields_are_whitelisted`` pins this.
_REPORT_TOP_LEVEL_FIELDS: tuple[str, ...] = (
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

# Recognized jsonl event types. Anything else is a fail-closed
# defect unless ``--best-effort`` is passed.
#
# Routing / metadata event types (``pr-link``, ``worktree-state``,
# ``custom-title``, ``mode``, ``agent-name``) carry only project /
# session identifiers (URLs, mode enums, session UUIDs). Their bodies
# are NEVER walked — the script records only ``sessionId`` +
# ``timestamp`` for these types, same as for ``permission-mode`` and
# ``last-prompt``. Adding them here recognizes the type so default
# fail-closed mode doesn't trip on the observed Claude Code session
# schema; it does NOT widen the data surface read into the report.
_KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    "assistant",
    "user",
    "system",
    "attachment",
    "ai-title",
    "last-prompt",
    "permission-mode",
    "queue-operation",
    "file-history-snapshot",
    # C0.5 fix (2026-06-01) — observed-but-routing metadata types.
    # Bodies are NOT walked; only sessionId + timestamp are used.
    "pr-link",
    "worktree-state",
    "custom-title",
    "mode",
    "agent-name",
})

# Secret-shape regex panel. Defense-in-depth on whitelisted string
# values — even if a forbidden field somehow leaked into the
# extraction, the redactor catches a real-secret-shaped value.
_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"sk-[A-Za-z0-9_\-]{24,}", "anthropic-style-key"),
    (r"sk_(live|test)_[A-Za-z0-9]{24,}", "stripe-style-key"),
    (r"gh[psorau]_[A-Za-z0-9_]{32,}", "github-token"),
    (r"xox[bporas]-[A-Za-z0-9-]{10,}", "slack-token"),
    (r"AKIA[0-9A-Z]{16}", "aws-access-key-id"),
    (r"postgres(ql)?://[^\s:@/]+:[^\s@]+@", "postgres-url-with-credentials"),
    (r"mysql://[^\s:@/]+:[^\s@]+@", "mysql-url-with-credentials"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", "bearer-token"),
    (r"(?i)password\s*[:=]\s*[^\s'\"]{8,}", "password-assignment"),
    (r"(?i)api[_\-\s]?key\s*[:=]\s*[A-Za-z0-9_\-]{16,}", "api-key-assignment"),
    (r"-----BEGIN\s+(RSA|DSA|EC|OPENSSH|PGP)[\sA-Z]*PRIVATE KEY-----", "private-key-block"),
)


def _redact_if_secret(value: str) -> tuple[str, int]:
    """Return (possibly-redacted-value, redaction-count-for-this-string)."""
    if not isinstance(value, str):
        return value, 0
    redactions = 0
    result = value
    for pattern, label in _SECRET_PATTERNS:
        new = re.sub(pattern, f"<REDACTED:{label}>", result)
        if new != result:
            redactions += len(re.findall(pattern, result))
            result = new
    return result, redactions


def _git_safe(cmd: list[str]) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=5,
        )
        return out.stdout.strip() or "<unknown>"
    except (OSError, subprocess.SubprocessError):
        return "<unknown>"


def _default_input_dir() -> Path:
    return Path.home() / ".claude" / "projects" / (
        "-Users-michael-short-term-trading-engine"
    )


def _default_output_dir(repo_root: Path) -> Path:
    return repo_root / ".operator" / "reports" / "claude"


def _script_sha256() -> str:
    here = Path(__file__).resolve()
    return hashlib.sha256(here.read_bytes()).hexdigest()


def _pick_rate(model_name: str) -> dict[str, float] | None:
    lowered = model_name.lower()
    # Longest-key match wins (so claude-opus-4 wins over a future
    # bare ``claude-opus``).
    for key in sorted(_COST_RATE_SNAPSHOT, key=len, reverse=True):
        if key in lowered:
            return _COST_RATE_SNAPSHOT[key]
    return None


def _aggregate_usage(model_usage: dict[str, dict[str, int]]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for usage in model_usage.values():
        for k, v in usage.items():
            total[k] += int(v or 0)
    return dict(total)


def _estimate_costs(
    model_usage: dict[str, dict[str, int]],
    warning_counts: Counter[str],
) -> tuple[dict[str, float | None], float | None]:
    per_model: dict[str, float | None] = {}
    aggregate: float | None = 0.0
    saw_unknown = False
    for model_name, usage in model_usage.items():
        rate = _pick_rate(model_name)
        if rate is None:
            per_model[model_name] = None
            warning_counts[
                f"unknown model rate for {model_name!r}; "
                "estimated_cost_usd for this model is null"
            ] += 1
            saw_unknown = True
            continue
        cost = 0.0
        # Per-1M-token math.
        for token_key, rate_key in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_creation_input_tokens", "cache_creation_input"),
            ("cache_read_input_tokens", "cache_read_input"),
        ):
            tokens = int(usage.get(token_key, 0) or 0)
            cost += (tokens / 1_000_000.0) * rate[rate_key]
        per_model[model_name] = round(cost, 6)
        if aggregate is not None:
            aggregate += cost
    if saw_unknown and aggregate == 0.0 and not any(per_model.values()):
        aggregate = None
    elif aggregate is not None:
        aggregate = round(aggregate, 6)
    return per_model, aggregate


def _scan_event(
    evt: dict,
    *,
    include_ai_titles: bool,
    tool_call_counts: Counter[str],
    model_usage: dict[str, dict[str, int]],
    model_names: set[str],
    session_ids: set[str],
    timestamps: list[str],
    redacted_strings: list[tuple[str, int]],
    warning_counts: Counter[str],
    best_effort: bool,
) -> None:
    """Walk a single jsonl event. ONLY whitelisted metadata reaches
    the report — no content text, no tool args, no tool results.

    Warnings are accumulated into ``warning_counts`` (a Counter) so
    a session jsonl with thousands of routing-metadata events of one
    unknown type produces one aggregated warning instead of thousands
    of per-event lines.
    """
    evt_type = evt.get("type")
    if evt_type not in _KNOWN_EVENT_TYPES:
        msg = f"unknown event type {evt_type!r}"
        if best_effort:
            warning_counts[msg] += 1
            return
        raise ValueError(
            f"{msg}; re-run with --best-effort to skip (fail-closed default)"
        )

    sid = evt.get("sessionId")
    if isinstance(sid, str):
        session_ids.add(sid)
    ts = evt.get("timestamp")
    if isinstance(ts, str):
        timestamps.append(ts)

    if evt_type == "assistant":
        msg = evt.get("message")
        if not isinstance(msg, dict):
            return
        model_raw = msg.get("model")
        model_key: str | None = None
        if isinstance(model_raw, str) and model_raw:
            redacted, n = _redact_if_secret(model_raw)
            if n:
                redacted_strings.append(
                    (f"assistant.model[{redacted[:8]}…]", n),
                )
            # ALL downstream surfaces use the redacted form so a raw
            # secret in the model field cannot leak via model_usage,
            # per_model_cost, or model_names.
            model_key = redacted
            model_names.add(redacted)
        usage = msg.get("usage")
        if isinstance(usage, dict) and model_key is not None:
            slot = model_usage.setdefault(model_key, {})
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                v = usage.get(k)
                if isinstance(v, int):
                    slot[k] = slot.get(k, 0) + v
        # Tool-name counting ONLY. Never read tool input or result.
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str) and name:
                        redacted, n = _redact_if_secret(name)
                        if n:
                            redacted_strings.append(
                                (f"tool_use.name[{name[:8]}…]", n),
                            )
                        tool_call_counts[redacted] += 1
        return

    if evt_type == "ai-title" and include_ai_titles:
        title = evt.get("aiTitle")
        if isinstance(title, str):
            _, n = _redact_if_secret(title)
            if n:
                redacted_strings.append(("ai-title", n))
            # ai-title body is NOT recorded in the report even when
            # included — we count the event only.
        warning_counts[
            "ai-title events were considered via --include-ai-titles "
            "(operator opt-in)"
        ] += 1
    # All other event types are counted via timestamps / session_ids
    # above. Their bodies are NEVER walked.


def _build_report(
    args: argparse.Namespace,
    *,
    repo_root: Path,
    output_path: Path,
) -> dict:
    input_dir = Path(args.input_dir).expanduser().resolve()
    if not input_dir.is_dir():
        raise SystemExit(
            f"input dir not found: {input_dir}"
        )
    jsonl_files = sorted(input_dir.glob("*.jsonl"))
    if len(jsonl_files) > args.max_files:
        raise SystemExit(
            f"refusing to read {len(jsonl_files)} files (max={args.max_files}); "
            "raise --max-files explicitly if intended"
        )

    tool_call_counts: Counter[str] = Counter()
    model_usage: dict[str, dict[str, int]] = {}
    model_names: set[str] = set()
    session_ids: set[str] = set()
    timestamps: list[str] = []
    redacted_strings: list[tuple[str, int]] = []
    warning_counts: Counter[str] = Counter()

    for jsonl in jsonl_files:
        try:
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        warning_counts[
                            f"malformed jsonl line in {jsonl.name}"
                        ] += 1
                        continue
                    _scan_event(
                        evt,
                        include_ai_titles=args.include_ai_titles,
                        tool_call_counts=tool_call_counts,
                        model_usage=model_usage,
                        model_names=model_names,
                        session_ids=session_ids,
                        timestamps=timestamps,
                        redacted_strings=redacted_strings,
                        warning_counts=warning_counts,
                        best_effort=args.best_effort,
                    )
        except (OSError, UnicodeDecodeError) as exc:
            warning_counts[f"failed to read {jsonl.name}: {exc}"] += 1

    redaction_count = sum(n for _, n in redacted_strings)
    if redaction_count > args.max_redactions:
        raise SystemExit(
            f"redaction_count={redaction_count} exceeds --max-redactions"
            f"={args.max_redactions}; aborting to avoid surfacing a "
            "report that may still carry sensitive shapes"
        )

    per_model_cost, agg_cost = _estimate_costs(model_usage, warning_counts)
    aggregate_token_counts = _aggregate_usage(model_usage)

    # Flatten the Counter into ordered "<message> (N events)" lines so
    # a session jsonl with thousands of routing-metadata events
    # surfaces ONE aggregated warning instead of thousands of per-event
    # lines (PR #415 fix).
    warnings: list[str] = [
        f"{msg} (encountered {n} time{'s' if n != 1 else ''})"
        if n > 1
        else msg
        for msg, n in sorted(
            warning_counts.items(), key=lambda kv: (-kv[1], kv[0]),
        )
    ]

    report = {
        "report_generated_at": datetime.now(UTC).isoformat(),
        "repo": repo_root.name,
        "git_branch": _git_safe(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit": _git_safe(["git", "-C", str(repo_root), "rev-parse", "HEAD"]),
        "session_file_count": len(jsonl_files),
        "session_date_range": {
            "min": min(timestamps) if timestamps else None,
            "max": max(timestamps) if timestamps else None,
        },
        "estimated_total_sessions": len(session_ids),
        "tool_call_counts_by_tool_name": dict(
            sorted(tool_call_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "model_names": sorted(model_names),
        "token_counts": {
            "per_model": model_usage,
            "aggregate": aggregate_token_counts,
        },
        "estimated_cost_usd": {
            "per_model": per_model_cost,
            "aggregate": agg_cost,
        },
        "cost_rate_snapshot": {
            "rate_snapshot_date": _RATE_SNAPSHOT_DATE,
            "rates_usd_per_1m_tokens": _COST_RATE_SNAPSHOT,
            "warning": (
                "Rates are a hardcoded snapshot; refresh "
                "scripts/claude_session_report.py::_COST_RATE_SNAPSHOT "
                "deliberately when Anthropic publishes new rates."
            ),
        },
        "redaction_count": redaction_count,
        "warnings": warnings,
        "input_source_paths": [str(p) for p in jsonl_files],
        "output_report_path": str(output_path) if not args.dry_run else None,
        "version_label": _VERSION_LABEL,
        "script_sha256": _script_sha256(),
    }
    return report


def _markdown_render(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Claude session report — {report['report_generated_at']}")
    lines.append("")
    lines.append(f"- **repo**: `{report['repo']}`")
    lines.append(
        f"- **git**: `{report['git_branch']}` @ "
        f"`{report['git_commit'][:12]}…`"
    )
    lines.append(
        f"- **input source paths**: {len(report['input_source_paths'])} file(s)"
    )
    lines.append(
        f"- **session files**: {report['session_file_count']}"
    )
    lines.append(
        f"- **distinct sessions**: {report['estimated_total_sessions']}"
    )
    rng = report["session_date_range"]
    lines.append(f"- **date range**: {rng['min']} → {rng['max']}")
    lines.append(f"- **redactions**: {report['redaction_count']}")
    lines.append(f"- **script_sha256**: `{report['script_sha256'][:16]}…`")
    lines.append(f"- **version_label**: `{report['version_label']}`")
    lines.append("")

    lines.append("## Token counts (aggregate)")
    for k, v in report["token_counts"].get("aggregate", {}).items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")

    lines.append("## Estimated cost (USD)")
    agg = report["estimated_cost_usd"].get("aggregate")
    lines.append(
        "- **aggregate**: " + ("null" if agg is None else f"${agg}")
    )
    for model, cost in report["estimated_cost_usd"].get("per_model", {}).items():
        lines.append(
            f"- `{model}`: " + ("null" if cost is None else f"${cost}")
        )
    snap = report["cost_rate_snapshot"]
    lines.append(f"- **rate_snapshot_date**: {snap['rate_snapshot_date']}")
    lines.append(f"- _{snap['warning']}_")
    lines.append("")

    lines.append("## Tool-call counts (names only — never arguments or results)")
    for name, n in report["tool_call_counts_by_tool_name"].items():
        lines.append(f"- `{name}`: {n}")
    lines.append("")

    if report["warnings"]:
        lines.append("## Warnings")
        for w in report["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _write_report(
    report: dict, output_path: Path, *, fmt: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
    elif fmt == "markdown":
        output_path.write_text(_markdown_render(report), encoding="utf-8")
    else:
        raise SystemExit(f"unsupported --format: {fmt!r}")


def _print_summary(report: dict, *, dry_run: bool) -> None:
    agg_tok = report["token_counts"].get("aggregate", {})
    agg_cost = report["estimated_cost_usd"].get("aggregate")
    print(
        f"claude_session_report: "
        f"{report['session_file_count']} files, "
        f"{report['estimated_total_sessions']} sessions, "
        f"{sum(agg_tok.values())} total tokens, "
        f"estimated ${'null' if agg_cost is None else agg_cost} USD"
    )
    print(f"  redactions: {report['redaction_count']}")
    if report["warnings"]:
        print(f"  warnings: {len(report['warnings'])} (see report)")
    if dry_run:
        print("  dry-run: no file written")
    else:
        print(f"  output: {report['output_report_path']}")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude_session_report",
        description=(
            "Manual, redacted metadata-only Claude session/cost report. "
            "See docs/CLAUDE_SESSION_OBSERVABILITY.md."
        ),
    )
    p.add_argument(
        "--input-dir",
        default=str(_default_input_dir()),
        help=(
            "Local Claude project session directory; default is "
            "~/.claude/projects/-Users-michael-short-term-trading-engine/"
        ),
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Where to write the report. Default is "
            ".operator/reports/claude/ at the repo root (gitignored)."
        ),
    )
    p.add_argument(
        "--format", choices=("json", "markdown"), default="json",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Emit summary only; never write to disk.",
    )
    p.add_argument(
        "--best-effort", action="store_true",
        help=(
            "Gracefully skip unknown event types with a warning. "
            "Default is OFF (fail-closed on schema drift)."
        ),
    )
    p.add_argument(
        "--include-ai-titles", action="store_true",
        help=(
            "Opt in to counting aiTitle events (their bodies are "
            "still never recorded). Default OFF — aiTitle can leak "
            "a prompt summary."
        ),
    )
    p.add_argument(
        "--max-files", type=int, default=200,
        help="Refuse to read more than N input jsonl files (default 200).",
    )
    p.add_argument(
        "--max-redactions", type=int, default=10,
        help=(
            "Fail closed if redaction count exceeds this (default 10). "
            "A high redaction count signals a leak elsewhere."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else _default_output_dir(repo_root)
    )
    timestamp_slug = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "md" if args.format == "markdown" else "json"
    output_path = output_dir / f"claude-session-report-{timestamp_slug}.{suffix}"

    report = _build_report(
        args, repo_root=repo_root, output_path=output_path,
    )

    if not args.dry_run:
        _write_report(report, output_path, fmt=args.format)
    _print_summary(report, dry_run=args.dry_run)

    # Final schema-of-output gate. Catches a bug where a future
    # refactor introduces an unwhitelisted top-level field.
    extra = set(report) - set(_REPORT_TOP_LEVEL_FIELDS)
    if extra:
        print(
            f"ERROR: report contains unwhitelisted top-level fields: "
            f"{sorted(extra)}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
