# LLM Data-Triage Agent — Operator Runbook (config-not-code)

**Scope:** the GitHub/repo settings and runtime-env steps the operator
must apply to harden the LLM data-triage agent (Ladder rung 5, #187,
BUILT 2026-05-18). These are deliberately **not enforceable purely in
code** — they live in GitHub repo settings and the daemon's runtime
env, not the tree.

**Honest framing — the code-side fence is independently sufficient.**
The deterministic CI fence (the label-gated `llm-triage-fence` job:
provenance + hard-denied protected paths) + the credential-starved
sandbox + the no-merge-call agent + the full existing PR / clockwork-
drift gate + the post-merge canary already make a system-breaking
*merge* impossible without a human. The settings below are
defense-in-depth and an explicit audit trail — they raise the floor,
they are **not** the load-bearing safety boundary (that is the spec
§3/§4 fence). Apply them anyway: redundant human-gating on an LLM-
authored PR is cheap insurance.

Cross-links: spec
`docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md`
· persona `docs/llm_data_triage_persona.md` · Ladder rung 5
`docs/ESCALATION_HARDENING_LADDER.md`.

## (i) Branch protection — required checks on LLM PRs

On the default branch's protection rule (Settings → Branches), require
status checks to pass before merging and add both:

- `llm-triage-fence` — the label-gated provenance + hard-denied
  protected-path job in `.github/workflows/ci.yml`.
- `test` — the existing full pytest job (clockwork-drift / registry
  lockstep run with the new spec active in fixture).

A draft PR cannot be merged while draft; un-drafting still requires
both checks green. Do NOT add `llm-triage-fence` as an exception or
allow administrators to bypass it.

## (ii) CODEOWNERS — two human approvals, one ≠ dispositioning operator

Add a `CODEOWNERS` entry covering the HealSpec / RemediationSpec /
Ladder-binding files and require **2 approving reviews** in branch
protection, with "Require review from Code Owners" enabled. Operationally:
one of the two approvers MUST be a human other than the operator who
dispositioned the originating escalation — operator-sole-review is a
single point of failure (spec §3). Do not configure a team that
resolves to one person.

## (iii) Dedicated bot identity with NO merge permission

The `gh pr create --draft` call inside the agent runs as a dedicated
GitHub identity/token (a fine-grained PAT or GitHub App installation
token) scoped to **create branches and open draft PRs only — no merge,
no admin, no branch-protection bypass**. The human reviewers merge; the
bot never can. Store this token in the daemon runtime env (see (v)),
never in CI.

## (iv) The `llm-data-triage` label must exist

Create the repo label `llm-data-triage` (Settings → Labels). The agent
applies it on PR creation; the `llm-triage-fence` CI job is
**label-gated** on it (runs only for PRs carrying the label). If the
label does not exist the agent's `gh pr create --label llm-data-triage`
fails, the PR is not opened, and the proposal is still emitted — fail
safe, but the operator loses the fenced PR. Create it once.

## (v) `ANTHROPIC_API_KEY` — runtime-only, NEVER in CI

- The key lives **only** in the `llm_triage_service` daemon's runtime
  environment via the gitignored `.env`. Its **absence is a safe
  no-op**: the agent logs `llm_data_triage.no_api_key` and the cycle
  proceeds untouched (escalation stays undispositioned → human).
- **Explicit instruction: remove any `ANTHROPIC_API_KEY` GitHub Actions
  secret.** CI and the `llm-triage-fence` job are fully deterministic
  and MUST stay credential-starved — the fence never references
  `ANTHROPIC_API_KEY` and must never gain access to it. The key must
  live ONLY in the daemon runtime env, never in CI, never in any
  workflow secret. Audit `Settings → Secrets and variables → Actions`
  and delete it if present.

## (vi) The agent runs via the `llm_triage_service` launchd daemon

The agent is **event-driven**, not scheduled. `ops/llm_triage_service.py`
is a sibling daemon (structural mirror of `data_repair_service` /
`engine_service`) installed by `scripts/install_all_daemons.sh`; it
polls `platform.application_log` for `DATA_REPAIR_ESCALATED` /
`DATA_SOURCE_ESCALATED` and fires the triage agent. There is **no
cron, no scheduled GitHub workflow, and no `run_data_operations.sh`
step** — do not add one. Re-run `scripts/install_all_daemons.sh` after
pulling #187 to register the new daemon.

## What good looks like — verification checklist

- [ ] Branch protection on the default branch requires `llm-triage-fence`
      AND `test`; admins cannot bypass.
- [ ] `CODEOWNERS` covers the spec/binding files; 2 approvals + code-owner
      review required; the two approvers can be distinct humans, neither
      necessarily the dispositioning operator.
- [ ] The `gh` token used by the agent is a dedicated identity with no
      merge/admin scope; verified it cannot merge a PR.
- [ ] The `llm-data-triage` label exists in the repo.
- [ ] No `ANTHROPIC_API_KEY` secret in GitHub Actions; key present only
      in the daemon's gitignored `.env`; removing it yields a clean
      `llm_data_triage.no_api_key` no-op (no cycle failure).
- [ ] `scripts/install_all_daemons.sh` lists/installs `llm_triage_service`;
      no cron or scheduled workflow invokes the agent.
- [ ] A test escalation event on the bus produces a draft PR carrying
      the `llm-data-triage` label, with `llm-triage-fence` running and
      no auto-merge possible.
