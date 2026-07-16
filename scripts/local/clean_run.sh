#!/bin/bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
RUN_ROOT="${1:-$PROJECT_ROOT/runs/fs_diloco}"

if [[ "$RUN_ROOT" != "$PROJECT_ROOT"/runs/fs_diloco* ]]; then
  echo "Refusing to remove path outside $PROJECT_ROOT/runs/fs_diloco: $RUN_ROOT" >&2
  exit 2
fi

rm -rf "$RUN_ROOT"
