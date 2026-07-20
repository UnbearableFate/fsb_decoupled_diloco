#!/bin/bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
DEFAULT_TARGET="$PROJECT_ROOT/runs/fs_diloco"
KEEP_SUBSTRING="5000"

delete=false
target_dir=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [--delete] [TARGET_DIR]

Find immediate run directories below TARGET_DIR whose directory names do not
contain "$KEEP_SUBSTRING". Without --delete, only print the directories that
would be removed. TARGET_DIR defaults to:
  $DEFAULT_TARGET

Options:
  --delete    Recursively remove the matching run directories.
  -h, --help  Show this help message.

For safety, TARGET_DIR must be a strict descendant of the project's runs/
directory. Only immediate child directories are treated as runs.
EOF
}

while (($# > 0)); do
  case "$1" in
    --delete)
      delete=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      if (($# > 1)); then
        echo "Expected at most one TARGET_DIR" >&2
        usage >&2
        exit 2
      fi
      if (($# == 1)); then
        target_dir="$1"
      fi
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "$target_dir" ]]; then
        echo "Expected at most one TARGET_DIR" >&2
        usage >&2
        exit 2
      fi
      target_dir="$1"
      ;;
  esac
  shift
done

target_dir="${target_dir:-$DEFAULT_TARGET}"
runs_root="$PROJECT_ROOT/runs"

if [[ ! -d "$runs_root" ]]; then
  echo "Runs directory does not exist: $runs_root" >&2
  exit 2
fi
if [[ ! -d "$target_dir" ]]; then
  echo "Target directory does not exist: $target_dir" >&2
  exit 2
fi

runs_root="$(cd "$runs_root" && pwd -P)"
target_dir="$(cd "$target_dir" && pwd -P)"
if [[ "$target_dir" == "$runs_root" ]]; then
  echo "Refusing to treat the runs root itself as a run collection: $target_dir" >&2
  exit 2
fi
case "$target_dir/" in
  "$runs_root/"*) ;;
  *)
    echo "Refusing to scan path outside $runs_root: $target_dir" >&2
    exit 2
    ;;
esac

run_dirs=()
while IFS= read -r -d '' run_dir; do
  run_dirs+=("$run_dir")
done < <(find "$target_dir" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

if ((${#run_dirs[@]} == 0)); then
  echo "No run directories found directly below $target_dir"
  exit 0
fi

removed=0
kept=0
for run_dir in "${run_dirs[@]}"; do
  run_title="${run_dir##*/}"
  if [[ "$run_title" == *"$KEEP_SUBSTRING"* ]]; then
    printf 'KEEP         %s\n' "$run_dir"
    ((kept += 1))
    continue
  fi

  if $delete; then
    rm -rf -- "$run_dir"
    printf 'DELETE       %s\n' "$run_dir"
  else
    printf 'WOULD DELETE %s\n' "$run_dir"
  fi
  ((removed += 1))
done

if $delete; then
  printf 'Deleted %d run(s); kept %d run(s) whose titles contain "%s".\n' \
    "$removed" "$kept" "$KEEP_SUBSTRING"
else
  printf 'Dry run: would delete %d run(s); would keep %d run(s) whose titles contain "%s".\n' \
    "$removed" "$kept" "$KEEP_SUBSTRING"
  echo "Re-run with --delete to remove them."
fi
