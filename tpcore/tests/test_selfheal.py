"""Unit tests for the generic self-heal orchestrator + registry.

The orchestrator is pure (run_stage injected), so these run with a
fake stage runner and a fake pool whose red-set advances per
validation cycle — no DB, no subprocess.
"""
from __future__ import annotations

from tpcore.selfheal.orchestrator import run_self_heal
from tpcore.selfheal.registry import HEAL_SPECS, registry_drift, spec_for


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args):
        # Each validation cycle consumes the next red-set.
        reds = self._pool.red_sequence[self._pool.cycle]
        self._pool.cycle = min(self._pool.cycle + 1, len(self._pool.red_sequence) - 1)
        return [{"source": f"validation.{c}"} for c in reds]

    async def fetchval(self, sql: str, *args):
        # The vendor-late probes call MAX(date) FROM platform.aaii_sentiment;
        # the fake pool returns whatever the test put in our_latest_by_table.
        for table, value in self._pool.our_latest_by_table.items():
            if table in sql:
                return value
        return None

    async def fetchrow(self, sql: str, *args):
        # The macro_indicators probe uses a per-series MIN(MAX) query.
        # Route by table substring.
        for table, value in self._pool.our_latest_by_table.items():
            if table in sql:
                return {"our_latest": value}
        return None


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    """red_sequence[i] = bare check names red after the i-th
    data_validation run."""

    def __init__(
        self,
        red_sequence: list[list[str]],
        *,
        our_latest_by_table: dict[str, object] | None = None,
    ) -> None:
        self.red_sequence = red_sequence or [[]]
        self.cycle = 0
        # Mock per-table MAX(date) responses for the vendor-late probes.
        # Test passes {"aaii_sentiment": date(...), "macro_indicators":
        # date(...)} to drive the fake fetchval/fetchrow.
        self.our_latest_by_table = our_latest_by_table or {}

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


def _runner(*, fail_stage: str | None = None, fail_rc: int = 1):
    """Fake run_stage; records calls; optional forced failure."""
    calls: list[tuple[str, dict]] = []

    async def run_stage(stage: str, params: dict) -> int:
        calls.append((stage, dict(params)))
        if fail_stage is not None and stage == fail_stage:
            return fail_rc
        return 0

    run_stage.calls = calls  # type: ignore[attr-defined]
    return run_stage


async def test_green_first_pass() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([[]]), rs)
    assert out.green is True
    assert out.iterations == 1
    assert out.healed == []
    assert out.escalated == []
    # only the data_validation refresh ran, no repair
    assert [c[0] for c in rs.calls] == ["data_validation"]


async def test_heals_on_retry() -> None:
    rs = _runner()
    out = await run_self_heal(
        _Pool([["prices_daily_completeness"], []]), rs
    )
    assert out.green is True
    assert out.iterations == 2
    assert "daily_bars" in out.healed
    assert ("daily_bars", {"repair_gaps": "true"}) in rs.calls


async def test_unhealable_escalates_immediately() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([["fundamentals_integrity"]]), rs)
    assert out.green is False
    assert any("fundamentals" in s for s, _ in out.escalated)
    # no repair stage attempted — only the validation refresh
    assert [c[0] for c in rs.calls] == ["data_validation"]


async def test_unknown_red_escalates() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([["totally_new_check"]]), rs)
    assert out.green is False
    assert any("unknown red" in r for _, r in out.escalated)


async def test_failed_repair_escalates() -> None:
    rs = _runner(fail_stage="daily_bars", fail_rc=2)
    out = await run_self_heal(_Pool([["prices_daily_freshness"]]), rs)
    assert out.green is False
    assert any("exited 2" in r for _, r in out.escalated)


async def test_validation_stage_failure_escalates() -> None:
    rs = _runner(fail_stage="data_validation", fail_rc=3)
    out = await run_self_heal(_Pool([[]]), rs)
    assert out.green is False
    assert out.escalated and "data_validation" in out.escalated[0][0]


async def test_exhaustion_escalates() -> None:
    # Always red on a healable check; repair "succeeds" but never
    # clears → must exhaust and escalate, not loop forever.
    rs = _runner()
    out = await run_self_heal(
        _Pool([["prices_daily_completeness"]]), rs, max_iterations=3
    )
    assert out.green is False
    assert out.iterations == 3
    assert any("exhausted" in r for _, r in out.escalated)


def test_registry_in_lockstep_with_suite() -> None:
    """Clockwork guarantee: every validation check has a deliberate
    HealSpec decision; no missing, no extras. Adding a feed/check
    breaks this until a spec is recorded."""
    missing, extra = registry_drift()
    assert missing == set(), f"checks with no HealSpec: {missing}"
    assert extra == set(), f"HealSpecs for unknown checks: {extra}"


def test_every_spec_is_self_consistent() -> None:
    for name, spec in HEAL_SPECS.items():
        assert spec.check_name == name
        if spec.healable:
            assert spec.stage, f"{name}: healable but no stage"
        else:
            assert spec.unhealable_reason, f"{name}: unhealable but no reason"


def test_depends_on_resolves_to_known_healable_sources() -> None:
    """A derived feed must never silently depend on an unknown or
    UNHEALABLE upstream — that is exactly the fear_greed-class
    fake-heal (recompute no-ops forever because the real blocker is an
    upstream that can't heal). Every depends_on entry must be the
    `source` of some healable HealSpec."""
    healable_sources = {s.source for s in HEAL_SPECS.values() if s.healable}
    for name, spec in HEAL_SPECS.items():
        for dep in spec.depends_on:
            assert dep in healable_sources, (
                f"{name}: depends_on '{dep}' is not a known healable "
                f"HealSpec source — a derived feed depending on an "
                f"unhealable/unknown upstream can never self-heal"
            )


def test_depends_on_graph_is_acyclic() -> None:
    """Dependency-ordered healing (the HealProfile follow-up) requires
    a DAG; a cycle would deadlock topological heal ordering."""
    by_source: dict[str, tuple[str, ...]] = {}
    for spec in HEAL_SPECS.values():
        by_source.setdefault(spec.source, ())
        if spec.depends_on:
            by_source[spec.source] = by_source[spec.source] + spec.depends_on

    visiting: set[str] = set()
    done: set[str] = set()

    def _walk(node: str, path: tuple[str, ...]) -> None:
        if node in done:
            return
        assert node not in visiting, (
            f"depends_on cycle: {' -> '.join((*path, node))}"
        )
        visiting.add(node)
        for nxt in by_source.get(node, ()):
            _walk(nxt, (*path, node))
        visiting.discard(node)
        done.add(node)

    for src in list(by_source):
        _walk(src, ())


def test_spec_for_unknown_is_none() -> None:
    assert spec_for("no_such_check") is None


# ── Vendor-late probe consult (#165 facet 4 self-heal wiring) ──────────


async def test_vendor_late_skips_heal_and_classifies(monkeypatch) -> None:
    """When the probe positively says the vendor has nothing newer than
    our_latest, the orchestrator skips the heal stage AND records the
    source under vendor_late — distinct from escalated (not our defect)
    and distinct from healed (no work was done). The sacred green gate
    stays sacred: vendor-late reds leave the data_quality_log row red
    so green=False; the wrapper emits TRIGGER_VENDOR_LATE for INFO."""
    from datetime import date

    from tpcore.selfheal import orchestrator, probes

    async def vendor_late_probe(pool):
        return probes.VendorState(
            our_latest=date(2026, 5, 14),
            vendor_latest=date(2026, 5, 14),  # equal ⇒ vendor has nothing newer
            has_newer=False,
        )
    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", vendor_late_probe,
    )

    rs = _runner()
    out = await orchestrator.run_self_heal(
        _Pool([["aaii_sentiment_freshness"]]), rs, max_iterations=2,
    )

    # Sacred gate preserved — green=False because the row is still red.
    assert out.green is False
    # Heal stage NEVER ran for aaii (the probe spared the cycle).
    assert "aaii" not in {c[0] for c in rs.calls}
    # vendor_late surfaces the source + dates for the wrapper's INFO event.
    assert out.vendor_late == [
        ("aaii_sentiment", "2026-05-14", "2026-05-14"),
    ]
    # NOT classified as escalated (vendor-MISSED is not our defect).
    assert out.escalated == []
    # Early-exit instead of burning max_iterations on a hopeless re-probe.
    assert out.iterations == 1


async def test_vendor_has_newer_heals_as_usual(monkeypatch) -> None:
    """Probe says vendor HAS newer ⇒ existing heal-as-usual path runs
    (no vendor_late classification)."""
    from datetime import date

    from tpcore.selfheal import orchestrator, probes

    async def vendor_ahead_probe(pool):
        return probes.VendorState(
            our_latest=date(2026, 5, 7),
            vendor_latest=date(2026, 5, 14),
            has_newer=True,
        )
    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", vendor_ahead_probe,
    )

    rs = _runner()
    # First cycle red, second clears — heal stage advances state.
    out = await orchestrator.run_self_heal(
        _Pool([["aaii_sentiment_freshness"], []]), rs,
    )
    assert out.green is True
    assert out.vendor_late == []
    # The heal stage WAS run (probe said yes, do the heal).
    assert any("aaii" in c[0] for c in rs.calls)


async def test_probe_unavailable_falls_back_to_existing_heal(monkeypatch) -> None:
    """A source with NO entry in VENDOR_PROBES falls back to the
    existing heal-as-usual flow — no vendor_late classification, the
    repair stage runs (proves backward-compat for the majority of
    feeds that don't have a probe yet).

    Exercises a finra_short_interest red — finra's per-bulk-pull
    structural mismatch with the IBorrowDesk pattern (see
    tpcore.feeds.targeting docstring) means it has no
    latest_published / publication probe, so the source is also
    absent from VENDOR_PROBES."""
    from tpcore.selfheal import orchestrator, probes

    # Sanity: source we exercise has NO probe.
    assert "finra_short_interest" not in probes.VENDOR_PROBES

    rs = _runner()
    out = await orchestrator.run_self_heal(
        _Pool([["short_interest_freshness"], []]), rs,
    )
    assert out.green is True
    assert out.vendor_late == []
    # The repair stage ran — probe-less sources go through the unchanged path.
    assert ("finra_short_interest", {"skip_guard_days": "0"}) in rs.calls


async def test_vendor_late_with_unhealable_escalates_both_separately(
    monkeypatch,
) -> None:
    """A mixed iteration with one vendor-late + one unhealable red
    must escalate the unhealable AND surface the vendor-late entry —
    the wrapper needs to see both classifications independently."""
    from datetime import date

    from tpcore.selfheal import orchestrator, probes

    async def vendor_late_probe(pool):
        return probes.VendorState(
            our_latest=date(2026, 5, 14),
            vendor_latest=date(2026, 5, 14),
            has_newer=False,
        )
    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", vendor_late_probe,
    )

    rs = _runner()
    out = await orchestrator.run_self_heal(
        _Pool([["aaii_sentiment_freshness", "fundamentals_integrity"]]),
        rs,
    )
    assert out.green is False
    # vendor_late surfaced.
    assert out.vendor_late == [
        ("aaii_sentiment", "2026-05-14", "2026-05-14"),
    ]
    # Unhealable also surfaced — both classifications coexist.
    assert any("fundamentals" in s for s, _ in out.escalated)


async def test_probe_returning_none_falls_back_to_heal(monkeypatch) -> None:
    """If the probe returns None (vendor probe failed, our DB empty,
    malformed) the orchestrator stays strict — runs the heal as usual.
    A probe failure must never silently silence a heal cycle."""
    from tpcore.selfheal import orchestrator, probes

    async def broken_probe(pool):
        return None  # undeterminable
    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", broken_probe,
    )

    rs = _runner()
    out = await orchestrator.run_self_heal(
        _Pool([["aaii_sentiment_freshness"], []]), rs,
    )
    assert out.green is True
    assert out.vendor_late == []
    # The aaii heal WAS run (probe was None ⇒ fall back).
    assert any("aaii" in c[0] for c in rs.calls)
