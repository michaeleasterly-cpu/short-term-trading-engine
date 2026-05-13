#!/usr/bin/env bash
# Show Alpaca paper account equity + current positions. Use after market
# open (~6:30 AM PT) to see what filled from the monthly rebalance.
set -uo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck disable=SC1091
source .env
set +a

.venv/bin/python -c "
import asyncio
from tpcore.alpaca import AlpacaPaperBrokerAdapter

async def main():
    b = AlpacaPaperBrokerAdapter()
    a = await b.get_account()
    p = await b.get_positions()
    print(f'equity=\${a.equity}  cash=\${getattr(a, \"cash\", \"n/a\")}  positions={len(p)}')
    for x in sorted(p, key=lambda r: r.symbol)[:60]:
        print(f'  {x.symbol:<6} qty={int(x.qty):>5} mv=\${x.market_value or 0:>10}')
    if len(p) > 60:
        print(f'  … ({len(p) - 60} more)')

asyncio.run(main())
"
