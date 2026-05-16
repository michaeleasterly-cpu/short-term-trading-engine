# Adapter Readiness Checklist

Pre-merge checklist for any new data adapter (or substantial change to an existing one) in `tpcore/`. Every box must be checked before the PR ships.

Template: copy `tpcore/templates/adapter_template.py` as the starting point — it satisfies most of these by construction.

## 1. Error handling

- [ ] Every HTTP-making method is wrapped with `@with_retry` from `tpcore.outage`. No local `await asyncio.sleep()` retry loops, no `tenacity.AsyncRetrying`, no bare `try/except: continue`.
- [ ] Permanent failures (4xx-not-429: auth, not-found, bad request) raise immediately. No retries on permanent errors.
- [ ] At the public-method boundary, transient + exhausted-retry failures are mapped to `tpcore.outage.DataProviderOutage` (data adapters) or `tpcore.alpaca.BrokerUnavailableError` (broker adapters). Engine code must never see a raw `httpx.HTTPError`.
- [ ] If the adapter wraps an SDK that has its own retry (e.g., `alpaca-py`), document that and don't layer additional retry on top.

## 2. Logging

- [ ] Uses `structlog.get_logger(__name__)`. Never `print()`, never the stdlib `logging` module directly.
- [ ] INFO for successful operations with structured context (`ticker=`, `endpoint=`, `n_rows=`).
- [ ] DEBUG for high-frequency events (per-row writes, per-ticker fetches inside a loop).
- [ ] WARNING for retries (handled by `@with_retry` — don't double-log).
- [ ] ERROR only for unrecoverable failures (`@with_retry` handles exhaustion automatically).

## 3. Configuration

- [ ] Credentials (API keys, tokens) come from environment variables via `os.getenv`. **Never** hardcoded.
- [ ] Base URLs default to env var, then to a sensible default. The default is a const at module level, not buried in the constructor.
- [ ] Missing required credentials raise `DataProviderOutage` at construction time, not on first request. Fail-fast.
- [ ] Tunable thresholds (rate-limit pauses, timeouts) are module-level constants or constructor args — not magic numbers in the middle of methods.

## 4. Interface compliance

- [ ] If the adapter implements an ABC (e.g., `DataProviderInterface`, `BrokerExecutionInterface`), **every** abstract method is implemented.
- [ ] Any method that legitimately can't be implemented yet raises `NotImplementedError("<one-line reason + link to follow-up issue/spec>")`. Stub docstrings explain why and what future work is needed.
- [ ] Pydantic models (v2) used for any structured response.
- [ ] All public methods type-hinted, including return type.

## 5. Tests

- [ ] Has a corresponding `tpcore/tests/test_<adapter>.py`.
- [ ] Happy path test: instantiate, call the primary method against `httpx.MockTransport`, assert correct shape.
- [ ] **Retry-on-429** test: mock returns 429 then 200; assert exactly one retry, assert eventual success.
- [ ] **No-retry-on-permanent** test: mock returns 403; assert exactly one call (no retry), assert correct outage type raised.
- [ ] **Outage mapping** test: persistent 5xx or network error → `DataProviderOutage` (not raw `HTTPError`).
- [ ] **Config error** test: missing required env var → raises `DataProviderOutage` at construction.
- [ ] If the adapter writes to the DB: physical-truth predicate test — bad rows (NULLs, out-of-range values) must be rejected at write time, not silently inserted. See `tpcore/tests/test_ingest_physical_truth.py` for the pattern.
- [ ] Optional live test gated by an env var (e.g., `RUN_PROVIDER_LIVE_TESTS=1`) for the operator's sanity check.

## 6. Rate limiting

- [ ] Documented limit (req/sec, req/min, daily quota) in a comment at the top of the adapter file.
- [ ] `@with_retry`'s `backoff_cap_sec` is set so the worst-case server-specified `Retry-After` is respected without stalling forever.
- [ ] If the API allows batch requests, the adapter uses them (e.g., `?symbols=A,B,C` over N single calls).
- [ ] Per-batch courtesy delay (e.g., `await asyncio.sleep(0.4)` between batches) for providers that enforce strict per-second limits.

## 7. Documentation

- [ ] Module docstring names the provider, the endpoints used, and the rate-limit class.
- [ ] Public methods have one-line docstrings describing what they return + what they raise.
- [ ] If the adapter is meant to be used as an async context manager, it has working `__aenter__` / `__aexit__` / `aclose`.

## 8. Ingestion-handler-specific (if applicable)

- [ ] Wired into `tpcore/ingestion/handlers.py` `HANDLERS` registry.
- [ ] Added as a stage in `scripts/ops.py:_STAGE_SPECS` with appropriate timeout (heavy stages: `HEAVY_STAGE_TIMEOUT_SEC`, light stages: `STAGE_TIMEOUT_SEC`).
- [ ] Added to `dashboard_components/health.py:OPS_UPDATE_STAGES` so the dashboard's per-stage panel reflects it.
- [ ] Has a skip-guard for high-cost / low-frequency operations (e.g., catalyst refresh: skip if last run < 6 days ago).
- [ ] Writes back to `platform.ingestion_jobs.last_run_at` on completion (TODO once that wiring lands; see follow-up).

## 9. Self-heal (pipeline stage 6 — mechanically enforced)

- [ ] A `HealSpec` for this feed's validation check is registered in `tpcore/selfheal/registry.py`. (The registry-coverage test in `tpcore/tests/test_selfheal.py` asserts the HealSpec set == `suite.KNOWN_CHECK_NAMES` — the build is RED until this exists. You cannot forget self-heal.)
- [ ] The HealSpec decision is deliberate and honest: **either** `healable=True` with a *bounded targeted* repair, **or** `healable=False` with a real `unhealable_reason`. No silent gap, no fake-green.
- [ ] If `healable=True`: the repair is a **bounded targeted** mode on the canonical `ops.py` stage (the `daily_bars --param repair_gaps=true` pattern). It re-pulls ONLY the invariant-flagged rows, over a window bracketing the oldest miss. **Never a whole-universe `force_refresh`** — proven 2026-05-15 to exceed the 3600s stage timeout and so never actually self-heal.
- [ ] If `healable=True`: the repair's target set is computed from the **same evaluation function as the validation check** (cf. `_evaluate` shared by `check_prices_daily_completeness` + `compute_gap_repair_targets`) so detector and healer can never disagree.
- [ ] Heal executes ONLY via the canonical `ops.py --stage` path — no one-off script, no ingestion reimplemented in the orchestrator.
- [ ] `python -m tpcore.selfheal` was run against live data and the result is in the PR description (green, or the honest escalation if `healable=False`).
- [ ] No edits to `run_data_operations.sh` or `tpcore/selfheal/orchestrator.py` were needed — capability was added by the one declarative HealSpec line (if you had to touch the orchestrator, the abstraction is wrong — stop and fix that, don't special-case).

## 10. Final checks

- [ ] `ruff check .` clean.
- [ ] Full `pytest -q` passes — no regressions in other engines/adapters.
- [ ] If the adapter introduces new env vars, they're documented in `docs/OPERATIONS.md` under "Environment variables".
- [ ] If the adapter changes a public ABC, every implementer in the repo is updated in the same PR.

---

## Why this exists

Before this checklist, every adapter on the platform had its own:

- ad-hoc retry loop (`await asyncio.sleep(1.0)` or local tenacity AsyncRetrying)
- inconsistent logging (some `print`, some stdlib logging, some structlog)
- inconsistent error mapping (some raised `httpx.HTTPError`, some `RuntimeError`)
- partial tests (some had MockTransport, some only had live smoke tests, some had no tests)

The 2026-05-12 production failure (Alpaca 429 killed the whole Sunday cron because `handle_corporate_actions` had zero retry logic) is the motivating example. The shared `@with_retry` primitive plus this checklist guarantee the next adapter doesn't repeat that.

Reference implementations that follow the pattern:

- `tpcore/fmp/fundamentals_adapter.py` — full ABC + retry + outage mapping + tests.
- `tpcore/data/ingest_corporate_actions.py` — simple fetch + retry + tests.

Template:

- `tpcore/templates/adapter_template.py` — copy-paste-start.
