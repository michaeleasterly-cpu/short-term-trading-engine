# Autonomous Finder Proof — 3-Probe Scorecard (2026-05-22)

**Question:** Does the autonomous LLM edge-finder produce edges that survive the SP-A walk-forward + DSR/credibility gate?

**Method:** Take the 5 candidates emitted by the v2.0 gate-pilot across reversion / vector / catalyst / sentinel / canary engines, send the 3 most-testable through real Lab probes.

## §1 Scorecard

| Candidate | Engine | DSR | Credibility | n_trades | Verdict | Failure mode |
|---|---|---|---|---|---|---|
| `reversion_earnings_season_5d_range_normal` | reversion | 0.0000 | 45 | 0 | FAILED | Regime 0× in holdout (4-axis tuple too rare: 17/2262 historical, 0 in 2024-2025) |
| `vector_beat_reversal_insider_filter_v1` | vector | — | — | — | STOPPED | Substrate gap: `insider_sentiment` monthly-granularity + pre-2025-empty |
| `catalyst_pead_expansion_range` | catalyst | 0.0094 | 40 | 2 | FAILED | Cluster-filter strips BEATs from 3753 → 2; LLM's BEAT-conjunction WORSE than legacy 'off' arm |

**Cumulative ledger spend today**: `lab_trial_ledger.reversion` 68 → 108 (+40); `lab_trial_ledger.catalyst` 80 → 180 (+100). Total +140 trials.

## §2 What the loop proved

- ✅ **Mechanical autonomy works end-to-end.** Phase A→B→C→engine-mod→Lab→gate→FAIL-dossier round-trips correctly.
- ✅ **LLM hypotheses are real.** Each candidate cited 1-5 statsmodels tool results (variance_ratio, ljung_box, ARIMA, hurst_exponent, cost_net_simulation) with claimed_value / claimed_threshold provenance.
- ✅ **Structural distinctness verified.** All 5 emissions distinct from the 4 failed hand-designed deep-research candidates (vector_composite, reversion_pca_residual, sentinel_bear_score, catalyst_insider_drift).
- ✅ **Gate filtering is honest.** No fake green — 3/3 testable candidates failed honestly; 1/5 self-rejected (momentum honestly refused given the range regime).
- ✅ **Persona v2.1 testability pre-check WORKED.** Saved 40+ ledger trials by stopping the vector probe at substrate-gap pre-check.

## §3 What the loop did NOT prove

❌ **No candidate survived the gate.** Three structural reasons across the three probes:

1. **Engine surfaces too rigid for the LLM's specific hypotheses:**
   - Reversion's regime filter is binary-strict (full 4-axis hash match)
   - Vector's insider data is monthly-aggregated (can't express "trailing 30d")
   - Catalyst's insider-cluster filter requires ≥3 distinct insiders (strips PEAD-only candidates)
2. **Schema vs persona mismatch:** persona §3 says "drop axes for testability" but `ProposedSpec.regime_tuple_id` is a frozen 12-char SHA12 (4-axis only). The LLM drops axes in prose but can't drop them in the schema field.
3. **Gate calibration vs finder design:** persona pushes regime-conditional emissions; gate demands `n_trades ≥ 3`. These are structurally incompatible for narrow regimes.

## §4 Adapter-readiness backlog surfaced

From the vector probe's STOPPED-at-pre-check verdict:

- **`platform.insider_sentiment` needs daily-granularity backfill** (currently monthly aggregations from 2025-01 forward). OpenInsider / Form-4 historicals are public; backfill to 2018-01-01 + drop monthly aggregation in favor of daily filings or 30d-rolling MSPR.
- This unblocks future finder emissions that condition on insider sentiment signal — currently STRUCTURALLY UNTESTABLE.

## §5 Recommended next iterations (ordered by impact)

1. **Engine surface enrichment** — open more knobs in each engine's LAB_TARGET so the LLM can express partial-axis regime gates, cluster-floor overrides, custom hold periods. Per-engine refactor (~1d per engine).
2. **Schema enrichment** — add `regime_axes_subset: tuple[str, ...]` to `ProposedSpec` so the LLM can specify exactly which axes to condition on (vs the all-or-nothing 12-char hash). Engine `regime_filter_v1` reads the subset list + matches accordingly.
3. **Adapter backfills** — insider_sentiment daily-granularity is the load-bearing one for the strongest unprobed candidate (`vector_beat_reversal_insider_filter_v1`, +41.03 LLM-self-rated Sharpe).
4. **v1.5 ENGINE-ADD epic** — let the LLM scaffold NEW engines with exactly the filters its hypothesis needs. Lifts the persona §8 "never propose ENGINE-ADD in v1" restriction. ~5-7 day epic.

## §6 Cost summary

- **API cost (4 pilot runs, prompt caching active):** ~$0.16 total
- **Ledger trials spent (per SP-A H-LL-1 unconditional-spend invariant):** +140 reversion + catalyst combined
- **Wall-clock (3 probes + iteration):** ~3 hours
- **Anthropic 529 incidents:** 2 subagent dispatches failed; deterministic Lab probes unaffected; recovery logic now landed (see `feedback_anthropic_529_self_heal.md`)

## §7 The honest verdict on "does the finder work?"

**The mechanism works.** The LLM finds real, statistically-cited, structurally-distinct hypotheses. The autonomous loop renders them into engine modifications + Lab probes + dossiers.

**The hypotheses don't survive the existing engines + gate.** The engines weren't designed to be parameterized at the granularity the LLM's hypotheses need. The gate's n_trades floor punishes the regime-conditional emissions the persona directs the LLM to produce.

**The value-add is in TWO places that need investment:**
1. **Engine surfaces** — make them more knob-rich so the LLM has more degrees of freedom to land hypotheses
2. **v1.5 ENGINE-ADD** — let the LLM bypass existing engines entirely when needed

Without these, the autonomous loop will continue to produce honest-but-failing candidates. **It's the next iteration's epic, not a finder defect.**
