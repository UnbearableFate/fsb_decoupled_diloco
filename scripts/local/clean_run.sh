#!/bin/bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
DEFAULT_TARGET="$PROJECT_ROOT/runs/fs_diloco"

delete=false
keep_latest_global=false
target_dir=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [--delete] [--keep-latest-global] [TARGET_DIR]

Recursively find *.safetensors and *.db files below TARGET_DIR. Without
--delete, only print the files that would be removed. TARGET_DIR defaults to:
  $DEFAULT_TARGET

Options:
  --delete                 Remove the discovered files.
  --keep-latest-global     In each directory, preserve the highest-numbered
                           global_v<version>.safetensors file and remove all
                           other discovered files, including *.db files.
  -h, --help               Show this help message.

For safety, TARGET_DIR must be the project's runs directory or one of its
descendants.
EOF
}

while (($# > 0)); do
  case "$1" in
    --delete)
      delete=true
      ;;
    --keep-latest-global)
      keep_latest_global=true
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
case "$target_dir/" in
  "$runs_root/"*) ;;
  *)
    echo "Refusing to scan path outside $runs_root: $target_dir" >&2
    exit 2
    ;;
esac

files=()
while IFS= read -r -d '' file; do
  files+=("$file")
done < <(find "$target_dir" -type f \( -name '*.safetensors' -o -name '*.db' \) -print0)

if ((${#files[@]} == 0)); then
  echo "No safetensors or DB files found below $target_dir"
  exit 0
fi

declare -A latest_version_by_dir=()
declare -A latest_path_by_dir=()

normalize_version() {
  local version="$1"
  while [[ ${#version} -gt 1 && "$version" == 0* ]]; do
    version="${version#0}"
  done
  printf '%s' "$version"
}

version_is_greater() {
  local candidate="$1"
  local current="$2"

  if ((${#candidate} != ${#current})); then
    ((${#candidate} > ${#current}))
  else
    [[ "$candidate" > "$current" ]]
  fi
}

if $keep_latest_global; then
  for file in "${files[@]}"; do
    filename="${file##*/}"
    if [[ "$filename" =~ ^global_v([0-9]+)\.safetensors$ ]]; then
      directory="${file%/*}"
      version="$(normalize_version "${BASH_REMATCH[1]}")"
      current_version="${latest_version_by_dir[$directory]:-}"
      current_path="${latest_path_by_dir[$directory]:-}"

      if [[ -z "$current_version" ]] \
        || version_is_greater "$version" "$current_version" \
        || { [[ "$version" == "$current_version" ]] && [[ "$file" > "$current_path" ]]; }; then
        latest_version_by_dir["$directory"]="$version"
        latest_path_by_dir["$directory"]="$file"
      fi
    fi
  done
fi

removed=0
kept=0
for file in "${files[@]}"; do
  directory="${file%/*}"
  if $keep_latest_global && [[ "${latest_path_by_dir[$directory]:-}" == "$file" ]]; then
    printf 'KEEP   %s\n' "$file"
    ((kept += 1))
    continue
  fi

  if $delete; then
    rm -f -- "$file"
    printf 'DELETE %s\n' "$file"
  else
    printf 'WOULD DELETE %s\n' "$file"
  fi
  ((removed += 1))
done

if $delete; then
  printf 'Deleted %d file(s); kept %d latest global checkpoint(s).\n' \
    "$removed" "$kept"
else
  printf 'Dry run: would delete %d file(s); would keep %d latest global checkpoint(s).\n' \
    "$removed" "$kept"
  echo "Re-run with --delete to remove them."
fi
