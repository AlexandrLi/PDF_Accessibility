#!/usr/bin/env bash
# Post-remediation sweeps on topic preview PDFs only — see docs/PREVIEW_SCOPE.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONUNBUFFERED=1
exec "$ROOT/scripts/with-a11y-python.sh" \
  "$ROOT/scripts/sweep_topic_previews.py" \
  "$@"
