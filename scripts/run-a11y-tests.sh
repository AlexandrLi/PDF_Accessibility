#!/usr/bin/env bash
# Local feedback loop after a11y sweep / migration script changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -z "${PYTHON:-}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  else
    PYTHON=python
  fi
fi

echo "Running a11y unit tests (scripts/lib/test_*.py) ..."
"$PYTHON" -m unittest discover -s lib -p 'test_*.py' -v
