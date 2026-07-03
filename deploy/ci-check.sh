#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_sha=""
head_sha="HEAD"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-sha)
      base_sha="${2:-}"
      shift 2
      ;;
    --head-sha)
      head_sha="${2:-}"
      shift 2
      ;;
    -h|--help)
      printf 'Usage: %s [--base-sha SHA] [--head-sha SHA]\n' "$0"
      exit 0
      ;;
    *)
      printf 'ERROR: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

args=(--head-sha "$head_sha")
if [ -n "$base_sha" ]; then
  args+=(--base-sha "$base_sha")
fi
python3 "$ROOT/deploy/ci/check.py" "${args[@]}"

mapfile -t source_files < <(git -C "$ROOT" ls-files --cached --others --exclude-standard)
mapfile -t python_files < <(
  for path in "${source_files[@]}"; do
    if [[ "$path" == *.py ]] || head -n 1 "$ROOT/$path" 2>/dev/null | grep -Eq '^#!/usr/bin/env python3$'; then
      printf '%s\n' "$path"
    fi
  done
)
python3 - "$ROOT" "${python_files[@]}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
for rel in sys.argv[2:]:
    path = root / rel
    compile(path.read_bytes(), str(path), "exec")
print(f"Python syntax OK ({len(sys.argv) - 2} files)")
PY

mapfile -t node_files < <(printf '%s\n' "${source_files[@]}" | grep -E '\.(js|mjs)$' || true)
for path in "${node_files[@]}"; do
  node --check "$ROOT/$path"
done
printf 'Node syntax OK (%d files)\n' "${#node_files[@]}"

mapfile -t shell_files < <(
  for path in "${source_files[@]}"; do
    if [[ "$path" == *.sh ]] || head -n 1 "$ROOT/$path" 2>/dev/null | grep -Eq '^#!/usr/bin/env bash$'; then
      printf '%s\n' "$path"
    fi
  done
)
for path in "${shell_files[@]}"; do
  bash -n "$ROOT/$path"
done
printf 'Shell syntax OK (%d files)\n' "${#shell_files[@]}"

pycache="$(mktemp -d)"
trap 'rm -rf "$pycache"' EXIT
mapfile -t python_tests < <(printf '%s\n' "${source_files[@]}" | awk -F/ '$NF ~ /^test_.*\.py$/ || $NF ~ /_test\.py$/')
for path in "${python_tests[@]}"; do
  printf 'Running %s\n' "$path"
  PYTHONPATH="$ROOT/workspace/lib:$ROOT/workspace/account-link" \
    PYTHONPYCACHEPREFIX="$pycache" \
    python3 "$ROOT/$path"
done

mapfile -t node_tests < <(printf '%s\n' "${source_files[@]}" | grep -E '\.test\.(js|mjs)$' || true)
if [ "${#node_tests[@]}" -gt 0 ]; then
  node --test "${node_tests[@]/#/$ROOT/}"
fi

"$ROOT/workspace/bin/openclaw-cap" --help >/dev/null
printf 'Portable CI validation OK\n'
