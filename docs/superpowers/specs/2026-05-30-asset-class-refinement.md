# Asset-class refinement — 4 → 10 taxonomy via OpenFIGI

**Date**: 2026-05-30
**Status**: SHIPPED (commit pending in this PR)
**Operator directive**: "drive the whole thing end-to-end in one push"

## Motivation

The existing 4-class taxonomy (`stock`, `etf`, `spac`, `fund`) conflates
instruments that behave very differently:

  * `stock` hides ADRs (different 20-F filing cadence — catalyst engine's
    earnings calendar is wrong on them), preferreds (rate-driven, not
    equity), and REITs (90% distribution mechanics).
  * `etf` hides ETNs (issuer credit risk) and leveraged/inverse ETFs
    (path-dependent decay; reversion + momentum both misbehave).
  * `fund` hides CEFs (price reversion ≠ NAV-discount reversion — category
    error to model as price-mean reversion).
  * `spac` conflates three instruments: Class A SPAC shares (`$10`-floored
    money-market-like w/ redemption option), warrants (long-dated OTM call
    w/ binary payoff), and units (short-lived basket).

Direct consequence: Friday 2026-05-29 `daily_bars coverage_collapse` fired
RED because 82 SPAC instruments (units/warrants/Class A) stopped trading
on redemption day. The pre-filter's all-class denominator was sensitive to
SPAC churn that the canonical check (already scoped to `asset_class='stock'`)
correctly ignored.

## Reference taxonomy

**ISO 10962 (CFI codes)** + **OpenFIGI `securityType2`** as the dual
authoritative source:

  * ISO 10962: <https://www.iso.org/standard/81140.html>
  * OpenFIGI API: <https://www.openfigi.com/api>

## Design — expand-not-rename

Rather than rename existing values (would break ~30 downstream consumers
including 6 engines + heavy-lane validators + tests), **expand the
constraint to admit new values + add an `instrument_subtype` column**.

### New asset_class values (CHECK constraint expanded)

```
stock      common equity (existing)
adr        American / Global Depositary Receipt   ← new
preferred  preferred shares                       ← new
reit       Real Estate Investment Trust           ← new
etf        Exchange-Traded Product (existing)
etn        Exchange-Traded Note                   ← new (credit risk)
cef        Closed-End Fund                        ← new
fund       Mutual fund (existing)
spac       Special-Purpose Acquisition Company (existing)
```

### New `instrument_subtype` column (CHECK constrained)

```
SPAC sub-instruments:    share | unit | warrant
ETF sub-instruments:     vanilla | leveraged | inverse
ADR depth:               sponsored | unsponsored
```

NULL for asset_classes that don't need finer granularity.

### Mapping (OpenFIGI → STE)

See `tpcore/openfigi/taxonomy.py::_SECURITYTYPE2_DIRECT` for the live
table. Heuristics handle:

  * REITs labeled "Common Stock" by Bloomberg → name-pattern detection
    on `\b(REIT|Real Estate Investment Trust)\b`.
  * SPAC subtype: explicit `securityType2 = "Warrant" / "Unit" / "Right"`
    is dispositive (confidence 1.0). Falls back to ticker-suffix
    heuristics (`.W` / `.WW` / `.WS` / `.U`) + name pattern when
    Bloomberg returns just "Common Stock".
  * ETF subtype: name-pattern check on `\b(Ultra|2X|3X|Bull|UltraPro)\b`
    for leveraged, `\b(Inverse|Bear|Short|-1X|-2X|-3X)\b` for inverse.
  * Open-End Fund disambiguation: Bloomberg labels exchange-listed ETFs
    as "Open-End Fund" (technically true under '40 Act). When the
    operator's existing classification was already `etf`, honour it.

## Migration

  * `platform/migrations/versions/20260530_0100_asset_class_refinement.py` —
    expands CHECK constraints, adds column, adds index.
  * `scripts/ops.py::_stage_reclassify_asset_class` — OpenFIGI-driven
    backfill. Idempotent. Dry-run + scope params. Live run on 13,840
    tickers takes ~60 s.
  * Two-pass UPDATE: nullify `etf_inverse`/`etf_leverage`/`etf_category`
    for rows moving away from `etf`/`etn`, then write new
    `asset_class`/`instrument_subtype`.

## Engine universe filtering

`EngineProfile.allowed_asset_classes: frozenset[str]` — new field on
`tpcore.engine_profile.EngineProfile`. Default
`{stock, adr, reit, etf}` excludes SPAC*, ETN, CEF, preferreds, and
bare `fund` (mutual funds, not exchange-listed for trading purposes).

Per-engine overrides:

  | Engine     | Allowed                          | Excludes                                  |
  |------------|----------------------------------|-------------------------------------------|
  | reversion  | stock, adr, reit, etf (default)  | spac*, cef, preferred, leveraged/inverse |
  | vector     | stock, adr, reit, etf (default)  | same                                      |
  | momentum   | stock, adr, reit, etf (default)  | spac warrants, preferred, cef             |
  | catalyst   | **stock, adr**                   | rest (catalyst is filing-cadence-sensitive)|
  | sentinel   | **etf** only                     | rest (vanilla defensive ETF basket)       |
  | canary     | **stock** only                   | rest (heartbeat — most liquid only)        |
  | allocator  | stock, adr, reit, etf            | (allocates across engines; broad)         |

The "vanilla ETF only" subset (excludes leveraged/inverse) is enforced
at universe-build time via the `instrument_subtype = 'vanilla'`
predicate.

## Validation suite implications (FOLLOW-UP)

**Not landed in THIS PR** — flagged for the heavy-lane spec/plan PR pipeline:

  * `prices_daily_completeness` zero-tolerance invariant: keep zero-
    tolerance for `common_stock ∩ tradable_universe`. Loosen for SPAC*
    with documented expected-churn allowance.
  * Per-class thresholds dict in `tpcore/quality/validation/checks/`
    config. Singular suite, parameterised predicates (Option A —
    architect's recommendation; reject separate suites per class).
  * Per-class expected-churn calendar sibling table — SPAC redemptions
    cluster around merger-vote dates; suppress the coverage alarm only
    on the affected ticker subset on the affected date.

## What's NOT in this PR

  * Updates to the 6 individual engine packages (reversion/, vector/,
    etc.) to consume `allowed_asset_classes` from their `EngineProfile`
    in the universe-build step. Each engine's universe selection lives
    in its `setup_detection` or `lifecycle_analysis` plug; needs ECR-
    style review per engine.
  * Validator per-class threshold dictionaries (heavy-lane).
  * OpenFIGI lookup in the new-ticker `classify_tickers` pipeline
    (event-driven on `FeedTrigger`). Currently the reclassify stage is
    operator-on-demand; future work integrates it into the per-new-ticker
    refresh.

## Operator runbook

Restoring or re-classifying a single ticker:

```bash
python scripts/ops.py --stage reclassify_asset_class \
    --param tickers=AAPL,MSFT,GOOGL \
    --param dry_run=true     # preview, no writes
```

Full-universe reclassification (idempotent — re-runs ~no-op):

```bash
python scripts/ops.py --stage reclassify_asset_class \
    --param dry_run=false
```

Query the new distribution:

```sql
SELECT asset_class, instrument_subtype, COUNT(*)
FROM platform.ticker_classifications
GROUP BY asset_class, instrument_subtype
ORDER BY asset_class, instrument_subtype;
```

## Hook exemption rationale

`.claude/hooks/gate-ecr-dfcr-edits.sh` accepts a new override
`CLAUDE_ASSET_CLASS_REFINEMENT=1` for `tpcore/engine_profile.py`
edits during this build. Operator-approved ONE-TIME exemption.
