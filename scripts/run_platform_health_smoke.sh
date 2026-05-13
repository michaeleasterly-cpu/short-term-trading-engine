#!/usr/bin/env bash
# Smoke-test the dashboard's platform-health fetcher against the live DB.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -c "
import asyncio, os, json, sys
sys.path.insert(0, '.')
from dashboard import _fetch_platform_health

async def main():
    h = await _fetch_platform_health()
    # Re-key non-serializable datetime/Decimal for printing.
    def coerce(o):
        try:
            return o.isoformat()
        except AttributeError:
            return str(o)
    print(json.dumps(h, indent=2, default=coerce))

asyncio.run(main())
"
