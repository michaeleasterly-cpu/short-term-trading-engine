#!/usr/bin/env bash
# Full-historical-backfill workflow. Use after major data-quality events
# (large cleanup, source switch, new universe expansion) — NOT for the
# daily cadence. Daily uses ``scripts/run_post_close.sh``.
#
# Implements the operator's canonical pattern:
#   DOWNLOAD → UPLOAD → VERIFY → FIX → COMPRESS
#
# All three sources (Alpaca bars, FMP fundamentals, Alpaca corp actions)
# follow the two-phase CSV-first pattern. Each Phase 2 loader auto-
# compresses its source CSV on successful upsert.
#
# Long-running: 30-60 min depending on universe size and FMP quota.
#
# Usage:
#   scripts/run_full_backfill.sh                 # tier ≤ 2 universe
#   scripts/run_full_backfill.sh --tickers AAPL,MSFT  # focused
set -uo pipefail
cd "$(dirname "$0")/.."

EXTRA_ARGS="$@"

echo "════════════════════════════════════════════════════════════════════════"
echo "  FULL BACKFILL — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  $EXTRA_ARGS"
echo "════════════════════════════════════════════════════════════════════════"

# ── DOWNLOAD ────────────────────────────────────────────────────────────────
echo ""
echo "▶ DOWNLOAD 1/3 — Alpaca bars → CSV"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_backfill_alpaca_csv.sh $EXTRA_ARGS || { echo "✗ alpaca bars download failed"; exit 1; }

echo ""
echo "▶ DOWNLOAD 2/3 — FMP fundamentals → CSV"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_backfill_fmp_csv.sh $EXTRA_ARGS || { echo "✗ fmp fundamentals download failed"; exit 1; }

echo ""
echo "▶ DOWNLOAD 3/3 — Alpaca corp actions → CSV"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_backfill_corp_actions_csv.sh $EXTRA_ARGS || { echo "✗ corp actions download failed"; exit 1; }

# ── UPLOAD ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ UPLOAD 1/3 — alpaca CSV → prices_daily (auto-compresses on success)"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_load_alpaca_csv.sh || { echo "✗ alpaca bars load failed"; exit 1; }

echo ""
echo "▶ UPLOAD 2/3 — fmp CSV → fundamentals_quarterly"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_load_fmp_csv.sh || { echo "✗ fmp load failed"; exit 1; }

echo ""
echo "▶ UPLOAD 3/3 — corp actions CSV → corporate_actions"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_load_corp_actions_csv.sh || { echo "✗ corp actions load failed"; exit 1; }

# ── VERIFY ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ VERIFY 1/2 — validation suite (6 checks)"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_stage.sh data_validation || { echo "✗ validation suite red — investigate before trading"; exit 1; }

echo ""
echo "▶ VERIFY 2/2 — cross-table audit"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_audit_all_tables.sh || { echo "✗ audit red"; exit 1; }

# ── FIX / COMPRESS ─────────────────────────────────────────────────────────
# FIX is a no-op when everything is green; otherwise the verify steps
# above already exited non-zero.
echo ""
echo "▶ COMPRESS — any CSVs not yet compressed by their loader"
echo "────────────────────────────────────────────────────────────────────────"
scripts/run_compress_backfill_csvs.sh

echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "  FULL BACKFILL COMPLETE — every gate 🟢"
echo "════════════════════════════════════════════════════════════════════════"
