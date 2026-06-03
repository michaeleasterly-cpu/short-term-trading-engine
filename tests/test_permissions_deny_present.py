"""Sentinel for the ``.claude/settings.json`` ``permissions.deny`` block.

Pins the load-bearing deny entries added 2026-06-04 per controls-audit
§13 #5 (``docs/audits/2026-06-03-claude-code-workflow-controls.md``).

The deny list is Anthropic's documented second layer of defense — it
is enforced by Claude Code itself (not by ``.claude/hooks/*.sh``;
hooks can be bypassed with env-var overrides, deny rules cannot —
``docs/audits/2026-06-03-claude-code-workflow-controls.md`` §5).

Substring-presence + structure checks. The deny list can grow; this
sentinel only fails if a load-bearing entry is removed silently.

Per ``.claude/rules/tests-and-ci.md``: this test runs no ``git``,
``gh``, or DB access — pure JSON read of the tracked settings file.

Authoritative external:
  - https://code.claude.com/docs/en/permissions
  - https://code.claude.com/docs/en/settings
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SETTINGS = _REPO / ".claude" / "settings.json"


def _settings() -> dict:
    assert _SETTINGS.is_file(), f"missing {_SETTINGS.relative_to(_REPO)}"
    text = _SETTINGS.read_text(encoding="utf-8")
    assert text.strip(), f"{_SETTINGS.relative_to(_REPO)} is empty"
    return json.loads(text)


def _deny_list() -> list[str]:
    data = _settings()
    perms = data.get("permissions", {})
    assert isinstance(perms, dict), (
        "settings.json `permissions` must be an object — see "
        "https://code.claude.com/docs/en/permissions"
    )
    deny = perms.get("deny", [])
    assert isinstance(deny, list), (
        "settings.json `permissions.deny` must be a list of rule strings"
    )
    assert deny, (
        "settings.json `permissions.deny` must be a non-empty list — "
        "controls-audit #5 added it 2026-06-04 as the canonical second "
        "layer of defense (cannot be bypassed by env-var overrides the "
        "way hooks can)"
    )
    return deny


# ---------------------------------------------------------------------------
# 1. Structural — settings.json is well-formed and has a permissions block
# ---------------------------------------------------------------------------


def test_settings_has_permissions_deny_block() -> None:
    _deny_list()


def test_settings_documents_why_via_comment_key() -> None:
    """``$comment_permissions`` explains the design choice + cites the
    audit + names the sentinel — future maintainers should be able to
    read the JSON and understand the rationale without grepping audits."""
    data = _settings()
    assert "$comment_permissions" in data, (
        "settings.json must carry a $comment_permissions key explaining "
        "the deny list design (Anthropic-canonical second layer, hooks "
        "can be bypassed, deny rules cannot)"
    )
    comment = data["$comment_permissions"]
    assert "controls-audit #5" in comment, (
        "$comment_permissions must cite the audit item that added the "
        "block so future audits can trace the decision"
    )
    assert "tests/test_permissions_deny_present.py" in comment, (
        "$comment_permissions must name the sentinel — sentinels self-"
        "document via the file they pin"
    )


# ---------------------------------------------------------------------------
# 2. Secret files in this project tree
# ---------------------------------------------------------------------------


def test_deny_blocks_dotenv_read_edit_write() -> None:
    """The .env files in the project root must be denied for Read,
    Edit, and Write — they are the only file class STE keeps secrets in
    (per CLAUDE.md universal invariants). The ``.env.*`` glob covers
    .env.bak, .env.local, .env.production, etc."""
    deny = _deny_list()
    for rule in (
        "Read(./.env)",
        "Read(./.env.*)",
        "Edit(./.env)",
        "Edit(./.env.*)",
        "Write(./.env)",
        "Write(./.env.*)",
    ):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r} — "
            "STE's universal invariant is that secrets ONLY live in "
            ".env (gitignored). Reading/writing it from a Claude "
            "session would leak the secret into the conversation "
            "transcript"
        )


def test_deny_blocks_secrets_directory() -> None:
    """``./secrets/**`` is a defensive deny — STE doesn't currently
    have a secrets/ dir, but if one ever appears (operator-driven, not
    Claude-driven), no Claude session can read or write it. Defense in
    depth against accidental secrets/ creation."""
    deny = _deny_list()
    for rule in (
        "Read(./secrets/**)",
        "Edit(./secrets/**)",
        "Write(./secrets/**)",
    ):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r}"
        )


# ---------------------------------------------------------------------------
# 3. Credential directories in $HOME
# ---------------------------------------------------------------------------


def test_deny_blocks_home_credential_directories() -> None:
    """Claude has no legitimate need to READ files under ~/.ssh,
    ~/.aws, ~/.gnupg, ~/.netrc, or ~/.config/gh. The CLIs that need
    them (ssh, aws, gpg, git, gh) read their own configs — Claude
    running them as subprocesses works regardless of these denies."""
    deny = _deny_list()
    for rule in (
        "Read(~/.ssh/**)",
        "Read(~/.aws/**)",
        "Read(~/.gnupg/**)",
        "Read(~/.netrc)",
        "Read(~/.config/gh/**)",
    ):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r}"
        )


# ---------------------------------------------------------------------------
# 4. Network calls Claude shouldn't make directly
# ---------------------------------------------------------------------------


def test_deny_blocks_curl_and_wget() -> None:
    """Verified 2026-06-04: zero curl/wget usage in tpcore/ops/scripts.
    STE uses Python httpx/requests for HTTP. curl/wget from a Claude
    Bash call has no legitimate purpose AND is a common prompt-
    injection exfil vector."""
    deny = _deny_list()
    for rule in ("Bash(curl *)", "Bash(wget *)"):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r}"
        )


# ---------------------------------------------------------------------------
# 5. Destructive ops that should NEVER happen from a Claude session
# ---------------------------------------------------------------------------


def test_deny_blocks_root_and_home_recursive_delete() -> None:
    """rm -rf / and rm -rf ~* should never happen from a Claude
    session. Per Anthropic's docs, bypassPermissions mode still prompts
    on these as a 'circuit breaker' — but the explicit deny is the
    canonical layer."""
    deny = _deny_list()
    for rule in (
        "Bash(rm -rf /)",
        "Bash(rm -rf /*)",
        "Bash(rm -rf ~)",
        "Bash(rm -rf ~/*)",
        "Bash(rm -rf $HOME*)",
    ):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r}"
        )


def test_deny_blocks_disk_and_permission_ops() -> None:
    """``dd if=``, ``chmod -R 777``, and ``chown -R`` are the
    canonical "mass disk/permission mutation" patterns that should
    never come out of a Claude session — they hide tampering and
    destroy reproducibility."""
    deny = _deny_list()
    for rule in (
        "Bash(dd if=*)",
        "Bash(chmod -R 777 *)",
        "Bash(chown -R *)",
    ):
        assert rule in deny, (
            f"settings.json permissions.deny must include {rule!r}"
        )


# ---------------------------------------------------------------------------
# 6. Allow list is NOT polluted — we use deny-only as the binding layer
# ---------------------------------------------------------------------------


def test_permissions_allow_is_not_used_as_a_workaround() -> None:
    """STE doesn't use ``permissions.allow`` to broaden the surface —
    hooks + the per-tool defaults are sufficient. If a future change
    adds an ``allow`` entry that contradicts a deny entry, deny still
    wins (per Anthropic's docs: 'deny → ask → allow, first match
    wins; deny from any scope blocks'). This test asserts the current
    state — operator must explicitly approve any ``allow`` entry."""
    data = _settings()
    perms = data.get("permissions", {})
    allow = perms.get("allow", [])
    assert isinstance(allow, list), (
        "permissions.allow must be a list if present"
    )
    assert not allow, (
        "settings.json permissions.allow is currently empty by design; "
        "if you need to add an allow entry, do it in a deliberate PR "
        "that also documents why the existing hooks + defaults are "
        f"insufficient. Found: {allow!r}"
    )
