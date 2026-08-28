#!/usr/bin/env bash
# Run an accessibility Python command from the repository's shared environment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${A11Y_VENV:-$ROOT/.venv}"
PYTHON="$VENV/bin/python"
REQUIREMENTS="$ROOT/scripts/requirements-migrate.txt"
LOCK="$VENV/.a11y-deps-install.lock"

dependencies_ready() {
  "$PYTHON" -c 'import boto3, pikepdf, pypdf, pymupdf' >/dev/null 2>&1
}

if [[ ! -x "$PYTHON" ]]; then
  python3 -m venv "$VENV"
fi

if ! dependencies_ready; then
  acquired_lock=false
  for _ in {1..180}; do
    if mkdir "$LOCK" 2>/dev/null; then
      acquired_lock=true
      break
    fi
    if dependencies_ready; then
      break
    fi
    sleep 1
  done

  if [[ "$acquired_lock" == true ]]; then
    cleanup_lock() {
      rmdir "$LOCK" 2>/dev/null || true
    }
    trap cleanup_lock EXIT
    if ! dependencies_ready; then
      "$PYTHON" -m pip install -r "$REQUIREMENTS"
    fi
    cleanup_lock
    trap - EXIT
  fi
fi

if ! dependencies_ready; then
  echo "Accessibility Python dependencies are unavailable in $VENV" >&2
  exit 1
fi

export PYTHONPATH="$ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" "$@"
