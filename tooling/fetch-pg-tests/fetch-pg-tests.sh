#!/bin/bash
# Fetches upstream PostgreSQL regression test suites for a specified branch / tag
# Usage: ./fetch-pg-tests.sh <branch-or-tag> <dest-dir>
set -euo pipefail

REF=${1:-"REL_18_STABLE"}
DEST=${2:-"suites/postgresql-v18"}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Fetching PostgreSQL $REF regression sql/expected..."
git clone --depth 1 --filter=blob:none --sparse "https://github.com/postgres/postgres.git" "$TMP/pg" -b "$REF"
git -C "$TMP/pg" sparse-checkout set src/test/regress/sql src/test/regress/expected

mkdir -p "$DEST"
echo "Splitting statements into $DEST/statements.jsonl..."
python3 "$(dirname "$0")/split_pg_regress.py" "$TMP/pg" "$DEST/statements.jsonl"
echo "Done."
