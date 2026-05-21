---
name: research-llm-edge-discovery
description: "Backlog epic: an LLM that does research to find a tradable edge by driving 'The Lab' and graduating discovered edges into engines — Path B autonomous-loop posture (operator decision 2026-05-21)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Ownership (post 2026-05-19 single-session consolidation):** #242
is owned by the one remaining session per [[cross-session-coordination]].
(Historical: 2026-05-19 operator transferred from data-lane→engine-lane;
both lanes were consolidated to one session 2026-05-19.)
Distinct from — do NOT conflate with — the engine-LLM-**triage** Epic
E ([[engine-llm-triage-ownership]] / #187 sibling) which is a separate
mechanism (triage = advisory escalation; #242 = alpha-discovery
loop). **Status: Path B v1 spec at
`docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`
(supersedes Path A v1 in PR #213). Build does not begin until the
operator spec-read gate clears and a plan PR follows.**

Operator intent (2026-05-18 → 2026-05-21 path B reversal):
the engine lane built an **Engine SDLC + "The Lab"** (SP1 shipped,
SP2 in progress on the engine side). On top of that, the operator
wanted an **LLM that does *research* to find a tradable edge**:
it drives the Lab, discovers new edge cases / candidate strategies,
and the good ones **graduate into a real engine** via the engine
SDLC/graduation path.

**Why it matters / how to apply:** this is an alpha-discovery loop,
not a triage/ops agent — categorically different from #187 / Epic E
(those are advisory escalation triage). When greenlit it gets its own
brainstorm→spec→plan→build.

**⚠ OPERATOR AMBITION RAISED 2026-05-20 (originally surfaced at SP-G
design point):** operator explicitly wants more than a "thin advisory
spec-emitter" — an LLM that finds edges **on its own**, driving a
real **quantitative toolkit: statistics, math, predictive analytics**
(statsmodels / scipy.stats / etc.).

**⚑ PATH B REVERSAL — operator decision 2026-05-21 (the binding
posture; SUPERSEDES prior HARD CONSTRAINT clause (a) below).**
Operator chose Path B (true end-to-end autonomy) over Path A
(human-gated). Verbatim: *"the finder finds AND automates AND
monitors AND retires. I become the auditor of OUTCOMES, not the
gate-keeper of EACH STEP."*

The Path B v1 spec implements:
- **Auto-undraft + auto-merge + auto-ECR (ADD / MODIFY / RETIRE).**
  Operator no longer signs each PR or opens each ECR; the finder
  routes all roster mutations through `python -m ops.engine_sdlc
  --ecr <file>` machine path.
- **Auto-retire on outcome violation.** Phase E live-paper outcome
  monitor co-task computes `LiveOutcome` per session-close;
  `auto_retire_triggered=True` the moment the bleed cap is hit
  ($5k cumulative; 20% of the $25k per-engine PAPER slot) OR a
  rolling-Sharpe / drawdown / trade-count clause is violated.
- **Outcome-proven marker on success.** `EngineProfile.outcome_proven
  = True` when a finder-emitted PAPER engine satisfies every clause
  of the Tier 2 criterion over a 30-session window. The engine STAYS
  PAPER per the paper-only mandate; operator decides LIVE separately.
- **Paper-only mandate is the ONLY remaining operator gate.** PAPER
  → LIVE is operator-only. Every other gate the operator previously
  held flips to deterministic mechanism with the LLM as the trigger
  and the operator as the auditor.

**Path B is safe at autonomous scale because of THREE new structural
fences (constraint additions 14/15/16 in the spec):**
1. **Regime-aware n_trials ledger (constraint 14).** Same hypothesis
   re-fired in a different market regime is a fresh trial against the
   regime axis. `tpcore.lab.ledger.cumulative_n_trials_by_regime`
   reads cumulatively WITHIN regime; the DSR gate is monotone-harder
   PER REGIME — defense against autonomous DSR-laundering by "novel
   regime" hypothesis-relabeling.
2. **Bleed-budget per finder-emitted PAPER engine (constraint 15).**
   $5,000 structural max-bleed over the 30-session outcome window;
   auto-retire ECR fires the moment the cap is reached, NOT end-of-
   window. Defense against autonomous capital destruction.
3. **Provenance is non-negotiable (constraint 16).** Every autonomous
   action writes one append-only `LAB_FINDER_ACTION` row to
   `application_log` with `action`, `triggered_by`, `human_override
   = 'none'`. The §12 dashboard
   (`dashboard_components/finder_audit.py`) is the operator's audit
   lane.

**Operator framing (F1)+(F2) sharpened 2026-05-20 (carries into Path
B v1 verbatim):** the curated reference set
([[ref-carver-systematic-trading]], [[ref-chan-algorithmic-trading]],
+ new bundles `dsr_ntrials_discipline.md`,
`market_structure_primer.md`, `regime_aware_trading.md`) is explicitly
chosen to teach TWO things — (1) the **trading environment**: market
structure / micro-structure and how everything interconnects; (2) a
**repeatable workflow**: collect data → analyse → find trade ideas
to automate. Path B v1 internalises (1) as domain context (the
`MarketSnapshot` now carries macro/sentiment/calendar/regime per
expert-review BLOCKING #1) and operates (2) as its loop (Phases A–F).

**"Venturing out" semantics (operator decision 2026-05-21, expert
review §3.7 path (a)):** "venture out" means RICHER OPERATOR-STAGED
CONTEXT (broader `MarketSnapshot` substrates + broader
`docs/lab_emitter_references/*.md` bundles) PLUS the LLM's trained
knowledge as a deliberate, NOT-mining supplement — NEVER runtime
browsing, NEVER unguided trained-knowledge spec generation. The
persona §7 makes this explicit; trained-knowledge alone cannot
ground a `ProposedSpec.rationale`.

**HARD CONSTRAINT — carry this into any future design (do not build
naively):** [[project_ml_research_track]] — the commissioned expert
verdict is that naive automated strategy/edge search **attacks the
wrong constraint**: generating many candidate strategies inflates
the **DSR `n_trials` / multiple-testing** count and manufactures
overfit "edges" that die out-of-sample.
- (a) **(REVERSED 2026-05-21 — Path B replaces this with autonomous-
  loop posture.)** ~~Advisory + human-gated only.~~ Path B routes
  every candidate through the existing graduation / DSR / credibility
  gate (unchanged), but the finder auto-undrafts, auto-merges, auto-
  issues ECR (ADD / MODIFY / RETIRE) via the machine path, and auto-
  retires on outcome violation. The reversal is safe because of the
  THREE new fences above (regime-aware ledger + bleed budget +
  provenance audit), NOT because operator-gates each step.
- (b) Honestly account for the trials it consumes (the LLM's search
  IS part of `n_trials`; SP-A `record_trial_spend_with_regime` fires
  unconditionally at emission time, BEFORE the draft PR).
- (c) Prefer the expert-blessed framings (meta-labeling / cross-engine
  combiner; both still v2.0/v2.5 deferred in the Path B roadmap)
  over free-form strategy mining.
- (d) Keep forensics/allocator/governor/the graduation gate
  deterministic ([[feedback_symmetry_not_copy]] discipline).

The Lab + Engine SDLC are the engine lane's deliverables
([[project_engine_sdlc_lifecycle]],
[[project_deterministic_agents_epic]]); the Path B research-LLM sits
atop them. Brainstorm-first → expert-harden → spec → plan PR → build.
