---
name: supabase-pro-tier
description: "Supabase project short-term-engine (qrdtlxmnctwfxbnlcqxj) on Pro plan ($25/mo, 8GB disk) as of 2026-05-11; upgraded after free-tier 500MB read-only lock"
metadata:
  node_type: memory
  type: project
  originSessionId: 6626da25-0752-45ca-99c0-beeb2f8af7bb
---
Supabase project `short-term-engine` (ref `qrdtlxmnctwfxbnlcqxj`, region us-east-1) is on **Pro plan — $25/month**, upgraded 2026-05-11. Disk capacity is now **8 GB** (was 500 MB on free).

**Why:** `platform.prices_daily` grew to ~578 MB (4.45M rows) and pushed cluster size to 625 MB, tripping Supabase's free-tier 500MB read-only lock. Upgrading to Pro is required for >500MB and was the chosen path over deleting historical bars.

**How to apply:**
- Treat the 500MB → 8GB headroom as the working budget. `prices_daily` dominates (~92% of current usage); any new high-cardinality time-series table should be sized against that headroom before adding.
- Pro plan adds **auto-scaling disks** (expand at 90% util, +50% jumps capped at +200GB, max 4 modifications per 24h). The project will not silently re-lock once a single table grows; it'll grow the disk first.
- The fixed monthly platform total is **$52/mo** — see [[railway-hobby-tier]] for the full breakdown.
- If Supabase ever goes read-only again on Pro, it means 95% disk + auto-scale quota exhausted. Different fix path (free space or wait for the 24h window) — do not assume "upgrade plan" is the answer.
