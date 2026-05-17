# Data Provider RETIRE / OFFBOARD Checklist

Gate for taking a provider (or a whole feed) **out**. Stage 7 of the
Data Provider Lifecycle (spec `…/specs/2026-05-17-data-provider-
lifecycle-design.md` §4; plan Phase 3). Mirrors
`data_provider_evaluate.md` — every box checked before the binding's
status becomes `RETIRED`.

**Why this gate exists:** this session's ad-hoc retirements (Sigma →
left a fake-healable HealSpec; FRED `BAMLH0A0HYM2` truncation → left a
dangling spec + no archive plan) proved that retiring a feed/provider
*without a process* leaves half-retired state: a HealSpec that no-ops
forever, a FeedProfile monitoring a dead feed, an audit check for data
that no longer exists. **Retirement is 3-way-atomic or it is wrong.**

## 1. Decide the disposition

- [ ] **Provider swap** (feed survives, different provider): this is a
      CUTOVER, not a retire — the outgoing provider becomes `FALLBACK`
      (parity-verified) or `RETIRED` *after* the replacement is
      `ACTIVE`. The feed's FeedProfile/HealSpec/audit stay.
- [ ] **Feed retirement** (the logical need itself is gone): the feed
      is fully offboarded — proceed through §2–§4.

## 2. Preserve provenance (CSV-first archive)

- [ ] The outgoing provider's historical pull is archived (the
      CSV-first `data/<source>_archive/*.csv.gz` pattern — the
      eco-archive discipline). A retired feed's history must remain
      recoverable; never delete the last archive.
- [ ] The final archive row count is recorded in the binding
      `evidence` (so a future audit can prove what was retired, when,
      and how big).

## 3. 3-WAY-ATOMIC retirement (the enforced invariant)

A fully-retired feed (no non-`RETIRED` binding) **must not** leave any
of these behind. All in the **same change**:

- [ ] **ProviderBinding** — every binding for the feed set to
      `RETIRED` (registry SoT reflects reality).
- [ ] **FeedProfile** — the feed's entry removed from
      `tpcore.feeds.FEED_PROFILES` (stop monitoring a dead feed; a
      stale freshness red on a retired feed is noise).
- [ ] **HealSpec** — the feed's `HealSpec`(s) removed/repointed in
      `tpcore.selfheal.registry` (a heal spec for a feed that no
      longer exists is the fake-healable class by construction). The
      HealSpec registry-coverage test (`HEAL_SPECS == suite.
      KNOWN_CHECK_NAMES`) forces the matching validation check to be
      removed too.
- [ ] **Audit check** — the feed's `audit_data_pipeline` check
      removed (the "audit tracks current reality, not a frozen
      snapshot" rule).
- [ ] `test_provider_lifecycle_consistency.py` is **green** — it
      mechanically asserts the 3-way invariant: a live binding ⇒ feed
      present in FeedProfile *and* HealSpec; a fully-retired feed ⇒
      absent from both. You cannot half-retire and pass CI.

## 4. Verify + sign-off

- [ ] Validation suite + `audit_data_pipeline` green post-retirement
      (no orphan red for the removed feed).
- [ ] No engine still reads the retired feed (grep `ENGINE_TABLES` /
      `HealSpec.source` consumers — a retired feed an engine depends
      on is a CUTOVER-to-fallback, not a retire).
- [ ] **Operator-confirmed** (spec non-goal: retirement is structural,
      never an automatic side-effect — same rule as engine archival).
