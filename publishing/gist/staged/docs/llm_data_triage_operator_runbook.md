# LLM Triage Agent — Operator Runbook (config-not-code, both lanes)

**Scope:** the GitHub/repo settings and runtime-env steps the operator
must apply to harden the LLM triage agents — the **data-lane** agent
(Ladder rung 5, #187, BUILT 2026-05-18) and the **engine-lane** agent
(engine Ladder R5, Epic E, BUILT 2026-05-18; the engine-native
symmetric mirror). Both share this one runbook (spec §9). These steps
are deliberately **not enforceable purely in code** — they live in
GitHub repo settings and the daemon's runtime env, not the tree.

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

Cross-links: data-lane spec
`docs/superpowers/specs/2026-05-18-llm-triage-advisory-layer-design.md`
· engine-lane spec
`docs/superpowers/specs/2026-05-18-engine-llm-triage-advisory-layer-design.md`
· personas `docs/llm_data_triage_persona.md` /
`docs/engine_llm_triage_persona.md` · Ladders
`docs/ESCALATION_HARDENING_LADDER.md` (data rung 5) /
`docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (engine R5).

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

## (vii) Engine-lane parity — same settings, the engine label + fence

The engine-lane agent (`ops/engine_llm_triage.py`, engine Ladder R5,
Epic E) is the symmetric mirror of #187 and is hardened by the **same**
config-not-code steps above, applied identically — there is no second
runbook. Honestly flagged config-not-code, same framing as the
data section: the code-side fence is independently sufficient; these
are defense-in-depth.

- **Label:** create the repo label **`engine-llm-triage`** (Settings →
  Labels), a separate label from `llm-data-triage`. The engine fence
  CI job is **label-gated** on `engine-llm-triage` exactly as the data
  job is on `llm-data-triage`; if the label does not exist the agent's
  `gh pr create --label engine-llm-triage` fails (fail-safe — proposal
  still emitted, fenced PR lost). Create it once.
- **CI fence:** the `engine-llm-triage-fence` job in
  `.github/workflows/ci.yml` is the engine analogue of
  `llm-triage-fence` — same deterministic provenance + hard-denied
  protected-path machinery (it reuses the one shipped pure
  `tpcore/llm_data_triage/fence` object verbatim, no twin), and is
  **equally credential-starved**: it never references
  `ANTHROPIC_API_KEY` (the (v) audit/removal already covers this — one
  key, daemon-runtime-only, never in CI for either lane). Add
  `engine-llm-triage-fence` alongside `llm-triage-fence` + `test` as a
  required status check in branch protection (i); do not allow admin
  bypass.
- **No new daemon, no new install step:** the engine triage runs as a
  **second crash-isolated `_run_supervised` co-task inside the existing
  `ops/llm_triage_service.py`** (Epic E B1) — NOT in the live-trading
  `engine_service`, NOT a 5th daemon. The installer name, launchd
  label, and the closed 4-token whitelist are unchanged; nothing new
  to install — re-running `scripts/install_all_daemons.sh` after
  pulling Epic E is sufficient (the same daemon now polls
  `ENGINE_ESCALATED` too).
- **CODEOWNERS / branch protection / no-merge bot identity** ((ii),
  (iii)) apply to `engine-llm-triage` PRs identically: same dedicated
  no-merge bot identity, same 2-human / code-owner review (one ≠ the
  dispositioning operator), CODEOWNERS extended to cover the
  `ops/engine_ladder.py` `DISPOSITION_POLICIES` binding files.

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
- [ ] The `engine-llm-triage` label exists (separate from
      `llm-data-triage`); branch protection also requires
      `engine-llm-triage-fence`; that fence is credential-starved.
- [ ] No new daemon/install step for the engine lane — the existing
      `llm_triage_service` now also polls `ENGINE_ESCALATED` (two
      crash-isolated co-tasks); 4-token whitelist unchanged.
