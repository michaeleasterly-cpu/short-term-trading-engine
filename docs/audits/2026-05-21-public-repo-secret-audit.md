# Public-repo secret audit — baseline (2026-05-21)

**Status:** clean — 0 CRITICAL findings, 0 REVIEW findings, 3 CONFIRMED-CLEAN false positives.
**Scope:** every blob across every commit on every branch of the local canonical checkout `/Users/michael/short-term-trading-engine/`.
**Trigger:** repo flipped public on 2026-05-21 (GitHub Actions quota for private repos exhausted; public quota is unbounded). Any committed secret is now visible to the world — operator directive: "make sure that none of my api keys are in the repo... its public now".
**Tool:** [gitleaks](https://github.com/gitleaks/gitleaks) `8.30.1` (homebrew install on the operator's Mac; CI pins the same version in `.github/workflows/secret-scan.yml`).

## 1. Method

```
gitleaks detect \
    --source . \
    --report-format json \
    --report-path /tmp/gitleaks-report.json \
    --no-banner
```

Two passes:

1. **Full git history** (`--source .`, no `--no-git`) — 817 commits, ~23.40 MB of blobs, 1.77 s wall.
2. **Working tree** (`--source . --no-git`) — what the CI gate sees on every push / PR.

The default gitleaks 8.x ruleset covers ~150 detectors including:

- Anthropic API keys (`anthropic-api-key`, `sk-ant-*`)
- OpenAI API keys (`openai-api-key`, `sk-*`)
- AWS access keys (`aws-access-token`, `AKIA*` / `ASIA*`)
- SSH private keys (`private-key`)
- Postgres / generic database URIs with credentials
- GitHub / GitLab personal-access tokens
- Slack, Discord, Stripe, Twilio, ... — see [`gitleaks.toml` upstream](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml)
- `generic-api-key` — catch-all entropy + length heuristic

`trufflehog filesystem .` was considered for live-key validation against vendor endpoints but rejected: validating an operator's key by hitting the vendor's API would leak the key to the validator. Not worth the marginal coverage.

## 2. Headline results

| Surface | Hits | Status |
|---|---|---|
| Full git history (817 commits) | 3 | All CONFIRMED-CLEAN test fixtures — see §3. |
| Worktree (CI gate surface) | 3 | Same three fixtures. The 11 `.env` hits seen in the operator's local working tree are gitignored, never tracked, not visible to CI. |
| Worktree with `.gitleaks.toml` allowlist | **0** | Gate green. |
| Full history with `.gitleaks.toml` + `.gitleaksignore` | **0** | Gate green. |

**Bottom line:** no real secret has ever been committed to this repo. The public flip is safe with respect to credential exposure.

## 3. Findings

### F1 — CONFIRMED-CLEAN — `mo_AAPL_close_2026-05-19`

- **Rule:** `generic-api-key`
- **File:** `tpcore/tests/test_order_ids.py:29`
- **Commit:** `7ec7867d5a4bad751d437008fbfc8a31911e553c` (PR #82, "feat(risk): #251 B1 — idempotent record_close")
- **Context:** assertion that `build_close_id("momentum", "AAPL", date(2026, 5, 19)) == "mo_AAPL_close_2026-05-19"` — a deterministic close-ledger key string derived from public engine prefix + public ticker + ISO date.
- **Classification:** CONFIRMED-CLEAN. Test fixture, no entropy, reconstructible from the module's documented format. Not a credential.
- **Action:** allowlisted in `.gitleaks.toml` and pinned by fingerprint in `.gitleaksignore`.

### F2 — CONFIRMED-CLEAN — `sn_TLT_close_2026-01-03`

- **Rule:** `generic-api-key`
- **File:** `tpcore/tests/test_order_ids.py:34`
- **Commit:** `7ec7867d5a4bad751d437008fbfc8a31911e553c` (same PR as F1)
- **Context:** assertion that `build_close_id("sentinel", "TLT", date(2026, 1, 3)) == "sn_TLT_close_2026-01-03"`.
- **Classification:** CONFIRMED-CLEAN. Same shape as F1 — sentinel engine prefix, public ticker, ISO date. Not a credential.
- **Action:** allowlisted in `.gitleaks.toml` and pinned by fingerprint in `.gitleaksignore`.

### F3 — CONFIRMED-CLEAN — `YUMC_1778582356`

- **Rule:** `generic-api-key`
- **File:** `tpcore/tests/test_order_ids.py:102` (current line is 156 — content unchanged; the line shift is from later edits in the same file)
- **Commit:** `f019a0b3b2ddd5a98c8bce1ae6b600bfddaaca3d` (PR introducing `tpcore.order_ids`)
- **Context:** regression fixture for the legacy `<TICKER>_<TS>` client-order-id format that pre-dated the per-engine prefix. The numeric suffix `1778582356` is a Unix-timestamp-shaped string used by `parse_cid` to round-trip a legacy CID; it is NOT a secret.
- **Classification:** CONFIRMED-CLEAN. Public ticker + decimal-looking suffix, no entropy. Not a credential.
- **Action:** allowlisted in `.gitleaks.toml` and pinned by fingerprint in `.gitleaksignore`.

## 4. Recurring gate

Three layers, all committed to the repo on 2026-05-21:

1. **CI workflow** — `.github/workflows/secret-scan.yml`
   - Triggers: every `push` to `main`, every PR to `main`, manual `workflow_dispatch`.
   - Runs `gitleaks detect --source . --no-git --config .gitleaks.toml --redact` against the worktree.
   - Fails the workflow on any un-allowlisted hit (gitleaks 8.x exits non-zero on leaks).
   - Uploads SARIF to GitHub code-scanning — findings appear in the **Security → Code scanning** tab.
   - Uploads the JSON report as a workflow artifact for 14 days.

2. **Pre-commit hook** — `.pre-commit-config.yaml`
   - Optional adoption: `pip install pre-commit && pre-commit install`.
   - Runs the same gitleaks `v8.30.1` on staged files before `git commit` completes.
   - Catches a leak BEFORE the push, so the public timeline never sees it (load-bearing for a public repo).

3. **Allowlist** — `.gitleaks.toml` + `.gitleaksignore`
   - `.gitleaks.toml`: pattern + path-scoped allowlist entries for F1, F2, F3 and the documented `postgresql://u:p@h/d` test DSN.
   - `.gitleaksignore`: fingerprint pins for F1, F2, F3 (covers the full-history scan mode).
   - Tests in `tests/test_secret_scan_gate.py` assert both files exist, parse, and stay coupled to the workflow.

## 5. Operator action items

None required at this time — no rotation, no history rewrite. If gitleaks ever reds the gate with a NEW finding, the playbook is:

1. **Triage** the report (artifact attached to the failed workflow run).
2. **If CRITICAL** (real key): rotate FIRST at the vendor, THEN purge from git history via `git filter-repo` (preferred) or BFG. Force-push only after rotation completes — the leaked credential is the operator's threat surface, the git history is the secondary concern.
3. **If REVIEW** (entropy false-positive): add a narrow `[[allowlists]]` block to `.gitleaks.toml` (path + regex scoped to the specific file) and a fingerprint line to `.gitleaksignore`. Document under §3 here with the next available F-id.
4. **If CONFIRMED-CLEAN already**: shouldn't reach the gate — file a bug against the allowlist.

## 6. Audit reproduction

```sh
# From the canonical checkout (NOT a worktree):
brew install gitleaks   # or pin the binary download — see secret-scan.yml
cd /Users/michael/short-term-trading-engine
gitleaks detect --source . --config .gitleaks.toml --no-banner
# Expected output: ``no leaks found``
gitleaks detect --source . --no-git --config .gitleaks.toml --no-banner
# Expected output: ``no leaks found``
```

A red on either command means a new committed secret. Walk §5.
