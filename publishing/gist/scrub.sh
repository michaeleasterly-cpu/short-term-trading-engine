#!/usr/bin/env bash
# Pre-publish PII / secret scrub for the gist staging directory.
# Run from the repo root: bash publishing/gist/scrub.sh
#
# Exits non-zero if ANY pattern hits — the gist must not be published
# until every hit is redacted or the file is removed from staging.

set -u
STAGED="$(dirname "$0")/staged"

if [ ! -d "$STAGED" ]; then
    echo "FATAL: staging dir not found: $STAGED" >&2
    exit 2
fi

echo "=== scrub.sh: scanning $STAGED ==="
HITS=0

echo
echo "--- secrets pattern (API keys, AWS keys, private keys, DSNs, privaterelay email) ---"
if grep -rEn "(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN.*PRIVATE KEY|postgresql?://[^@[:space:]]+:[^@[:space:]]+@|@privaterelay\.appleid)" "$STAGED"; then
    HITS=$((HITS+1))
fi

echo
echo "--- operator identity pattern (gh handle, apple-relay localpart) ---"
if grep -rEn "(michaeleasterly|kb5dprpghr)" "$STAGED"; then
    HITS=$((HITS+1))
fi

echo
if [ "$HITS" -eq 0 ]; then
    echo "PASS: no scrub hits — staging is publish-eligible."
    exit 0
else
    echo "FAIL: $HITS pattern(s) hit. Redact or remove the offending files before publishing." >&2
    exit 1
fi
