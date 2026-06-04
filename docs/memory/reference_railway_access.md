---
name: reference_railway_access
description: How to reach Railway — use RAILWAY_API_TOKEN + RAILWAY_PROJECT_ID from .env; the railway CLI/MCP OAuth session is dead. Do NOT ask the operator to log in.
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

**Railway access (do not re-learn this):** the operator's `.env` carries `RAILWAY_API_TOKEN` + `RAILWAY_PROJECT_ID` (workspace `PacketVoidLabs`, project `TCP`, project id `4a0e14ee-5f82-4416-b6d9-04526b1d3cf1`). The stored `railway` CLI / MCP **OAuth session is dead** (`invalid_grant`) — do NOT prompt the operator for `railway login`. Instead load the token from `.env` (Python `load_dotenv` or bash `set -a; source .env; set +a`) and run the CLI as `RAILWAY_API_TOKEN="$RAILWAY_API_TOKEN" railway <cmd>`.

**Token works — use the GraphQL API, not the CLI (2026-06-04):** the token successfully reads AND mutates via the **GraphQL API** (`https://backboard.railway.com/graphql/v2`, `Authorization: Bearer $RAILWAY_API_TOKEN`) — confirmed by a successful `serviceInstanceUpdate`. The `railway` CLI's `whoami`/`list` fail because the CLI uses the dead OAuth session, NOT the token — `whoami` is account-level and irrelevant. So: drive Railway via Python httpx GraphQL (the pattern `ops/apply_railway_service_config.py` uses), not the CLI. IDs: env production `58653d3b-ff14-4fef-97fa-370e96b0391e`; service data-operations `d39b7e55-5d77-47cd-bc2f-c4c2615832ce`.

**PAUSED STATE (2026-06-04, for the data-layer rebuild):** the `data-operations` cron is CLEARED (`cronSchedule=None`) so the daily ticker+macro ingest won't fire and overwrite the rebuild. **RESTORE before resuming normal ops:** `serviceInstanceUpdate(serviceId=d39b7e55…, environmentId=58653d3b…, input={cronSchedule:"30 21 * * MON-FRI"})`. The persistent services (engine/lane/trade-monitor) still run; when the rebuild EXECUTES, also pause lane-service + trade-monitor (reactive substrate writers).

**Architecture (railway.json, 4 services):** `data-operations` (the ONE cron, `30 21 * * MON-FRI`, runs `run_data_operations.sh` = `ops.py --update` = ALL ingest, **ticker AND macro in one pass**), `engine-service`, `lane-service`, `trade-monitor`. There is NO separate macro-only ingest — pausing `data-operations` pauses macro too. Memory `project_railway_hobby_tier` says Railway was paused 2026-05-12 (verify live state before assuming).

See [[reference_data_layer_index]], [[project_railway_hobby_tier]], [[project_data_layer_rebuild_arc]].
