#!/usr/bin/env bash
# Quick verification: count the momentum orders submitted to Alpaca paper,
# and show their statuses. Useful right after a kickoff to confirm the full
# batch landed at the broker before market open.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

.venv/bin/python -c "
import asyncio
from collections import Counter
from tpcore.alpaca import AlpacaPaperBrokerAdapter

async def main():
    b = AlpacaPaperBrokerAdapter()
    orders = await b.list_recent_orders(limit=500)
    mom = [o for o in orders if (o.client_order_id or '').startswith('mo_')]
    statuses = Counter((o.status.value if hasattr(o.status, 'value') else str(o.status)).lower() for o in mom)
    print(f'momentum orders at Alpaca: {len(mom)}')
    for status, n in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f'  {status:<20} {n}')

asyncio.run(main())
"
