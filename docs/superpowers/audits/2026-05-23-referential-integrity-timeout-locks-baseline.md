# Phase 0 Audit — Statement Timeout, Locks, Constraints Baseline

**Date:** 2026-05-23
**Plan:** v2 §2.4-2.6 / spec §9.1, §9.2, §9.3

## Statement timeout / lock timeout

| Setting | Value | Source | Context |
|---|---|---|---|
| `statement_timeout` | `120000` | `configuration file` | `user` |
| `lock_timeout` | `0` | `default` | `user` |
| `idle_in_transaction_session_timeout` | `0` | `default` | `user` |

**Current statement_timeout: 120s (2.0min)**

## Required budgets (per v2 plan §2.4)

| Phase | Required timeout | Current ceiling | Action |
|---|---|---|---|
| Phase 2 bulk NOT VALID (15 sub-second ops) | 5min | 2min | **Insufficient** but acceptable — set `SET LOCAL statement_timeout = '5min'` in the migration; if that's vetoed by role-level cap, raise via Supabase dashboard. |
| Phase 4 prices_daily VALIDATE (20.6M-row scan) | 30min | 2min | **INSUFFICIENT — MUST raise via Supabase dashboard before Phase 4 PR lands.** |

## Role config

- `postgres`: `rolconfig` = `['search_path="\\$user", public, extensions']`

Role-level statement_timeout override is NOT set on this connection's role; the 2min ceiling comes from the cluster default (postgresql.conf). **Supabase dashboard override path** is the way to raise this — verify operator has access to the project settings.

## Existing FK constraints (pg_constraint baseline)

**Count:** 0 (pre-Phase-2 expectation: 0 — matches)

_No FK constraints in `platform.*` schema. Confirms v2 spec assumption._

## Lock snapshot (current moment)

**Count:** 0 locks held on in-scope tables

_Note: this snapshot was captured during quiet hours (Saturday morning Manila). For the proper Phase 2 / Phase 4 baseline, re-snapshot during the nightly ingest window (UTC 21:30 = local 05:30 Manila) per v2 plan §2.5._
