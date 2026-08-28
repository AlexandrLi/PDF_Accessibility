#!/usr/bin/env bash
# Topic preview a11y migration (Adobe + sweeps) — see docs/PREVIEW_SCOPE.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONUNBUFFERED=1
exec "$ROOT/scripts/with-a11y-python.sh" \
  "$ROOT/scripts/migrate_channels_worksheets.py" \
  "$@"
