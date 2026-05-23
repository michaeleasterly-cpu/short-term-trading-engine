# CSV archive retention policy

`data/<source>_<feed>_archive/` directories are the **rebuild substrate** per the R3 substrate design (PR #235). Each successful vendor ingest writes a gzipped CSV here. They're NOT a data lake — they're per-feed rebuild input.

## Retention policy

**Keep latest 3 snapshots per archive.** Older snapshots are deleted.

Why 3:
- Latest = canonical (matches live DB)
- Previous = used by shrinkage detection (compare current count vs prior)
- 1 ago = buffer so the next compare cycle still has a baseline if previous gets rotated mid-run

More than 3 = wasted disk / cognitive overhead. Fewer than 3 = shrinkage detector can lose its baseline mid-cycle.

## Exceptions

| Archive | Special handling |
|---|---|
| `data/sec_backfill/` | One-shot historical bulk (130 quarterly SEC ZIPs from 2017-2026). NEVER rotate; this IS the SEC archive. |
| `data/tradier_export/tradier_bars_full.csv` | 1.1GB one-shot historical export (220K bars 1994-2019). NEVER rotate. |
| `data/*_backfill/` (non-archive suffix) | One-shot backfill data (fmp_backfill, alpaca_backfill, corp_actions_backfill, fred_macro_backfill, hy_spread_recovery). NEVER rotate. |
| `data/sentinel_activation_probe/` | Research artifact. NEVER rotate. |

## How to run cleanup manually

```bash
cd /Users/michael/short-term-trading-engine
for d in data/*_archive/; do
  files=$(ls -t "$d"*.csv.gz 2>/dev/null)
  count=$(echo "$files" | grep -c .)
  if [ "$count" -gt 3 ]; then
    to_del=$(echo "$files" | tail -n +4)
    echo "$to_del" | xargs rm -f
    echo "$d: cleaned to 3 files"
  fi
done
find data -name ".DS_Store" -delete
```

## Automation (future)

This cleanup SHOULD be wired into a daily ops stage (`csv_archive_cleanup`) — currently it's manual. Add to Task #9 follow-up: hook it into `run_data_operations.sh` after the ingest stages complete each night.

## Verification queries

```bash
# Archive sizes after cleanup
du -sh data/*_archive/ | sort -k2

# File counts per archive
for d in data/*_archive/; do
  count=$(ls "$d" 2>/dev/null | wc -l | tr -d ' ')
  echo "$count files  $d"
done
```

## Last cleanup run

- **2026-05-23:** Cleaned 83 → 27 archive files (56 stale snapshots removed). Total archive size dropped ~25%. `.DS_Store` macOS noise removed.
- Before cleanup, `alpaca_daily_bars_archive/` had 21 files (most extreme accumulation — runs every few minutes during heavy days).
- `fmp_earnings_events_archive/` had two <300B snapshots from earlier today indicating an empty-result snapshot pattern in the producer worth investigating separately.
