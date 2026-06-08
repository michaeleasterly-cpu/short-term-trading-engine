# Backlog spec — Liquidity / positioning / concentration gauges (2026-06-08)

> Operator-provided task spec, parked to backlog 2026-06-08 ("you can backlog some of it"). NOT started. Pick up after the data-foundation ingest completion. Branch when built: `feat/liquidity-positioning-gauges`.

## Goal

Add a flow/liquidity/positioning dimension to BOTH surfaces — (1) render new gauges on the self-fetching `/market` page, and (2) wire the new series into the EXISTING daily macro collector that persists to the macro table. **Do not build a parallel collection job.** Current dashboard is fundamentals+sentiment only (CAPE, Buffett, Sahm, claims, yield curve, credit spreads, VIX, breadth, AAII, F&G).

## Part 1 — page gauges

New section **"Liquidity & positioning"** between "Breadth" and "Recession watch". Match existing card styling, per-card "as of" date, and the "public snapshot, not advice" framing.

| id | priority | source | key detail |
|---|---|---|---|
| **net_liquidity** | **highest** | FRED | `(WALCL/1000) − WTREGEN − RRPONTSYD` in $bn. WALCL is **millions**, WTREGEN/RRPONTSYD **billions** — assert units at fetch, normalize WALCL first. Display $T level + 13wk change; as_of = oldest of the 3 component dates. |
| **move_index** | high | QUOTE_FEED `^MOVE` | bond-market VIX. Bands <80 calm / 80–120 watch / >120 stressed. If `^MOVE` unavailable on feed, flag — don't fabricate. |
| **vvix** | medium | QUOTE_FEED `^VVIX` | vol-of-vol; companion to VIX card. |
| **index_concentration** | high | HOLDINGS | top-10 % of S&P 500 mkt cap. Sources: SSGA SPY / iShares IVV holdings CSV / slickcharts. Fallback: Mag-7 mktcap / S&P total from quote feed, **labeled a proxy**. Band >35% extreme. |
| **passive_flows_4wk** | medium | ICI | weekly est. LT equity fund+ETF flows. No FRED series; ICI not cleanly machine-readable → scheduled weekly fetch/parse OR manual-entry scaffold, **weekly cadence**. Never fabricate. |
| **private_credit_note** | low | none_clean | informational caveat card (blind spot: risk migrated to private credit that doesn't mark-to-market). Optional proxy: a public BDC discount-to-NAV, labeled proxy. |

## Part 2 — collector integration (existing job only)

Find the existing collector/pipeline writing to the macro table (its series registry/config). If config-driven, append entries; if hardcoded, add fetchers in the existing pattern. Reuse shared fetch modules across page + collector — don't duplicate fetch logic. Series to add: `WALCL`, `WTREGEN`, `RRPONTSYD` (FRED) · `net_liquidity` (COMPUTED, from the 3 FRED rows) · `move_index` `^MOVE`, `vvix` `^VVIX` (QUOTE_FEED) · `index_concentration` (HOLDINGS) · `passive_flows_4wk` (ICI, weekly).

## Macro table — FIXED schema, do not alter columns

PK `(source, series_id, observed_date, realtime_start)`. Cols: `source` (FRED|QUOTE_FEED|ICI|HOLDINGS|COMPUTED), `series_id`, `observed_date`, `value_num` (numerics), `value_text` (categoricals only — never a number), `realtime_start`/`realtime_end` (FRED: vintage verbatim; non-vintage: = recorded_at), `recorded_at`.

Write rules: upsert on full PK (update value_num/value_text/realtime_end/recorded_at on conflict). FRED → store returned realtime_start/end verbatim, don't synthesize. Non-vintage → realtime_start=realtime_end=recorded_at (comment it). `net_liquidity` stored as COMPUTED row AND its 3 FRED components as FRED rows (reproducible from the table alone). Fetch failure → value_num=null (gap stays visible), log out of band; **no status column, don't invent one**.

## Constraints + acceptance

FRED for slow macro, live quote feed for fast market series. Both surfaces include all 6 new indicators. Never fabricate — flag instead. Assert/normalize units (WALCL!). Keep tone. Tests for net_liquidity math + upsert idempotency (no dup-key on re-run).

## Suggested tiering when picked up (the "backlog some of it")

- **Clean / do-first:** net_liquidity (FRED, deterministic), move_index + vvix (quote feed, same path as VIX) — these are the high-value automatable core.
- **Backlog-harder (no clean automatable source):** index_concentration (holdings CSV reliability), passive_flows_4wk (ICI not machine-readable — manual/scaffold), private_credit_note (no source, caveat card only).

## Cross-links / cautions

- `/market` was rebuilt self-fetching + DB-independent — the page gauges fetch live; the collector persists separately. Honor both surfaces.
- macro_data scope-leak caution (TODO.md "macro_data scope leak", 2026-06-04) — don't write per-state/LWA noise.
- Pairs with the deferred macro feature-layer (tall→wide+normalized) and Task #8 (vendor-characteristics-in-profile — `^MOVE`/`^VVIX`/FRED rate-limits belong in the vendor profile).
