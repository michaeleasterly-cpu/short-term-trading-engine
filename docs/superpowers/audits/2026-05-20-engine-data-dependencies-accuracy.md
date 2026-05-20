# Audit: EngineProfile.data_dependencies accuracy (2026-05-20)

## Scope

Re-verify the per-engine `EngineProfile.data_dependencies: frozenset[str]`
values that PR #171 backfilled from the 2026-05-16 hand-curated
`capital_gate.ENGINE_TABLES`. The engines have grown since (catalyst →
PAPER, carver → LAB, sentinel/momentum evolved); this audit confirms
whether each declared set still matches the engine's actual
`platform.<table>` reads, in the same vocabulary the per-engine capital
gate consumes (`tpcore.quality.validation.capital_gate._required_sources`
→ `HEAL_SPECS[*].source`).

Method per engine (mirrors the methodology preserved in PR #171's
`ENGINE_TABLES` diff comments):

1. Read declared deps live from `tpcore.engine_profile._PROFILE[<engine>].data_dependencies`.
2. Grep `<engine>/plugs/*.py`, `<engine>/scheduler.py`, `<engine>/backtest.py`,
   `<engine>/order_manager.py` for `platform.<table>` references. Backtest
   reads count (per PR #171 audit convention — e.g. `vector earnings_events`
   was cited from `backtest.py:269,847,1028`).
3. Exclude internal/meta tables `data_quality_log`, `application_log`,
   `open_orders`, `risk_state`, `aar_events`, `universe_candidates`,
   `allocations`. These are platform STATE (engine outputs / control-plane
   logs / computed-state caches), not validation-gated external feeds —
   the original PR #171 docstring explicitly enumerates them as
   intentional exclusions.

## Verdict matrix

| Engine | Declared | Actual reads | Verdict |
|---|---|---|---|
| reversion | `prices_daily`, `fundamentals_quarterly` | `prices_daily` (`backtest.py:18`, via `PostgresDataAdapter` in `scheduler.py:112`), `fundamentals_quarterly` (`backtest.py:289, 1008, 1243`) | ACCURATE |
| vector | `prices_daily`, `fundamentals_quarterly`, `earnings_events` | `prices_daily` (`scheduler.py:89, 194`; `backtest.py:216`), `fundamentals_quarterly` (`scheduler.py:132`; `backtest.py:280, 1132, 1372`), `earnings_events` (`backtest.py:302, 1136, 1376`) | ACCURATE |
| momentum | `prices_daily`, `liquidity_tiers` | `prices_daily` (`backtest.py:209`; `plugs/setup_detection.py:186`), `liquidity_tiers` (`backtest.py:199`; `plugs/setup_detection.py:165, 172`), **`earnings_events` (`backtest.py:485`)** — `_load_earnings_beats` is called unconditionally by `load_momentum_window_context` (line 529) and stored on `MomentumWindowContext.earnings_by_ticker` for the vol-managed Lab candidate's earnings-beat overlay (PR #180, post-#171). Legacy 12-1 path ignores the field but the SQL still runs. Plug also reads `universe_candidates` (`plugs/setup_detection.py:151`) — intentional exclusion per PR #171 (platform STATE: computed cache populated by `tpcore.universe.prescreener`). | **MISSING_DEPENDENCY** — `earnings_events` |
| sentinel | `prices_daily`, `macro_indicators` | `prices_daily` (`scheduler.py:369`; `backtest.py:92`; `plugs/setup_detection.py:297`), `macro_indicators` (`backtest.py:4`; `plugs/setup_detection.py:260`) | ACCURATE |
| canary | `prices_daily` | `prices_daily` (`scheduler.py:88`) | ACCURATE |
| catalyst | `prices_daily`, `sec_insider_transactions` | `prices_daily` (`scheduler.py:115`; `backtest.py:250`), `sec_insider_transactions` (`scheduler.py:86`; `backtest.py:226`), **`earnings_events` (`backtest.py:292`)** — strictly-additive read in `_fetch_earnings_events` consumed by `event_confirmation_mode="positive_beat_30d"` variant (spec §2.2). Pre-loaded into `CatalystWindowContext.earnings_events` (`backtest.py:539, 581`) unconditionally on every window load. | **MISSING_DEPENDENCY** — `earnings_events` |
| allocator | `prices_daily` | `prices_daily` (`tpcore/allocator/service.py:326`). `aar_events`/`risk_state`/`allocations` are engine-output / control-state tables (excluded per PR #171 convention; matches the existing inline comment "AAR/risk_state are engine *output* tables, not validation-gated"). | ACCURATE |
| carver | `frozenset()` (LAB default) | `prices_daily` (`backtest.py:193`), `liquidity_tiers` (`backtest.py:182`) | ACCURATE (LAB) — see graduation watch below |

LAB sentinel `lab` and RETIRED `sigma` are unprofiled-by-design with
empty `data_dependencies`; the `_DISPATCHABLE` filter (`engine_profile.py`
`_DISPATCHABLE = {PAPER, LIVE}`) keeps them out of every dispatch path,
so the drift sentinel
(`test_dispatchable_engine_declares_data_dependencies`) is satisfied
without a declaration. Confirmed in scope.

## Evidence — full grep transcript

Command per engine:

```
grep -rEn "platform\.[a-z_]+" <engine>/plugs/ <engine>/scheduler.py \
    <engine>/backtest.py <engine>/order_manager.py 2>/dev/null
```

Filtered to non-comment SQL/code references (DDL/INSERT writes for
logging excluded; SELECT/FROM reads retained):

- reversion: `backtest.py:289, 1008, 1243` (`FROM platform.fundamentals_quarterly`); `prices_daily` via `PostgresDataAdapter` (scheduler hand-off at line 112). The `data_quality_log` mention at `backtest.py:1501` is a WRITE message ("persisted to platform.data_quality_log") — excluded per platform-state convention.
- vector: `scheduler.py:89` (`FROM platform.prices_daily`), `scheduler.py:132` (`FROM platform.fundamentals_quarterly`); `backtest.py:280, 302, 1132, 1136, 1372, 1376`. `application_log`/`data_quality_log` references are write-paths or docstrings.
- momentum: `plugs/setup_detection.py:151, 165, 172, 186` and `backtest.py:199, 209, 485`. `application_log` references (`scheduler.py:155, 236, 242, 322`) are EQUITY_SNAPSHOT / SIGNAL / ORDER_SUBMITTED logging-bus state — engine-output / control-plane, excluded.
- sentinel: `plugs/setup_detection.py:260, 297`; `scheduler.py:369`; `backtest.py:92`. `risk_state` mention in `plugs/capital_gate.py:22` is a docstring; `scheduler.py:150` calls `governor.state_for(...)` (the RiskGovernor abstraction), not a direct table read.
- canary: `scheduler.py:88` (the only platform read in the heartbeat engine).
- catalyst: `scheduler.py:86, 115`; `backtest.py:226, 250, 292`. The `backtest.py:292` read is the missing dependency — it's a live SQL `SELECT … FROM platform.earnings_events` pulled unconditionally by `load_catalyst_window_context` (line 581), pre-loaded onto `CatalystWindowContext.earnings_events` (line 539). The legacy mode ignores the returned DataFrame; the `event_confirmation_mode="positive_beat_30d"` variant gates trades on it.
- allocator: `tpcore/allocator/service.py:326` (the only validation-gated read; everything else is writes to engine-output tables).
- carver: `backtest.py:182, 193` — LAB scope; `application_log` reads/writes in `plugs/execution_risk.py:14` and `plugs/lifecycle_analysis.py:67, 73, 101` are the CARVER_FLIP control-state counter (platform STATE).

## Corrections (staged ECR artifacts)

Both corrections derive from PRs landed on 2026-05-20 AFTER PR #171
backfilled `data_dependencies` from the 2026-05-16 audit:

- catalyst `_fetch_earnings_events` added by PR #178 (15:46 UTC+8) —
  spec §2.2 `event_confirmation_mode="positive_beat_30d"` overlay.
- momentum `_load_earnings_beats` added by PR #180 (15:59 UTC+8) —
  vol-managed Lab candidate's earnings-beat overlay.

PR #171 landed at 15:07 UTC+8 on the same day; the divergence is
strictly post-audit drift that the existing drift sentinel
(`test_dispatchable_engine_declares_data_dependencies`) can't catch
(it asserts non-empty, not source-match).

### catalyst — add `earnings_events`

Staged ECR: `ecr_catalyst_data_dependencies_2026-05-20.txt` at the repo
root. **Not applied** — the planner currently rejects MODIFY ECRs that
carry `data_dependencies` (see Follow-up below).

Planner output:

```
$ .venv/bin/python -m ops.engine_sdlc --ecr ecr_catalyst_data_dependencies_2026-05-20.txt
ecr.parse_fail error="1 validation error for EngineChangeRequest
  Value error, field(s) ['data_dependencies', 'need'] not valid for action MODIFY
   [type=value_error, input_value={'action': <ECRAction.MOD...e audit captured in PR'}, input_type=dict]"
ECR parse failed: …
```

### momentum — add `earnings_events`

Staged ECR: `ecr_momentum_data_dependencies_2026-05-20.txt` at the repo
root. **Not applied** — same planner limitation. The legacy 12-1 path
ignores `earnings_by_ticker`, but `_load_earnings_beats` itself executes
the SQL every window load, so the dependency is real for any window
context the live engine instantiates.

Both ECR files are shipped staged so the operator can apply them after
the planner-extension follow-up below lands. Until then, the per-engine
capital gate under-protects the `earnings_events`-dependent paths in
catalyst and momentum.

## Follow-up: extend ECR MODIFY to thread `data_dependencies`

Surfaces as a follow-up to PR #191 (spec §7). Today
(`ops/engine_sdlc/ecr.py:31`):

```python
_MODIFY_KEYS = {"lab_dossier", "param_change", "gate_dsr", "gate_cred"}
```

And (`ops/engine_sdlc/planner.py:142-145`):

```python
elif ecr.action is ECRAction.MODIFY:
    extra = {"lab_dossier": ecr.lab_dossier,
             "param_change": ecr.param_change,
             "gate_dsr": ecr.gate_dsr, "gate_cred": ecr.gate_cred}
```

Required changes (separate PR; out of scope for this audit):

1. Add `"data_dependencies"` to `_MODIFY_KEYS` (and `"need"` if free-text
   evidence on MODIFYs is desired — currently ADD-only).
2. Extend `attach_ecr_context`'s MODIFY branch to thread
   `ecr.data_dependencies` onto `extra` (mirroring the ADD branch at
   `planner.py:141`).
3. Extend `planner._apply_modify` (or equivalent) to re-render the
   `EngineProfile.data_dependencies=…` kwarg on the existing `_PROFILE`
   row — the rewrite path is targeted-line + AST-validated like
   `_rewrite_profile_source` (`planner.py:150-188`).
4. Add a drift test that round-trips
   `parse_ecr(MODIFY with data_dependencies)` → `attach_ecr_context` →
   apply → `engine_data_dependencies(engine)` equals the declared set.

Once shipped, the staged ECRs (`ecr_catalyst_data_dependencies_2026-05-20.txt`
and `ecr_momentum_data_dependencies_2026-05-20.txt`) can be applied via
`python -m ops.engine_sdlc --ecr <file>` through the canonical
hook-respecting path — no hand-edit of `_PROFILE`.

### Drift-prevention follow-up — source-match audit clockwork

Recommend a second drift test that ratchets this audit's logic into CI:
for every PAPER/LIVE engine, grep `<engine>/{plugs,scheduler.py,backtest.py,order_manager.py}`
for `platform\.<known-feed-table>` references; require that the discovered
set ⊆ `EngineProfile.data_dependencies` (modulo the
`data_quality_log`/`application_log`/`open_orders`/`risk_state`/`aar_events`/
`universe_candidates`/`allocations` allowlist enumerated in PR #171's
docstring). This would have red'd both PR #178 and PR #180 at merge time.

## Graduation watch — carver LAB → PAPER

When carver graduates from LAB to PAPER, the drift sentinel
(`test_dispatchable_engine_declares_data_dependencies`) will red on
`carver`'s empty `data_dependencies`. The graduating ECR must declare:

```
data_dependencies: prices_daily, liquidity_tiers
```

(evidence: `carver/backtest.py:182, 193`). Recorded here so the
graduation PR doesn't need a re-audit.

## Gate verification

Both `pytest -p no:xdist -p no:cacheprovider -q` (default order) and
`pytest -p no:randomly -p no:xdist -p no:cacheprovider -q` (order-flip)
plus `ruff check . --statistics` and
`tpcore.scripts.check_imports tpcore ops reversion vector momentum sentinel canary catalyst carver`
results recorded in the cover-letter / PR body. This audit makes
zero source-code changes — only adds the audit doc and a staged ECR
artifact — so all four gates remain at baseline.
