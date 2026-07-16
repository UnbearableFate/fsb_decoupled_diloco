#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 SQLITE_DB DEST_DB" >&2
  exit 2
fi

sqlite3 "$1" ".backup '$2'"
ls -lh "$2"
