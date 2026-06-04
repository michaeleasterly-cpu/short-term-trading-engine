---
name: research-llm-edge-discovery
description: "Backlog epic: an LLM that does research to find a tradable edge by driving 'The Lab' and graduating discovered edge cases into engines — bounded by the DSR/overfit expert verdict"
metadata: 
  node_type: memory
  type: project
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Ownership (post 2026-05-19 single-session consolidation):** #242
is owned by the one remaining session per [[cross-session-coordination]].
(Historical: 2026-05-19 operator transferred from data-lane→engine-lane;
both lanes were consolidated to one session 2026-05-19.)

**2026-05-22 BOUNDARY CHECK — the LLM-TRIAGE stack was REMOVED
ENTIRELY (operator directive "we aren't going to use the llm triage...
take it out") — `ops.llm_data_triage`, `ops.engine_llm_triage`,
`ops.llm_data_recovery`, `tpcore.llm_data_triage`,
`tpcore.engine_llm_triage` are DELETED. This epic (#242, the LLM
edge-finder / Task #25) IS A DIFFERENT MECHANISM — alpha-discovery
loop, not triage — and survives the directive. The Task #25 modules
(`ops.llm_edge_finder*`, `tpcore.lab.llm_finder/`, the
`/lab-edge-find` slash skill) are EXPLICITLY KEPT. So is SP-G's Lab
spec-emitter (`ops.llm_lab_emitter`, `tpcore.lab.llm_emitter/`,
`/lab-spec-emit`). The "do NOT conflate with engine-LLM-triage"
language below is now moot because that triage stack no longer
exists; the conflation hazard has been eliminated structurally.** #242 is HARD-GATED on the **"Lab front-half" epic**
([[lab-front-half-epic]]) — the LLM edge-finder is a thin, advisory,
human-gated spec-emitter that feeds the front-half pipeline (roster-
driven plug-and-play Lab + the cross-candidate **n_trials ledger** [the
safety mechanism] + the Lab Candidate Readiness checklist + pluggable
per-engine scoring); it never bypasses the graduation gate, never
auto-applies to live capital. Build the front-half FIRST (the n_trials
ledger SP-A is the safety floor — SHIPPED PR #93). #242 / SP-G is a
thin later phase on top. **Status: backlog, future epic — NOT now** (the
Lab front-half epic precedes it). Operator intent (2026-05-18):
the engine lane is building an **Engine SDLC + "The Lab"** (SP1
shipped, SP2 in progress on the engine side). On top of that, the
operator wants an **LLM that does *research* to find a tradable edge**:
it drives the Lab, discovers new edge cases / candidate strategies,
and the good ones **graduate into a real engine** via the existing
engine SDLC/graduation path.

**Why it matters / how to apply:** this is an alpha-discovery loop,
not a triage/ops agent — categorically different from #187 / Epic E
(those are advisory escalation triage). When greenlit it gets its own
brainstorm→spec→plan→build.

**⚠ OPERATOR AMBITION RAISED 2026-05-20 (carry into the SP-G design
point — do NOT silently build only the thin emitter):** operator
explicitly wants more than a "thin advisory spec-emitter" — an LLM
that finds edges **on its own**, driving a real **quantitative
toolkit: statistics, math, predictive analytics** (the "R for finance"
methods, in Python — e.g. statsmodels / arch / linearmodels /
scikit-learn / scipy.stats, factor / time-series / regime models).
When asked to redefine SP-G scope + sequencing the operator answered
**"keep going / stick to the plan"** — i.e. NO restructure now, SP-G
stays planned scope + built LAST in order (SP-C→D→E→F→G). BUT this
richer autonomous-quant ambition is ON RECORD and MUST be re-surfaced
to the operator at the **SP-G design/brainstorm point** (offer the
full autonomous LLM+quant finder vs the thin emitter as an explicit
decision then). The HARD CONSTRAINT below still binds the bigger
ambition: autonomous many-hypothesis search is safe ONLY because every
probe is counted against the SP-A cumulative n_trials ledger and must
clear the sacred DSR≥0.95 ∧ cred≥60 gate — that fence is the
non-negotiable live-money invariant that makes an autonomous finder
not a DSR-laundering machine.

**Operator sharpened the roadmap 2026-05-20:** the curated reference
set ([[ref-carver-systematic-trading]], [[ref-chan-algorithmic-trading]],
future adds) is explicitly chosen to teach TWO things — (1) the
**trading environment**: market structure / micro-structure and how
everything interconnects; (2) a **repeatable workflow**: collect data
→ analyse → find trade ideas to automate. Operator: *"this is what
the LLM edge finder will do ... future roadmap."* So SP-G's autonomous
finder is intended to internalise (1) as domain context and operate
(2) as its loop — not free-form strategy mining but a disciplined
environment-aware data→analysis→idea→Lab pipeline. Carry this framing
into the SP-G brainstorm; still gated by the sacred gate + cumulative
n_trials ledger (the bigger ambition does not relax the fence).

**HARD CONSTRAINT — carry this into any future design (do not build
naively):** [[project_ml_research_track]] — the commissioned expert
verdict is that naive automated strategy/edge search **attacks the
wrong constraint**: generating many candidate strategies inflates the
**DSR `n_trials` / multiple-testing** count and manufactures
overfit "edges" that die out-of-sample. A research-LLM that proposes
N strategies is exactly that failure mode at scale. Therefore the
design MUST: (a) route every candidate through the **existing
graduation / DSR / credibility gate** — the LLM proposes, the
deterministic gate disposes; it never bypasses or re-weights the
gate; (b) honestly account for the trials it consumes (the LLM's
search IS part of `n_trials`); (c) prefer the expert-blessed framings
(meta-labeling / cross-engine combiner) over free-form strategy
mining; (d) keep forensics/allocator/governor/the graduation gate
deterministic ([[feedback_symmetry_not_copy]] discipline applies if
mirrored from the data lane). The Lab + Engine SDLC are the engine
lane's deliverables ([[project_engine_sdlc_lifecycle]],
[[project_deterministic_agents_epic]]); this research-LLM sits atop
them. Brainstorm-first; expert-harden the overfit story before any
code.
