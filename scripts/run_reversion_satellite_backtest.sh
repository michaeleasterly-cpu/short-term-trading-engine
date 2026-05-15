#!/usr/bin/env bash
# Reversion satellite-classification backtest (2026-05-15).
# Runs reversion/backtest.py against the full backtest window with the
# winner-variant defaults (Z ≥ 3.0, HIGH earnings quality, T3+ universe)
# and captures the search-pipeline JSON to backtests/.
#
# Wrapped per the "wrap multi-flag commands in scripts/" rule.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Pull DATABASE_URL_IPV4 from .env (local Mac uses the IPv4 pooler URL).
# shellcheck disable=SC2046
export $(grep -E '^DATABASE_URL_IPV4=' .env | xargs)
export DATABASE_URL="${DATABASE_URL_IPV4:?DATABASE_URL_IPV4 missing from .env}"

OUT="backtests/reversion_satellite_backtest.json"
mkdir -p backtests

echo "→ running reversion backtest 2018-01-01 → 2025-12-31 (Z≥3.0, HIGH quality) …"
"$REPO_ROOT/.venv/bin/python" reversion/backtest.py \
    --start 2018-01-01 \
    --end 2025-12-31 \
    --z-threshold 3.0 \
    --earnings-quality HIGH \
    --json \
    --skip-statistical-validation \
    > "$OUT"

if [[ ! -s "$OUT" ]]; then
    echo "ERROR: $OUT is empty" >&2
    exit 1
fi

echo "→ wrote $OUT ($(wc -c < "$OUT") bytes)"
echo "→ summary:"
"$REPO_ROOT/.venv/bin/python" - <<'PY'
import json
with open("backtests/reversion_satellite_backtest.json") as f:
    d = json.load(f)
print(f"  trades              : {d.get('trades', '?')}")
print(f"  sharpe              : {d.get('sharpe', '?'):+.3f}")
print(f"  profit_factor       : {d.get('profit_factor', '?'):.3f}")
print(f"  max_drawdown        : {d.get('max_drawdown', '?'):+.3%}" if isinstance(d.get('max_drawdown'), (int,float)) else f"  max_drawdown        : {d.get('max_drawdown', '?')}")
print(f"  credibility_score   : {d.get('credibility_score', '?')}")
print(f"  dsr                 : {d.get('dsr', '?')}")
print(f"  passed_gate         : {d.get('passed_gate', '?')}")
PY
