# Adapter Contract-Population Sentinel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a silent vendor contract change (a required adapter-output field gone systematically empty) and producer-hard-stop before the corrupt load, via a declared per-feed contract SoT, rolled out enforced to 4 high-risk feeds.

**Architecture:** A frozen `tpcore/ingestion/adapter_contract.py` registry (`ADAPTER_CONTRACTS`) + a pure `assert_contract_populated(feed, records)` helper that raises `AdapterContractDrift` when a declared `required_field` is empty in EVERY record of a non-empty pull. Wired into 4 high-risk handlers at the post-adapter/pre-load boundary; clockwork drift test pins the registry to the CSV-first feed set; a thin Step-4c `adapter_contract` audit check adds coverage/visibility. Rung-1 detector of the Escalation & Hardening Ladder.

**Tech Stack:** Python 3.11, pydantic v2 (frozen, extra=forbid), pytest (`asyncio_mode=auto`), ruff. Spec: `docs/superpowers/specs/2026-05-17-schema-contract-drift-sentinel-design.md`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `tpcore/ingestion/adapter_contract.py` | `AdapterContract` model, `ADAPTER_CONTRACTS` SoT, `AdapterContractDrift`, `assert_contract_populated` | P1 |
| `tpcore/tests/test_adapter_contract.py` | helper behaviour + clockwork drift + guard_pending pin | P1 |
| `tpcore/ingestion/handlers.py` | 4 enforced call sites (fred_macro, iborrowdesk, finra, apewisdom) | P2 |
| `tpcore/tests/test_adapter_contract_wiring.py` | per-handler: blanked required field raises pre-DB-write; normal passes | P2 |
| `scripts/audit_data_pipeline.py` | known_knowns `adapter_contract` check (coverage + pending WARN + escalation FAIL) | P3 |
| `tpcore/tests/test_audit_adapter_contract.py` | fake-pool audit-check tests | P3 |
| `CLAUDE.md`, `TODO.md`, the spec | reconciliation | P4 |

One phase = one gated PR. Branch off fresh `main` per phase; CI green before merge; verify branch name before every commit (`test "$(git branch --show-current)" = "<branch>"`). Implementers commit only — the controller opens/merges the PR after spec + code-quality review (auditheal pattern).

---

## Ground truth (verified from source — do not re-derive or guess)

**CSV-first feed set** (every `write_archive(<source>, …)` 1st arg in `tpcore/ingestion/handlers.py` + `scripts/ops.py`). The Phase-1 clockwork test enumerates this from the code; the verified current set is exactly these 12:
`fmp_fundamentals`, `alpaca_corporate_actions`, `alpaca_daily_bars`, `fred_macro_hist`, `fred_macro`, `fmp_earnings_events`, `greeks_max_pain`, `finnhub_insider_sentiment`, `apewisdom_social_sentiment`, `finra_short_interest`, `iborrowdesk_borrow_rates`, `aaii_sentiment`.

**4 high-risk feeds — verified contract data:**

| feed | accessor | required_fields | excluded (legitimately nullable) — evidence | records-in-scope at pre-load boundary |
|---|---|---|---|---|
| `apewisdom_social_sentiment` | `attr` | `ticker, name, rank, mentions, upvotes` | `rank_24h_ago, mentions_24h_ago` are `int \| None` in `SocialSentimentRecord` (tpcore/apewisdom/adapter.py:46-47) | `records = await adapter.get_all_sentiment()` (handlers.py ~1670, full list pre-universe-filter) |
| `finra_short_interest` | `attr` | `ticker, settlement_date, short_position_qty` | `days_to_cover` is `Decimal \| None` (tpcore/finra/adapter.py:60); `short_interest_pct` is handler-derived, not an adapter field, legit None | `records` (FinraAdapter output, the list in `for rec in records:` ~handlers.py:1935) |
| `iborrowdesk_borrow_rates` | `attr` | `ticker, date, borrow_rate_pct` | none | per-ticker loop builds `rows`; add a minimal parallel `recs: list = []` + `recs.append(rec)` next to the existing `rows.append(...)` (~handlers.py:2070); check `recs` |
| `fred_macro` | `key` | `date` only | `value` is `Decimal \| None` in `get_all_indicators` (tpcore/fred/adapter.py:168 docstring "{'date': date, 'value': Decimal \| None}") — value is legitimately null (missing FRED observations); NOT a required field | flatten: `all_obs = [o for lst in per_indicator.values() for o in lst]` (after `per_indicator = await fred.get_all_indicators(...)`, ~handlers.py:1345) |

**Empty-test semantics (locked):** a field value is "empty" iff it is `None` or `""` (empty string). For numeric fields `0`/`0.0`/`Decimal(0)` is a VALID value, NOT empty. The helper treats `None` and `""` as empty for all field types; it never treats `0` as empty.

**Patterns to mirror:** registry + drift test like `tpcore/auditheal/registry.py` (`registry_drift()`); pydantic frozen model like `tpcore/auditheal/spec.py`; producer-raise like `assert_not_shrunk` in `tpcore/ingestion/csv_archive.py` (handlers already `from tpcore.ingestion.csv_archive import assert_not_shrunk` then call it post-`write_archive`). pytest `asyncio_mode=auto`.

---

## Phase 1 — registry + helper, landed dark (PR 1)

Branch: `feat/adapter-contract-p1`.

### Task 1.1: `AdapterContract` model + `ADAPTER_CONTRACTS` + helper

**Files:**
- Create: `tpcore/ingestion/adapter_contract.py`
- Test: `tpcore/tests/test_adapter_contract.py`

- [ ] **Step 1: Write the failing test** — create `tpcore/tests/test_adapter_contract.py`:

```python
"""Unit tests for the adapter contract-population sentinel (#186(6))."""
from __future__ import annotations

import re

import pytest

from tpcore.ingestion.adapter_contract import (
    ADAPTER_CONTRACTS,
    AdapterContract,
    AdapterContractDrift,
    assert_contract_populated,
    contract_drift,
)

# The CSV-first feed set, enumerated from every write_archive() 1st arg
# in tpcore/ingestion/handlers.py + scripts/ops.py. This test re-derives
# it from source so it cannot silently rot.
import pathlib


def _csv_first_feeds() -> set[str]:
    feeds: set[str] = set()
    pat = re.compile(r"write_archive\(\s*\n?\s*\"([a-z0-9_]+)\"")
    for rel in ("tpcore/ingestion/handlers.py", "scripts/ops.py"):
        txt = pathlib.Path(rel).read_text()
        feeds.update(pat.findall(txt))
    return feeds


def test_registry_in_lockstep_with_csv_first_feeds() -> None:
    missing, extra = contract_drift()
    assert missing == set(), f"CSV-first feeds with no AdapterContract: {missing}"
    assert extra == set(), f"AdapterContracts for non-CSV-first feeds: {extra}"
    # And the registry equals the live source-derived set exactly.
    assert set(ADAPTER_CONTRACTS) == _csv_first_feeds()


def test_guard_pending_set_is_pinned() -> None:
    enforced = {f for f, c in ADAPTER_CONTRACTS.items() if not c.guard_pending}
    assert enforced == {
        "fred_macro",
        "iborrowdesk_borrow_rates",
        "finra_short_interest",
        "apewisdom_social_sentiment",
    }


def test_every_contract_self_consistent() -> None:
    for feed, c in ADAPTER_CONTRACTS.items():
        assert c.feed == feed
        assert c.required_fields, f"{feed}: required_fields empty"
        assert c.accessor in ("attr", "key")
        assert c.evidence, f"{feed}: evidence empty (no-vendor-blame)"


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def test_empty_payload_is_noop() -> None:
    assert_contract_populated("apewisdom_social_sentiment", [])  # no raise


async def test_all_null_required_field_raises() -> None:
    recs = [_Rec(ticker=None, name="A", rank=1, mentions=2, upvotes=3),
            _Rec(ticker=None, name="B", rank=4, mentions=5, upvotes=6)]
    with pytest.raises(AdapterContractDrift, match="ticker"):
        assert_contract_populated("apewisdom_social_sentiment", recs)


async def test_single_stray_null_tolerated() -> None:
    recs = [_Rec(ticker=None, name="A", rank=1, mentions=2, upvotes=3),
            _Rec(ticker="MSFT", name="B", rank=4, mentions=5, upvotes=6)]
    assert_contract_populated("apewisdom_social_sentiment", recs)  # no raise


async def test_zero_is_not_empty() -> None:
    recs = [_Rec(ticker="MSFT", name="A", rank=0, mentions=0, upvotes=0)]
    assert_contract_populated("apewisdom_social_sentiment", recs)  # 0 is valid


async def test_key_accessor_for_dict_records() -> None:
    ok = [{"date": "2026-05-01"}, {"date": "2026-05-02"}]
    assert_contract_populated("fred_macro", ok)  # no raise
    bad = [{"date": None}, {"date": ""}]
    with pytest.raises(AdapterContractDrift, match="date"):
        assert_contract_populated("fred_macro", bad)


async def test_unknown_feed_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        assert_contract_populated("not_a_feed", [_Rec(x=1)])
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.ingestion.adapter_contract`).

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_adapter_contract.py -q`

- [ ] **Step 3: Create `tpcore/ingestion/adapter_contract.py`**

```python
"""Adapter contract-population sentinel — #186(6), Escalation &
Hardening Ladder rung 1.

A vendor renames/removes a field; the adapter absorbs it with a silent
``.get()``/default; the table loads structurally-fine but
semantically-empty rows (shrinkage- and header-blind). This detects
the SYMPTOM: a declared ``required_field`` empty in EVERY record of a
non-empty pull = unambiguous contract drift → producer hard-stop
before the load (no safe auto-heal — escalate-only).

Declarative SoT (mirrors HealSpec/RemediationSpec): one frozen
``AdapterContract`` per CSV-first feed. ``required_fields`` are the
adapter-output fields a valid vendor record ALWAYS populates; fields
that are legitimately nullable in some valid window (finra
``days_to_cover``, fred ``value``) are deliberately excluded — see
each entry's ``evidence``.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class AdapterContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feed: str
    required_fields: frozenset[str]
    accessor: Literal["attr", "key"]
    guard_pending: bool = False
    evidence: str = ""


class AdapterContractDrift(RuntimeError):
    """A required adapter-output field is empty across an entire
    non-empty pull — the vendor contract changed. Escalate-only."""


# Verified from each adapter's record model + handler (2026-05-17).
ADAPTER_CONTRACTS: dict[str, AdapterContract] = {
    # ── Enforced (high-risk: silent-truncation / scrape-fragile) ──
    "fred_macro": AdapterContract(
        feed="fred_macro", accessor="key",
        required_fields=frozenset({"date"}),
        evidence="get_all_indicators -> {'date': date, 'value': "
                 "Decimal|None} (tpcore/fred/adapter.py:168). value is "
                 "legitimately null (missing FRED obs) so excluded; "
                 "date is always parsed or the obs is dropped."),
    "iborrowdesk_borrow_rates": AdapterContract(
        feed="iborrowdesk_borrow_rates", accessor="attr",
        required_fields=frozenset({"ticker", "date", "borrow_rate_pct"}),
        evidence="BorrowRateRecord(ticker:str, date:date, "
                 "borrow_rate_pct) — all always populated "
                 "(tpcore/iborrowdesk/adapter.py:39-49)."),
    "finra_short_interest": AdapterContract(
        feed="finra_short_interest", accessor="attr",
        required_fields=frozenset(
            {"ticker", "settlement_date", "short_position_qty"}),
        evidence="ShortInterestRecord (tpcore/finra/adapter.py:47-60): "
                 "days_to_cover is Decimal|None -> excluded; "
                 "short_interest_pct is handler-derived not an adapter "
                 "field -> excluded; the 3 listed always populated."),
    "apewisdom_social_sentiment": AdapterContract(
        feed="apewisdom_social_sentiment", accessor="attr",
        required_fields=frozenset(
            {"ticker", "name", "rank", "mentions", "upvotes"}),
        evidence="SocialSentimentRecord (tpcore/apewisdom/adapter.py:"
                 "36-47): rank_24h_ago/mentions_24h_ago are int|None "
                 "-> excluded; the 5 listed are non-optional."),
    # ── Declared, guard_pending (rollout per HealSpec #132 pattern) ──
    "fmp_fundamentals": AdapterContract(
        feed="fmp_fundamentals", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: contract declared for coverage; "
                 "enforced wiring is a later increment."),
    "alpaca_corporate_actions": AdapterContract(
        feed="alpaca_corporate_actions", accessor="key",
        guard_pending=True, required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "alpaca_daily_bars": AdapterContract(
        feed="alpaca_daily_bars", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "fred_macro_hist": AdapterContract(
        feed="fred_macro_hist", accessor="key", guard_pending=True,
        required_fields=frozenset({"date"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "fmp_earnings_events": AdapterContract(
        feed="fmp_earnings_events", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "greeks_max_pain": AdapterContract(
        feed="greeks_max_pain", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "finnhub_insider_sentiment": AdapterContract(
        feed="finnhub_insider_sentiment", accessor="key",
        guard_pending=True, required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
    "aaii_sentiment": AdapterContract(
        feed="aaii_sentiment", accessor="key", guard_pending=True,
        required_fields=frozenset({"date"}),
        evidence="guard_pending: declared for coverage; enforced "
                 "wiring later."),
}


def _is_empty(v: Any) -> bool:
    # None or "" is empty. 0 / 0.0 / Decimal(0) are VALID values.
    return v is None or v == ""


def _read(rec: Any, field: str, accessor: str) -> Any:
    if accessor == "attr":
        return getattr(rec, field, None)
    try:
        return rec[field]
    except (KeyError, TypeError):
        return None


def contract_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) vs the CSV-first feed set, re-derived from the
    write_archive call sites. Both empty == in lockstep."""
    import pathlib
    import re

    pat = re.compile(r"write_archive\(\s*\n?\s*\"([a-z0-9_]+)\"")
    feeds: set[str] = set()
    for rel in ("tpcore/ingestion/handlers.py", "scripts/ops.py"):
        feeds.update(pat.findall(pathlib.Path(rel).read_text()))
    have = set(ADAPTER_CONTRACTS)
    return feeds - have, have - feeds


def assert_contract_populated(
    feed: str, records: Sequence[Any]
) -> None:
    """Raise AdapterContractDrift if any declared required_field is
    empty (None/"") in EVERY record of a non-empty payload. No-op on
    an empty payload (a freshness/coverage concern other checks own)."""
    contract = ADAPTER_CONTRACTS[feed]  # KeyError = unknown feed (loud)
    if not records:
        return
    for field in sorted(contract.required_fields):
        if all(
            _is_empty(_read(r, field, contract.accessor)) for r in records
        ):
            raise AdapterContractDrift(
                f"adapter_contract_drift: feed={feed!r} required field "
                f"{field!r} is empty in all {len(records)} records — "
                f"vendor contract changed (escalate-only; no auto-heal)"
            )


__all__ = [
    "ADAPTER_CONTRACTS",
    "AdapterContract",
    "AdapterContractDrift",
    "assert_contract_populated",
    "contract_drift",
]
```

- [ ] **Step 4: Run the tests — all must pass**

Run: `python -m pytest tpcore/tests/test_adapter_contract.py -q`
Expected: PASS (10 tests). If `test_registry_in_lockstep_with_csv_first_feeds` fails, the regex/feed-set is wrong — re-derive from the actual `write_archive` call sites (read the code; do not edit the test to pass).

- [ ] **Step 5: Ruff + full collection**

Run: `ruff check tpcore/ingestion/adapter_contract.py tpcore/tests/test_adapter_contract.py && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1`
Expected: clean; collection succeeds.

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "feat/adapter-contract-p1" || { echo WRONG; exit 1; }
git add tpcore/ingestion/adapter_contract.py tpcore/tests/test_adapter_contract.py
git commit -m "feat(adapter-contract): registry + assert_contract_populated (dark)"
```
STOP after commit. No push/PR/merge/switch. Report DONE (10/10, ruff, collection count, commit SHA) or BLOCKED.

---

## Phase 2 — wire the 4 high-risk handlers (PR 2)

Branch: `feat/adapter-contract-p2` off fresh `main`.

### Task 2.1: Wire all 4 enforced call sites + per-handler tests

**Files:**
- Modify: `tpcore/ingestion/handlers.py` (4 sites)
- Test: `tpcore/tests/test_adapter_contract_wiring.py`

The call is always: `from tpcore.ingestion.adapter_contract import assert_contract_populated` then `assert_contract_populated("<feed>", <records-in-scope>)` placed AFTER the adapter returns and BEFORE the first DB write / `write_archive` for that feed. Read each site first (`sed -n` the line range), then make the minimal edit.

- [ ] **Step 1: Read the 4 sites**

Run: `sed -n '1340,1385p' tpcore/ingestion/handlers.py` (fred_macro), `sed -n '1665,1700p' tpcore/ingestion/handlers.py` (apewisdom), `sed -n '1930,1965p' tpcore/ingestion/handlers.py` (finra), `sed -n '2055,2090p' tpcore/ingestion/handlers.py` (iborrowdesk). Confirm the exact variable names + insert points below; if a line number drifted, locate by the quoted `write_archive("<feed>"` anchor.

- [ ] **Step 2: fred_macro** — after `per_indicator = await fred.get_all_indicators(...)` and before `upsert_rows`/`write_archive`, insert:

```python
    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated(
        "fred_macro",
        [o for lst in per_indicator.values() for o in lst],
    )
```

- [ ] **Step 3: apewisdom** — immediately after `records = await adapter.get_all_sentiment()` (before the universe filter / dedup loop), insert:

```python
    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("apewisdom_social_sentiment", records)
```

- [ ] **Step 4: finra** — immediately after the FinraAdapter call that produces `records` and before the `for rec in records:` derivation loop, insert:

```python
    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("finra_short_interest", records)
```

- [ ] **Step 5: iborrowdesk** — the handler builds `rows` tuple-by-tuple in a per-ticker loop. Add a parallel record list with NO behavior change: where `rows: list[tuple] = []` is declared, add `recs: list = []` next to it; in the loop, immediately after the existing `rows.append((rec.ticker, rec.date, float(rec.borrow_rate_pct)))`, add `recs.append(rec)`. Then immediately after the loop ends and before the `if not rows:`/`write_archive`, insert:

```python
    from tpcore.ingestion.adapter_contract import assert_contract_populated
    assert_contract_populated("iborrowdesk_borrow_rates", recs)
```

(`recs` is the raw adapter `BorrowRateRecord`s; `assert_contract_populated` no-ops if empty, so the existing `if not rows:` early-return path is unaffected.)

- [ ] **Step 6: Write the wiring test** — create `tpcore/tests/test_adapter_contract_wiring.py`:

```python
"""Each enforced handler raises AdapterContractDrift BEFORE any DB
write when the adapter output has a required field blanked across the
whole payload, and passes on a normal payload (including a
legitimately-null NON-required field)."""
from __future__ import annotations

import pytest

from tpcore.ingestion.adapter_contract import (
    AdapterContractDrift,
    assert_contract_populated,
)


class _R:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_finra_blanked_required_raises_but_null_optional_ok() -> None:
    # short_position_qty blanked in every record -> drift.
    drift = [_R(ticker="AAA", settlement_date="2026-05-01",
                short_position_qty=None, days_to_cover=None)]
    with pytest.raises(AdapterContractDrift, match="short_position_qty"):
        assert_contract_populated("finra_short_interest", drift)
    # days_to_cover all-None is LEGIT (excluded) -> no raise.
    ok = [_R(ticker="AAA", settlement_date="2026-05-01",
             short_position_qty=10, days_to_cover=None)]
    assert_contract_populated("finra_short_interest", ok)


def test_fred_value_all_null_is_ok_date_all_null_raises() -> None:
    # value legitimately null everywhere -> NOT drift.
    assert_contract_populated(
        "fred_macro",
        [{"date": "2026-05-01", "value": None},
         {"date": "2026-05-02", "value": None}],
    )
    with pytest.raises(AdapterContractDrift, match="date"):
        assert_contract_populated(
            "fred_macro",
            [{"date": None, "value": "1.2"}, {"date": "", "value": "1.3"}],
        )


def test_iborrowdesk_and_apewisdom_required_blank_raises() -> None:
    with pytest.raises(AdapterContractDrift, match="borrow_rate_pct"):
        assert_contract_populated(
            "iborrowdesk_borrow_rates",
            [_R(ticker="AAA", date="2026-05-01", borrow_rate_pct=None)],
        )
    with pytest.raises(AdapterContractDrift, match="mentions"):
        assert_contract_populated(
            "apewisdom_social_sentiment",
            [_R(ticker="AAA", name="x", rank=1, mentions=None, upvotes=2)],
        )
```

- [ ] **Step 7: Run + lint + collection**

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_adapter_contract_wiring.py tpcore/tests/test_adapter_contract.py -q && ruff check tpcore/ingestion/handlers.py tpcore/tests/test_adapter_contract_wiring.py && python -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('h','tpcore/ingestion/handlers.py'); m=importlib.util.module_from_spec(s); sys.modules['h']=m; s.loader.exec_module(m); print('handlers import OK')" && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1`
Expected: tests pass; ruff clean; `handlers import OK`; full collection clean.

- [ ] **Step 8: Commit**

```bash
test "$(git branch --show-current)" = "feat/adapter-contract-p2" || { echo WRONG; exit 1; }
git add tpcore/ingestion/handlers.py tpcore/tests/test_adapter_contract_wiring.py
git commit -m "feat(adapter-contract): enforce in fred/iborrowdesk/finra/apewisdom handlers"
```
STOP. Report DONE (test counts, ruff, handlers import, commit SHA) or BLOCKED. If any handler's variable name / insert point differs from Steps 2-5, report it (do not guess) — controller re-verifies.

---

## Phase 3 — Step-4c audit check (PR 3)

Branch: `feat/adapter-contract-p3` off fresh `main`.

### Task 3.1: known_knowns `adapter_contract` check

**Files:**
- Modify: `scripts/audit_data_pipeline.py`
- Test: `tpcore/tests/test_audit_adapter_contract.py`

- [ ] **Step 1: Read the audit's known_knowns shape** — `sed -n '263,345p' scripts/audit_data_pipeline.py` and the `_append_shrinkage_finding` pattern (`grep -n "shrinkage_detector\|AuditFinding(\|def _append" scripts/audit_data_pipeline.py`). The new check mirrors how `shrinkage_detector` appends an `AuditFinding(phase="known_knowns", check_name="adapter_contract", source=..., severity=..., summary=..., recommended_action=...)`.

- [ ] **Step 2: Write the failing test** — create `tpcore/tests/test_audit_adapter_contract.py`:

```python
"""The known_knowns adapter_contract check: coverage OK, pending WARN,
recent-escalation FAIL."""
from __future__ import annotations

from scripts.audit_data_pipeline import _adapter_contract_findings


class _Conn:
    def __init__(self, escalations: int) -> None:
        self._n = escalations

    async def fetchval(self, *a, **k):
        return self._n


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, escalations: int = 0) -> None:
        self._c = _Conn(escalations)

    def acquire(self): return _CM(self._c)


async def test_coverage_ok_and_pending_warn() -> None:
    findings = await _adapter_contract_findings(_Pool(0))
    names = {(f.check_name, f.severity) for f in findings}
    assert ("adapter_contract", "OK") in names      # coverage in lockstep
    assert any(f.severity == "WARN" and "guard_pending" in f.summary
               for f in findings)                    # pending-gap visible


async def test_recent_escalation_fails() -> None:
    findings = await _adapter_contract_findings(_Pool(2))
    assert any(f.check_name == "adapter_contract" and f.severity == "FAIL"
               and "escalation" in f.summary.lower() for f in findings)
```

- [ ] **Step 3: Run — expect FAIL** (`ImportError: cannot import name '_adapter_contract_findings'`).

- [ ] **Step 4: Implement `_adapter_contract_findings` + call it in `run_known_knowns`.** Add near the shrinkage helpers in `scripts/audit_data_pipeline.py`:

```python
async def _adapter_contract_findings(pool) -> list[AuditFinding]:
    """#186(6) thin Step-4c check. The producer raise is authoritative;
    this adds (1) registry-coverage, (2) guard_pending visibility,
    (3) recent unacknowledged adapter_contract_drift escalations.
    It CANNOT re-derive drift post-cycle (adapter output is gone) —
    deliberately thinner than shrinkage_detector (spec §6)."""
    from tpcore.ingestion.adapter_contract import (
        ADAPTER_CONTRACTS,
        contract_drift,
    )

    out: list[AuditFinding] = []
    missing, extra = contract_drift()
    if missing or extra:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="registry", severity="FAIL",
            summary=f"ADAPTER_CONTRACTS drift: missing={sorted(missing)} "
                    f"extra={sorted(extra)}",
            recommended_action="declare/remove the AdapterContract"))
    else:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="registry", severity="OK",
            summary="ADAPTER_CONTRACTS in lockstep with CSV-first feeds"))

    pending = sorted(f for f, c in ADAPTER_CONTRACTS.items()
                     if c.guard_pending)
    if pending:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="guard_pending", severity="WARN",
            summary=f"guard_pending (declared, enforced wiring not yet "
                    f"rolled out): {pending}",
            recommended_action="rollout increment: flip guard_pending "
                               "+ wire assert_contract_populated"))

    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM platform.application_log
            WHERE event_type = 'INGESTION_FAILED'
              AND data->>'reason' = 'adapter_contract_drift'
              AND recorded_at > now() - interval '24 hours'
            """
        )
    if n and int(n) > 0:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="escalation", severity="FAIL",
            summary=f"{int(n)} adapter_contract_drift escalation(s) in "
                    f"the last 24h — a vendor contract changed",
            recommended_action="update the adapter/contract to the "
                               "vendor's new shape, then re-run"))
    return out
```

Then, inside `run_known_knowns`, after the shrinkage findings are appended, append these too:

```python
    for f in await _adapter_contract_findings(pool):
        findings.append(f)
```

(Match the existing `findings.append(...)` / sink convention exactly — read how `_append_shrinkage_finding` is invoked and mirror it.)

- [ ] **Step 5: Run tests + ruff + collection**

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_audit_adapter_contract.py -q && ruff check scripts/audit_data_pipeline.py tpcore/tests/test_audit_adapter_contract.py && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1`
Expected: 2 pass; ruff clean; collection clean. If the `application_log` column is `data` vs `data::jsonb` or `event_type` differs, read the table's other queries in the same file and match exactly (do not guess the schema).

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "feat/adapter-contract-p3" || { echo WRONG; exit 1; }
git add scripts/audit_data_pipeline.py tpcore/tests/test_audit_adapter_contract.py
git commit -m "feat(adapter-contract): thin Step-4c adapter_contract known_knowns check"
```
STOP. Report DONE or BLOCKED.

---

## Phase 4 — docs reconciliation (PR 4)

Branch: `docs/adapter-contract-p4` off fresh `main`.

### Task 4.1: Reconcile docs

**Files:** `CLAUDE.md`, `TODO.md`, `docs/superpowers/specs/2026-05-17-schema-contract-drift-sentinel-design.md`

- [ ] **Step 1: CLAUDE.md** — `grep -n "shrinkage\|producer self-validation\|Escalation.*Hardening\|coverage-collapse" CLAUDE.md`. In the producer-self-validation / data-layer-acceptance area, add one factual sentence: the adapter contract-population sentinel (`tpcore/ingestion/adapter_contract.py`) producer-hard-stops a stage when a declared required adapter-output field is empty across an entire non-empty pull (vendor contract drift; escalate-only), enforced on fred_macro/iborrowdesk/finra/apewisdom, the rest declared+guard_pending; it is rung 1 of the Escalation & Hardening Ladder. No emojis, match surrounding style, surgical edit.

- [ ] **Step 2: TODO.md** — `grep -n "#186\|candidate (6)\|schema.*drift\|deterministic data agents" TODO.md`. Record candidate (6) **DONE 2026-05-17** — adapter contract-population sentinel (symptom-level: required-field systematic-emptiness → producer hard-stop; declared SoT + clockwork; high-risk set enforced, rest guard_pending; thin Step-4c check). Note #186 now: (3)/(4) realized by #165, (5) auditheal done, (6) done → the remaining deterministic-agents work is the **Data Supervisor** (Ladder rung 2) + #187 LLM triage (rung 3). Match the file's DONE-item format.

- [ ] **Step 3: Spec status** — set `**Status:**` to `BUILT 2026-05-17` and append a `**Build record:**` listing P1/P2/P3/P4 with their PR numbers (controller fills the exact numbers when merging; leave them as `#P1/#P2/#P3/#P4` placeholders ONLY if unknown — otherwise the controller supplies them at merge and this step updates it). Mirror the build-record format in `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md`.

- [ ] **Step 4: Verify scope + commit**

```bash
source .venv/bin/activate && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1   # docs-only: still collects clean
git diff --stat   # exactly the 3 docs files
test "$(git branch --show-current)" = "docs/adapter-contract-p4" || { echo WRONG; exit 1; }
git add CLAUDE.md TODO.md docs/superpowers/specs/2026-05-17-schema-contract-drift-sentinel-design.md
git commit -m "docs: #186(6) adapter contract sentinel — reconcile CLAUDE.md/TODO/spec"
```
STOP. Report DONE.

---

## Self-Review

**1. Spec coverage:**
- §2 declared SoT + `assert_contract_populated` (all-null over non-empty; no-op empty; attr|key; None/"" empty, 0 valid) → Task 1.1 (model, registry, helper, 10 tests incl. zero-not-empty, stray-null-tolerated, key-accessor). ✓
- §2 producer hard-stop pre-load → Task 2.1 (4 sites, each post-adapter pre-write_archive). ✓
- §3 honest scope (legit-null excluded) → finra `days_to_cover`/`short_interest_pct`, fred `value` excluded with evidence; `test_finra_blanked_required_raises_but_null_optional_ok`, `test_fred_value_all_null_is_ok_date_all_null_raises`. ✓
- §4 clockwork drift test == CSV-first feed set (re-derived from source) → `test_registry_in_lockstep_with_csv_first_feeds` + `contract_drift()`. ✓
- §4 guard_pending pinned → `test_guard_pending_set_is_pinned`. ✓
- §5 rollout (4 enforced, rest guard_pending) → registry `guard_pending` flags + the pin test. ✓
- §6 thin Step-4c check (coverage + pending WARN + escalation FAIL; NOT re-derivation) → Task 3.1. ✓
- §7 Ladder seam: escalation shape = `INGESTION_FAILED reason=adapter_contract_drift` → the audit query keys on exactly that; the producer raise is `AdapterContractDrift` which fails the stage → `INGESTION_FAILED` (existing `_run_stage` behavior). ✓
- §9 phasing → 4 phases, 1 PR each. ✓
- §10 resolved by reading source → Ground-truth table (accessors, required_fields, exclusions, records-in-scope) all cited to file:line; clockwork test re-derives the feed set rather than freezing it. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step has full code. The only conditional is P4 Step-3 PR-number fill (controller-supplied at merge — explicit, not a placeholder gap). ✓

**3. Type consistency:** `AdapterContract(feed, required_fields: frozenset, accessor: Literal["attr","key"], guard_pending, evidence)`, `AdapterContractDrift`, `assert_contract_populated(feed, records)`, `contract_drift() -> (missing, extra)`, `_adapter_contract_findings(pool) -> list[AuditFinding]` — names/signatures identical across Tasks 1.1/2.1/3.1 and all tests. Empty-test rule (`None`/`""`, `0` valid) defined once in §Ground-truth and implemented once in `_is_empty`. ✓

(Carried to execution: Phase 2 line numbers are approximate — every site has an exact quoted `write_archive("<feed>"` / adapter-call anchor and Step 1 mandates reading the range first; the implementer reports any drift rather than guessing — the auditheal-execution discipline.)
