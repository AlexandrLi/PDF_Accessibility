#!/usr/bin/env bash
# Local feedback loop after a11y sweep / migration script changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Running a11y unit tests (scripts/lib/test_*.py) ..."
exec "$ROOT/scripts/with-a11y-python.sh" \
  -m unittest discover -s scripts/lib -p 'test_*.py' -v
