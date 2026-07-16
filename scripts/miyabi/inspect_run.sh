#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 SHARED_ROOT [DB_PATH]" >&2
  exit 2
fi

SHARED_ROOT="$1"
DB_PATH="${2:-}"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"

if [[ -n "$DB_PATH" ]]; then
  exec "$PYTHON_BIN" -m fs_diloco.analysis "$SHARED_ROOT" --db "$DB_PATH"
fi
exec "$PYTHON_BIN" -m fs_diloco.analysis "$SHARED_ROOT"
