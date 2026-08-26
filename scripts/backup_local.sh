#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/backend/data/tradingos.db"
DESTINATION="$ROOT/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -f "$SOURCE" ]]; then
  echo "No local SQLite database exists yet at $SOURCE"
  exit 1
fi

mkdir -p "$DESTINATION"
TARGET="$DESTINATION/tradingos-$STAMP.db"
python3 - "$SOURCE" "$TARGET" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
    source_db.backup(target_db)
PY
sha256sum "$TARGET" > "$TARGET.sha256"
echo "Created $TARGET and its SHA-256 digest."
