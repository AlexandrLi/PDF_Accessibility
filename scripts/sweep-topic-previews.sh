#!/usr/bin/env bash
# Post-remediation sweeps on topic preview PDFs only — see docs/PREVIEW_SCOPE.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -r scripts/requirements-migrate.txt

export PYTHONUNBUFFERED=1
exec python3 scripts/sweep_topic_previews.py "$@"
