#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/scripts/with-a11y-python.sh" \
  "$ROOT/scripts/sweep_accessibility_issue_map.py" \
  "$@"
