---
name: reference_railway_access
description: How to reach Railway — use RAILWAY_API_TOKEN + RAILWAY_PROJECT_ID from .env; the railway CLI/MCP OAuth session is dead. Do NOT ask the operator to log in.
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

**Railway access (do not re-learn this):** the operator's `.env` carries `RAILWAY_API_TOKEN` + `RAILWAY_PROJECT_ID` (workspace `PacketVoidLabs`, project `TCP`, project id `4a0e14ee-5f82-4416-b6d9-04526b1d3cf1`). The stored `railway` CLI / MCP **OAuth session is dead** (`invalid_grant`) — do NOT prompt the operator for `railway login`. Instead load the token from `.env` (Python `load_dotenv` or bash `set -a; source .env; set +a`) and run the CLI as `RAILWAY_API_TOKEN="$RAILWAY_API_TOKEN" railway <cmd>`.

**Current token state (2026-06-04):** partially working — `railway status` resolves the project, but `railway whoami` / `railway list` return **Unauthorized**, and service/env commands need an explicit `--environment`. The token looks **stale or project-scoped/limited**; it may need regeneration before it can pause/control services. Verify with `RAILWAY_API_TOKEN=… railway status` first.

**Architecture (railway.json, 4 services):** `data-operations` (the ONE cron, `30 21 * * MON-FRI`, runs `run_data_operations.sh` = `ops.py --update` = ALL ingest, **ticker AND macro in one pass**), `engine-service`, `lane-service`, `trade-monitor`. There is NO separate macro-only ingest — pausing `data-operations` pauses macro too. Memory `project_railway_hobby_tier` says Railway was paused 2026-05-12 (verify live state before assuming).

See [[reference_data_layer_index]], [[project_railway_hobby_tier]], [[project_data_layer_rebuild_arc]].
